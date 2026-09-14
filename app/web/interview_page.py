"""面试页的路由（`web/` 层，一个文件 = 一个 URL —— ADR-0010）。

**面试页是唯一跑 JS 的页面**（ADR-0004），它要做三件服务端替代不了的事：SSE 流式、
录音转写、当前轮次状态。**MVP 只做其中不需要 JS 的那部分**：表单提交 + 整页重渲染。
流式与语音都在后面各自的笔里 —— 先把"这条链能不能跑通"做出来。

状态住 URL 与服务端（ADR-0004）：题目 id、会话 id 全在路径里，刷新与前进后退天然正确。

判分是**同步**的（`def` handler → FastAPI 丢线程池）：一次 LLM 调用要等几秒，
但"事件循环里做同步 DB 查询"那条禁令（AGENTS.md §3.3）因此不会被踩到。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.account import service as account
from app.db.models import User
from app.deps import get_current_user, get_llm, get_session
from app.errors import AppError, QuotaExhausted
from app.interview import service as interview
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

    before = _usage_snapshot(llm)
    result = interview.submit_answer(
        session, ts=ts, answer_text=answer_text.strip(), llm=llm
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
        {"data": data, "last": result, "llm_failed": result.llm_failed},
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
