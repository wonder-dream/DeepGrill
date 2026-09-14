"""面试页的路由（`web/` 层，一个文件 = 一个 URL —— ADR-0010）。

**面试页是唯一跑 JS 的页面**（ADR-0004），它要做三件服务端替代不了的事：SSE 流式、
录音转写、当前轮次状态。三件都在这里了。

每个动作都有**两条路**，同一个逻辑：

| 动作 | 无 JS（整页） | 有 JS（流式） |
|---|---|---|
| 打字作答 | `POST /interview/{id}/answer` | `POST /interview/{id}/answer/stream` |
| 语音作答 | `POST /interview/{id}/voice` | `POST /interview/{id}/voice/stream` |

无 JS 的那两条**不是摆设**：它们是渐进增强的底座（浏览器禁用 JS、或脚本没加载
成功时页面照常能用），也是端到端测试最好写的那条路。两条路共用
`interview.run_round()` —— 判定、落库、收尾只有一份实现。

流式那两条的形状：

```
event: prose  data: {"delta": "…"}      面试官说的话，逐段来（**只到这为止**）
event: done   data: {"redirect": "…"}   去哪一页（浏览器跳过去）
event: error  data: {"message": "…"}    出事了（流已经开始，状态码改不了，就说出来）
```

状态住 URL 与服务端（ADR-0004）：题目 id、会话 id 全在路径里，刷新与前进后退天然
正确。**流只负责把文字早点显示出来**，它不改变任何状态 —— 跳转之后是服务端渲染的
真实状态。

判分是**同步**的（`def` handler → FastAPI 丢线程池）：一次 LLM 调用要等几秒，
但"事件循环里做同步 DB 查询"那条禁令（AGENTS.md §3.3）因此不会被踩到。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.account import service as account
from app.db import create_session_factory
from app.db.models import User
from app.deps import (
    get_current_user,
    get_engine,
    get_llm,
    get_session,
    get_stt,
    rate_limit_interviewer,
)
from app.errors import AppError, QuotaExhausted
from app.interview import service as interview
from app.llm.stt import MAX_AUDIO_BYTES, STTError, STTUnavailable, Transcript
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


# ---------------------------------------------------------------------------
# 开始与查看
# ---------------------------------------------------------------------------
@router.post("/interview/start")
def start(
    request: Request,
    session: SessionDep,
    user: UserDep,
    mode: Annotated[str, Form()] = "drill",
    question_id: Annotated[int | None, Form()] = None,
    _rate_limit: None = Depends(rate_limit_interviewer),
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


# ---------------------------------------------------------------------------
# 打字作答
# ---------------------------------------------------------------------------
@router.post("/interview/{session_id}/answer")
def answer(
    request: Request,
    session_id: int,
    session: SessionDep,
    user: UserDep,
    llm=Depends(get_llm),
    answer_text: Annotated[str, Form()] = "",
    _rate_limit: None = Depends(rate_limit_interviewer),
) -> object:
    me = _require(user, session)
    ts = interview.get_session_row(session, session_id, me.id)
    return _submit_round(
        request, session, me, ts, answer_text=answer_text.strip(), llm=llm, input_mode="text"
    )


@router.post("/interview/{session_id}/answer/stream")
def answer_stream(
    session_id: int,
    session: SessionDep,
    user: UserDep,
    engine: Engine = Depends(get_engine),
    llm=Depends(get_llm),
    answer_text: Annotated[str, Form()] = "",
    _rate_limit: None = Depends(rate_limit_interviewer),
) -> object:
    """打字作答的流式那条路。**越权校验在请求里做**（流开始之后就改不了状态码了）。"""
    me = _require(user, session)
    interview.get_session_row(session, session_id, me.id)
    return _stream_response(
        engine=engine, llm=llm, me=me, session_id=session_id, answer_text=answer_text.strip()
    )


# ---------------------------------------------------------------------------
# 语音作答
# ---------------------------------------------------------------------------
@router.post("/interview/{session_id}/voice")
def answer_voice(
    request: Request,
    session_id: int,
    session: SessionDep,
    user: UserDep,
    llm=Depends(get_llm),
    stt=Depends(get_stt),
    audio: Annotated[UploadFile | None, File()] = None,
    _rate_limit: None = Depends(rate_limit_interviewer),
) -> object:
    """语音作答（决策 32、33），无 JS 的那条路：转写 + 判分 + 整页重渲染。

    **转写直接进判分，不设确认环节。**
    """
    me = _require(user, session)
    ts = interview.get_session_row(session, session_id, me.id)
    data = report_service.interview_page_data(session, ts=ts, user_id=me.id)

    transcript, error = _transcribe(stt, audio)
    if error is not None:
        return _render_round_error(request, data, error[0], kind=error[1])

    assert transcript is not None
    return _submit_round(
        request,
        session,
        me,
        ts,
        answer_text=transcript.text.strip(),
        llm=llm,
        input_mode="voice",
        stt_text=transcript.text,
        notice=_placeholder_notice(transcript),
    )


@router.post("/interview/{session_id}/voice/stream")
def answer_voice_stream(
    session_id: int,
    session: SessionDep,
    user: UserDep,
    engine: Engine = Depends(get_engine),
    llm=Depends(get_llm),
    stt=Depends(get_stt),
    audio: Annotated[UploadFile | None, File()] = None,
    _rate_limit: None = Depends(rate_limit_interviewer),
) -> object:
    """语音作答的流式那条路。

    失败时回 **JSON**（不是页面）：这条路的消费者是 `interview.js`，它把消息写进
    状态行就够了，不需要整页渲染 —— 而"回一页 HTML 给一个期待 SSE 的消费者"会让
    前端拿到一屏乱码般的文本。
    """
    me = _require(user, session)
    interview.get_session_row(session, session_id, me.id)

    transcript, error = _transcribe(stt, audio)
    if error is not None:
        return JSONResponse({"error": error[0], "kind": error[1]}, status_code=400)

    assert transcript is not None
    response = _stream_response(
        engine=engine,
        llm=llm,
        me=me,
        session_id=session_id,
        answer_text=transcript.text.strip(),
        input_mode="voice",
        stt_text=transcript.text,
    )
    notice = _placeholder_notice(transcript)
    if notice:
        # 占位实现要在**流的开头**说清楚（后面的文字不是真的识别结果）
        response.headers["X-Interview-Notice"] = "placeholder-transcript"
    return response


def _placeholder_notice(transcript: Transcript) -> str | None:
    """占位转写的提示语（两种路径共用一句话，免得两处措辞慢慢分叉）。"""
    if not transcript.placeholder:
        return None
    return "这一轮是**占位转写**（没有接 STT 供应商）—— 上面那句不是真的识别结果。"


def _transcribe(stt, audio: UploadFile | None) -> tuple[Transcript | None, tuple[str, str] | None]:
    """读录音 → 校验 → 转写。返回 `(结果, 错误)`，**两者必有其一**。

    三件事按顺序说清楚：

    ① **录音只在内存里过一遍**。它被读成 bytes、交给 `stt.transcribe`，然后就丢掉
       —— 不写库、不写文件、不进 `task_logs`（决策 33 的原话是"不保存原始录音"，
       这是这条链上唯一的隐私面）。
       ⚠️ 唯一的落盘可能是 Starlette 自己的 spooled 临时文件（请求结束即删），
       我们**不额外**保存任何东西。

    ② **没有供应商时明确失败**（`STTUnavailable`）：页面上显示"语音转写还没接入，
       请用打字作答"，并且**不落这一轮** —— 绝不拿一段假转写去骗判分。
       `STTUnavailable` 与 `STTError` 分开：前者的处置是"去配一下"，后者是"再录一次"。

    ③ 占位实现（`DEEPGRILL_STT_PROVIDER=fake`）的转写会被**标注为占位**，然后照常
       进判分 —— 它存在的意义就是让整条链路能被真的走一遍。
    """
    if audio is None:
        return None, ("没有收到音频文件。", "input")

    raw = audio.file.read(MAX_AUDIO_BYTES + 1)  # 多读一个字节才能发现"超了"
    if not raw:
        return None, ("这段录音是空的 —— 再录一次试试。", "input")
    if len(raw) > MAX_AUDIO_BYTES:
        limit = MAX_AUDIO_BYTES // (1024 * 1024)
        return None, (f"录音太大了（上限 {limit} MB）。", "input")

    try:
        return stt.transcribe(raw, content_type=audio.content_type or ""), None
    except STTUnavailable as e:
        return None, (str(e), "stt_unavailable")
    except STTError as e:
        return None, (f"转写失败：{e}", "stt_error")


# ---------------------------------------------------------------------------
# 两条路共用的后半段
# ---------------------------------------------------------------------------
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
    """答一轮然后**整页重渲染**（无 JS 那条路）—— 收尾、跳题、记账都在这里。"""
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
        target = _finish_target(session, ts, llm)
        _record_usage(session, me.id, llm, before)
        return RedirectResponse(target, status_code=302)

    _record_usage(session, me.id, llm, before)
    data = report_service.interview_page_data(session, ts=ts, user_id=me.id)
    return render(
        request,
        "interview.html",
        {"data": data, "last": result, "llm_failed": result.llm_failed, "notice": notice},
    )


def _stream_response(
    *,
    engine: Engine,
    llm,
    me: User,
    session_id: int,
    answer_text: str,
    input_mode: str = "text",
    stt_text: str | None = None,
) -> StreamingResponse:
    """答一轮并把面试官的话流回去（SSE）。

    事件只有三种，前端只认三种：`prose`（增量文字）、`done`（去哪一页）、
    `error`（出事了，页面上说一句）。

    ## 为什么流自己开一个会话

    请求依赖的那个会话要到**响应结束之后**才提交，而我们在流的末尾就要发 `done`
    让浏览器跳转 —— 跳过去的那次页面渲染是**另一个请求**，它会读不到尚未提交的
    这一轮。所以这里显式 `commit()`，事件里给一个可以立刻打开的地址。

    ## 实测：首字延迟≈整轮时长（这不是实现问题）

    这个模型**先推理再吐正文** —— 实测一轮 `completion_tokens=1037` 里 **934 是
    推理**（`reasoning_content`，按基线不展示）。于是首字在 5.0s 出现、整轮 5.3s：
    流式几乎没有把"第一句话"提前，它省下的是**读的时间**（文字逐段出现，好过等
    5 秒再整段跳出来）。代价是页面在等待期间必须显示"面试官正在回应……"
    （`interview.js` 的 `live-status`）—— 否则用户面对 5 秒空白会以为卡住了。
    """
    factory = create_session_factory(engine)
    before = _usage_snapshot(llm)

    def event_stream() -> Iterator[str]:
        try:
            with factory() as s:
                ts = interview.get_session_row(s, session_id, me.id)
                result = None
                for event in interview.run_round(
                    s,
                    ts=ts,
                    answer_text=answer_text,
                    llm=llm,
                    input_mode=input_mode,
                    stt_text=stt_text,
                    stream=True,
                ):
                    if isinstance(event, interview.RoundResult):
                        result = event
                    else:
                        yield _sse("prose", {"delta": event})
                if result is None:  # run_round 的契约：它必须以 RoundResult 收尾
                    raise RuntimeError("这一轮没有产出结果")

                target = (
                    _finish_target(s, ts, llm)
                    if result.finished
                    else f"/interview/{session_id}"
                )
                _record_usage(s, me.id, llm, before)
                # ⚠️ 必须在发 done **之前**提交：浏览器收到 done 就跳转，而那次渲染
                # 是另一个请求 —— 提交晚一步它就读不到这一轮。
                s.commit()
                yield _sse("done", {"redirect": target, "llm_failed": result.llm_failed})
        except Exception as e:  # noqa: BLE001
            # 流已经开始，状态码改不了了 —— 那就**说出来**（§3.1：降级不静默），
            # 让前端提示一句、重新加载页面看真实状态。
            logger.exception("流式这一轮失败")
            yield _sse("error", {"message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # 反向代理（Cloudflare / nginx）默认会缓冲整个响应 —— 那样流式就没了
            "X-Accel-Buffering": "no",
        },
    )


def _finish_target(session: Session, ts, llm) -> str:
    """这道题问完之后去哪：还有下一题就跳过去，没有就收尾出报告。"""
    nxt = report_service.next_pending_session(session, interview_id=ts.interview_id)
    if nxt is not None:
        return f"/interview/{nxt.id}"
    report = report_service.finish_interview_by_id(
        session, interview_id=ts.interview_id, llm=llm
    )
    return f"/report/{report.interview_id}"


def _sse(event: str, payload: dict[str, object]) -> str:
    """一帧 SSE。`ensure_ascii=False` —— 中文直接出去，不变成 `\\uXXXX`。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _render_round_error(request: Request, data, message: str, *, kind: str = "input") -> object:
    """语音这一侧的失败页（**不落轮次**）。

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
