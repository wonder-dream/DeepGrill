"""面试页的路由（`web/` 层，一个文件 = 一个 URL —— ADR-0010）。

**面试页是唯一跑 JS 的页面**（ADR-0004），它要做三件服务端替代不了的事：SSE 流式、
录音转写、当前轮次状态。这个文件现在有**录音转写**（决策 32）与轮次状态；
SSE 流式是第三件，还没做。

状态住 URL 与服务端（ADR-0004）：题目 id、会话 id 全在路径里，刷新与前进后退天然正确。
**语音也是同一套**：录音由浏览器 POST 到这个页面的 `/voice`，服务端转写完直接判分、
返回整页 —— 没有"先转写、让用户确认、再提交"那一套（决策 33）。

判分是**同步**的（`def` handler → FastAPI 丢线程池）：一次 LLM 调用要等几秒，
但"事件循环里做同步 DB 查询"那条禁令（AGENTS.md §3.3）因此不会被踩到。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.account import service as account
from app.db.models import User
from app.deps import get_current_user, get_llm, get_session, get_stt
from app.errors import AppError, QuotaExhausted
from app.interview import service as interview
from app.llm.stt import MAX_AUDIO_BYTES, STTError, STTUnavailable
from app.report import service as report_service
from app.web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


def _require(user: User | None, session: Session) -> User:
    """需要登录。未登录时**直接重定向**（面试页天然需要身份，不必回一个 403 页）。"""
    from app.errors import Forbidden

    if user is None:
        raise Forbidden("请先登录")
    return user


@router.post("/interview/start")
def start(
    request: Request,
    session: SessionDep,
    user: UserDep,
    mode: Annotated[str, Form()] = "drill",
    question_id: Annotated[int | None, Form()] = None,
) -> object:
    """从题库页（或首页）开始一次面试。**扣额度点在这里发生**（service 里）。

    额度不足**不是错误页**，而是决策 13 的降级：面试官今天歇了，题库照旧。
    所以这一支单独渲染，把"还能做什么"写在页面上 —— 用户看到的不该是一堵墙。
    """
    me = _require(user, session)
    try:
        if mode == "interview":
            row = interview.start_interview(session, user_id=me.id)
            ts = interview.sessions_of(session, row.id)[0]
        else:
            if question_id is None:
                raise AppError("请选择一道题")
            ts = interview.start_drill(session, user_id=me.id, question_id=question_id)
    except QuotaExhausted as e:
        return render(
            request,
            "interview_start_failed.html",
            {
                "message": e.message,
                "exhausted": True,
                "quota": account.quota_state(session, me.id),
            },
            status_code=e.status_code,
        )
    except AppError as e:
        return render(
            request, "interview_start_failed.html", {"message": e.message, "exhausted": False},
            status_code=e.status_code,
        )

    return RedirectResponse(f"/interview/{ts.id}", status_code=302)


@router.get("/interview/{session_id}")
def show(request: Request, session_id: int, session: SessionDep, user: UserDep) -> object:
    me = _require(user, session)
    ts = interview.get_session_row(session, session_id, me.id)
    data = report_service.interview_page_data(session, ts=ts, user_id=me.id)
    return render(request, "interview.html", {"data": data})


@router.post("/interview/{session_id}/answer")
def answer(
    request: Request,
    session_id: int,
    session: SessionDep,
    user: UserDep,
    llm=Depends(get_llm),
    answer_text: Annotated[str, Form()] = "",
) -> object:
    me = _require(user, session)
    ts = interview.get_session_row(session, session_id, me.id)
    return _submit_round(
        request, session, me, ts, answer_text=answer_text.strip(), llm=llm, input_mode="text"
    )


@router.post("/interview/{session_id}/voice")
def answer_voice(
    request: Request,
    session_id: int,
    session: SessionDep,
    user: UserDep,
    llm=Depends(get_llm),
    stt=Depends(get_stt),
    audio: Annotated[UploadFile | None, File()] = None,
) -> object:
    """语音作答（决策 32、33）。**转写直接进判分，不设确认环节。**

    三件事按顺序说清楚：

    ① **录音只在内存里过一遍**。它被读成 bytes、交给 `stt.transcribe`，然后就被
       丢掉 —— 不写库、不写文件、不进 `task_logs`（决策 33 的原话是"不保存原始
       录音"，这是这条链上唯一的隐私面）。
       ⚠️ 唯一的落盘可能是 Starlette 自己的 spooled 临时文件（请求结束即删），
       我们**不额外**保存任何东西。

    ② **没有供应商时明确失败**（`STTUnavailable`）：页面上显示"语音转写还没接入，
       请用打字作答"，并且**不落这一轮** —— 绝不拿一段假转写去骗判分。

    ③ 占位实现（`DEEPGRILL_STT_PROVIDER=fake`）的转写会**在页面上被标注为占位**，
       然后照常进判分：它存在的意义就是让整条链路能被真的走一遍。
    """
    me = _require(user, session)
    ts = interview.get_session_row(session, session_id, me.id)
    data = report_service.interview_page_data(session, ts=ts, user_id=me.id)

    if audio is None:
        return _render_round_error(request, data, "没有收到音频文件。")

    raw = audio.file.read(MAX_AUDIO_BYTES + 1)  # 多读一个字节才能发现"超了"
    if not raw:
        return _render_round_error(request, data, "这段录音是空的 —— 再录一次试试。")
    if len(raw) > MAX_AUDIO_BYTES:
        return _render_round_error(
            request, data, f"录音太大了（上限 {MAX_AUDIO_BYTES // (1024 * 1024)} MB）。"
        )

    try:
        transcript = stt.transcribe(raw, content_type=audio.content_type or "")
    except STTUnavailable as e:
        # **不落这一轮**：转写没有发生，就不该有一条"候选人的回答"
        return _render_round_error(request, data, str(e), kind="stt_unavailable")
    except STTError as e:
        return _render_round_error(request, data, f"转写失败：{e}", kind="stt_error")

    return _submit_round(
        request,
        session,
        me,
        ts,
        answer_text=transcript.text.strip(),
        llm=llm,
        input_mode="voice",
        stt_text=transcript.text,
        notice=(
            "这一轮是**占位转写**（没有接 STT 供应商）—— 上面那句不是真的识别结果。"
            if transcript.placeholder
            else None
        ),
    )


def _submit_round(
    request: Request,
    session: Session,
    me: User,
    ts,
    *,
    answer_text: str,
    llm,
    input_mode: str,
    stt_text: str | None = None,
    notice: str | None = None,
) -> object:
    """答一轮的**公共后半段**（打字与语音走同一条）—— 收尾、跳题、记账、渲染。

    两条入口（`/answer` 与 `/voice`）只在前半段不同：一个直接拿表单文本，一个先
    转写。**后半段合成一处**，是为了让"收尾 → 下一题 / 出报告"那条分支只有一份
    实现 —— 抄一遍就是多一处会漂的地方。
    """
    before = _usage_snapshot(llm)
    result = interview.submit_answer(
        session,
        ts=ts,
        answer_text=answer_text,
        llm=llm,
        input_mode=input_mode,
        stt_text=stt_text,
    )

    if result.finished:
        # 这道题问完了：还有下一题就跳过去，没有就收尾出报告
        nxt = report_service.next_pending_session(session, interview_id=ts.interview_id)
        if nxt is not None:
            _record_usage(session, me.id, llm, before)
            return RedirectResponse(f"/interview/{nxt.id}", status_code=302)
        report = report_service.finish_interview_by_id(
            session, interview_id=ts.interview_id, llm=llm
        )
        # ⚠️ 记账必须在这个**请求的末尾**：收尾还会调两次模型（判分 + 总结）。
        # 第一版只在 submit_answer 后记一次，于是那两次的 token 从来没进账本
        # —— 决策 14 的"第二道安全网"因此少记了大半。
        _record_usage(session, me.id, llm, before)
        return RedirectResponse(f"/report/{report.interview_id}", status_code=302)

    _record_usage(session, me.id, llm, before)
    data = report_service.interview_page_data(session, ts=ts, user_id=me.id)
    return render(
        request,
        "interview.html",
        {"data": data, "last": result, "llm_failed": result.llm_failed, "notice": notice},
    )


def _render_round_error(request: Request, data, message: str, *, kind: str = "input") -> object:
    """录音这一侧的失败页（**不落轮次**）。

    状态码用 400：这是"这次请求的输入不对 / 环境没配好"，不是服务端错误。页面照常
    渲染出来，用户可以直接改用打字继续 —— 不中断这场面试（决策 13 的同一种态度：
    降级，而不是把用户堵在门口）。
    """
    logger.info("语音这一轮没成（%s）：%s", kind, message)
    return render(
        request,
        "interview.html",
        {"data": data, "voice_error": message, "voice_error_kind": kind},
        status_code=400,
    )


def _usage_snapshot(llm) -> dict[str, int]:
    """请求开始时客户端的累计用量（用于取差值）。"""
    return dict(getattr(llm, "usage_total", {}) or {})


def _record_usage(session: Session, user_id: int, llm, before: dict[str, int]) -> int:
    """把这个请求里**全部** LLM 调用消耗的 token 记进额度账本（决策 14）。

    为什么取差值而不是读"最近一次"：一次请求可能调用多次（判定 + 判分 + 总结），
    而"最近一次"只会记到最后一次 —— 实测就是这么漏掉两次的。

    没有 `usage_total` 的客户端（测试替身）返回 0，**不报错**：替身本来就没有真实
    用量，而记账不该因为测试替身而炸。
    """
    after = getattr(llm, "usage_total", None)
    if not isinstance(after, dict):
        return 0
    total = sum(after.get(k, 0) - before.get(k, 0) for k in ("prompt_tokens", "completion_tokens"))
    if total < 0:  # 客户端被换过（不该发生）——不记负账
        return 0
    account.record_tokens(session, user_id, total)
    return total
