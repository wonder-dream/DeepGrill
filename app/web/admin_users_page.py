"""后台：首页 / 邀请码 / 账号 / 额度（决策 6、7、14）。

**路由只做校验与调用**（AGENTS.md §3.4）—— 规则在 `account/service.py`。
`/admin/review`（知识层人审）与 `/admin/feedback`（反馈处理）在各自的文件里，
它们各自是一个页面（ADR-0010：一个文件 = 一个 URL）。

后台是**本地管理工具**，不是公开面：所以它不做分页与搜索（20 人规模用不上），
但每一条写操作都必须回答"谁做的"—— 这里是 owner，由 `_require_owner` 保证。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.account import repository as account_repository
from app.account import service as account
from app.bank import feedback as feedback_service
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
        raise Forbidden("只有 owner 能进后台")
    return user


@router.get("/admin")
def admin_home(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    owner = _require_owner(user)
    invites = account_repository.list_invites(session)
    users = account_repository.list_users(session)
    return render(
        request,
        "admin_home.html",
        {
            "user": owner,
            "invite_counts": {
                state: sum(1 for i in invites if account_repository.invite_state(i) == state)
                for state in ("unused", "used", "expired")
            },
            "user_count": len(users),
            "feedback_counts": feedback_service.counts(session),
            "usage": account.usage_summary(session, days=20),
        },
    )


@router.get("/admin/invites")
def invites_page(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    owner = _require_owner(user)
    rows = account_repository.list_invites(session)
    return render(
        request,
        "admin_invites.html",
        {
            "user": owner,
            "rows": [
                {"invite": i, "state": account_repository.invite_state(i)} for i in rows
            ],
        },
    )


@router.post("/admin/invites")
def create_invite(
    session: SessionDep,
    user: CurrentUserDep,
    days: Annotated[str, Form()] = "",
) -> RedirectResponse:
    owner = _require_owner(user)
    days_valid = int(days) if days.strip().isdigit() else None
    account.new_invite(session, created_by=owner.id, days_valid=days_valid)
    return RedirectResponse("/admin/invites", status_code=302)


@router.post("/admin/invites/{code}/revoke")
def revoke_invite(code: str, session: SessionDep, user: CurrentUserDep) -> RedirectResponse:
    _require_owner(user)
    account.revoke_invite(session, code)
    return RedirectResponse("/admin/invites", status_code=302)


@router.get("/admin/users")
def users_page(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    owner = _require_owner(user)
    rows = account_repository.list_users(session)
    return render(
        request,
        "admin_users.html",
        {
            "user": owner,
            "rows": [
                {
                    "account": u,
                    "remaining": account.remaining_units(session, u.id),
                    "used_today": account.units_used_today(session, u.id),
                }
                for u in rows
            ],
        },
    )


@router.post("/admin/users/{user_id}/password")
def reset_password(
    user_id: int,
    session: SessionDep,
    user: CurrentUserDep,
    new_password: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """重置口令（决策 7）。**同时撤销该用户的全部令牌** —— 否则旧会话还能用。"""
    _require_owner(user)
    account.admin_reset_password(session, user_id=user_id, new_password=new_password)
    return RedirectResponse("/admin/users?reset=ok", status_code=302)
