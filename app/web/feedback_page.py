"""题目反馈的页面（用户报 + 管理员处理）。

用户侧只要两处：题目详情页上的报告表单，与"我的反馈"列表。
管理员侧是待处理队列 + 两个动作。

**它写在 `web/` 而不是领域里**：路由属于 HTTP 那一边（ADR-0010 的目录纪律），
而规则全在 `bank/feedback.py`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.bank import feedback
from app.db.models import User
from app.deps import get_current_user, get_session
from app.errors import Forbidden
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]


def _require_owner(user: User | None) -> User:
    if user is None:
        raise Forbidden("请先登录")
    if user.role != "owner":
        raise Forbidden("只有 owner 能处理反馈")
    return user


@router.post("/bank/{question_id}/feedback")
def submit_feedback(
    question_id: int,
    session: SessionDep,
    user: CurrentUserDep,
    kind: Annotated[str, Form()] = "unclear",
    detail: Annotated[str, Form()] = "",
    duplicate_point_ids: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """用户报问题。**必须登录** —— 匿名反馈在表里允许（`user_id` 可空），
    但产品上不接受：没有回报渠道的反馈是单向的。"""
    if user is None:
        return RedirectResponse("/login", status_code=302)

    points = [int(x) for x in duplicate_point_ids.replace("，", ",").split(",") if x.strip().isdigit()]
    feedback.submit(
        session,
        question_id=question_id,
        user_id=user.id,
        kind=kind,
        detail=detail,
        duplicate_point_ids=points,
    )
    # 回到题目页 —— 那里会显示"已提交"
    # （302 之前必须提交：依赖的提交发生在响应之后，见 `app/deps.py::get_session`）
    session.commit()
    return RedirectResponse(f"/bank/{question_id}?feedback=ok", status_code=302)


@router.get("/me/feedback")
def my_feedback_page(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    if user is None:
        return RedirectResponse("/login", status_code=302)
    return render(
        request,
        "my_feedback.html",
        {"user": user, "rows": feedback.my_feedback(session, user.id),
         "labels": feedback.KIND_LABELS},
    )


@router.get("/admin/feedback")
def feedback_queue(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    owner = _require_owner(user)
    return render(
        request,
        "admin_feedback.html",
        {
            "user": owner,
            "rows": feedback.open_feedback(session),
            "counts": feedback.counts(session),
            "labels": feedback.KIND_LABELS,
            "question_of": lambda row: feedback.question_of(session, row),
        },
    )


@router.post("/admin/feedback/{feedback_id}/{action}")
def handle_feedback(
    feedback_id: int,
    action: str,
    session: SessionDep,
    user: CurrentUserDep,
    note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    _require_owner(user)
    feedback.resolve(session, feedback_id=feedback_id, action=action, note=note)
    session.commit()  # 302 之前必须提交（见 `app/deps.py::get_session`）
    return RedirectResponse("/admin/feedback", status_code=302)
