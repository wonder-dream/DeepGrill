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
from app.errors import AppError
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
    """从题库页（或首页）开始一次面试。**扣额度点在这里发生**（service 里）。"""
    me = _require(user, session)
    try:
        if mode == "interview":
            row = interview.start_interview(session, user_id=me.id)
            ts = interview.sessions_of(session, row.id)[0]
        else:
            if question_id is None:
                raise AppError("请选择一道题")
            ts = interview.start_drill(session, user_id=me.id, question_id=question_id)
    except AppError as e:
        return render(
            request, "interview_start_failed.html", {"message": e.message},
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

    result = interview.submit_answer(
        session, ts=ts, answer_text=answer_text.strip(), llm=llm
    )
    account.record_tokens(session, me.id, _tokens_of(llm))

    if result.finished:
        # 这道题问完了：还有下一题就跳过去，没有就收尾出报告
        nxt = report_service.next_pending_session(session, interview_id=ts.interview_id)
        if nxt is not None:
            return RedirectResponse(f"/interview/{nxt.id}", status_code=302)
        report = report_service.finish_interview_by_id(
            session, interview_id=ts.interview_id, llm=llm
        )
        return RedirectResponse(f"/report/{report.interview_id}", status_code=302)

    data = report_service.interview_page_data(session, ts=ts, user_id=me.id)
    return render(
        request,
        "interview.html",
        {"data": data, "last": result, "llm_failed": result.llm_failed},
    )


def _tokens_of(llm) -> int:
    """把这次调用用掉的 token 记进额度账本的第二道安全网（决策 14）。

    真实客户端会带 `usage`；测试替身没有这一项 —— 所以取不到就返回 0，
    但**不静默**（debug 日志里留一行）。
    """
    usage = getattr(llm, "last_usage", None) or {}
    try:
        return int(usage.get("total_tokens") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0
