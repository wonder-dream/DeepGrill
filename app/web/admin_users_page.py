"""后台：首页 / 邀请码 / 账号 / 额度（决策 6、7、14）。

**路由只做校验与调用**（AGENTS.md §3.4）—— 规则在 `account/service.py`。
`/admin/review`（知识层人审）与 `/admin/feedback`（反馈处理）在各自的文件里，
它们各自是一个页面（ADR-0010：一个文件 = 一个 URL）。

后台是**本地管理工具**，不是公开面：所以它不做分页与搜索（20 人规模用不上），
但每一条写操作都必须回答"谁做的"—— 这里是 owner，由 `_require_owner` 保证。

邀请码的**生成与作废是危险操作**：两步确认（先到一个确认页，再提交执行），且
提交与确认之间强制等待 `CONFIRM_WAIT_SECONDS` 秒 —— 服务端校验签发时刻，提前
提交只会把确认页重新渲染回来并告知剩余秒数。纯服务端实现，不引入 JS（ADR-0004）。
确认令牌是「签发时刻 + HMAC」的**无状态**形态：进程内随机密钥，不建内存状态表
（AGENTS.md §3.2），代价是重启进程会让未完成的确认作废 —— 那本来就该重新确认。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.account import repository as account_repository
from app.account import service as account
from app.bank import feedback as feedback_service
from app.db.models import User
from app.deps import get_current_user, get_session
from app.errors import Forbidden, InvalidInput, NotFound
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]

#: 危险操作二次确认的强制等待秒数（服务端校验，不是前端倒计时）。
CONFIRM_WAIT_SECONDS = 5
#: 确认令牌的 HMAC 密钥。进程内随机 = 重启即失效（见模块 docstring）。
_CONFIRM_SECRET = secrets.token_hex(32)


def _require_owner(user: User | None) -> User:
    if user is None:
        raise Forbidden("请先登录")
    if user.role != "owner":
        raise Forbidden("只有 owner 能进后台")
    return user


def _parse_days(raw: str) -> int | None:
    # 留空 = 永久有效（表单的默认）；给了就必须是**正整数天数**。
    # 原来写的是 `int(days) if days.isdigit() else None`：于是 `-1`、`abc`、`3.5`
    # 全都静默变成"永久有效" —— 一个输入错误换来的是一张永不过期的邀请码。
    raw = raw.strip()
    if raw == "":
        return None
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    raise InvalidInput(f"有效期只能是正整数天数（收到 {raw!r}）")


def _confirm_token(action: str, issued_at: int) -> str:
    sig = hmac.new(
        _CONFIRM_SECRET.encode(), f"{action}:{issued_at}".encode(), hashlib.sha256
    ).hexdigest()[:16]
    return f"{issued_at}-{sig}"


def _issued_at(action: str, token: str) -> int:
    """校验确认令牌并返回签发时刻；不合法的令牌直接拒绝。"""
    try:
        raw_ts, sig = token.rsplit("-", 1)
        issued_at = int(raw_ts)
    except ValueError:
        raise InvalidInput("确认凭据不合法，请回列表重新发起") from None
    expect = hmac.new(
        _CONFIRM_SECRET.encode(), f"{action}:{issued_at}".encode(), hashlib.sha256
    ).hexdigest()[:16]
    if not hmac.compare_digest(sig, expect):
        raise InvalidInput("确认凭据不合法，请回列表重新发起")
    return issued_at


def _render_invites(
    request: Request,
    owner: User,
    session: Session,
    pending: dict | None = None,
) -> object:
    """渲染邀请码页；`pending` 非空时同时渲染确认悬浮窗（两步确认的第二步）。"""
    rows = account_repository.list_invites(session)
    return render(
        request,
        "admin_invites.html",
        {
            "user": owner,
            "rows": [
                {"invite": i, "state": account_repository.invite_state(i)} for i in rows
            ],
            "pending": pending,
        },
    )


def _pending_confirm(
    action: str,
    *,
    code: str = "",
    days_valid: int | None = None,
    issued_at: int | None = None,
    too_fast: int = 0,
) -> dict:
    """构造确认悬浮窗的上下文。`issued_at` 复用原值时，已等待的时间继续累计。"""
    if issued_at is None:
        issued_at = int(time.time())
    if action == "create":
        validity = "永久有效" if days_valid is None else f"有效期 {days_valid} 天"
        summary = f"生成一张{validity}的邀请码"
    else:
        summary = f"作废邀请码 {code}"
    return {
        "action": action,
        "code": code,
        "days_valid": days_valid,
        "summary": summary,
        "token": _confirm_token(action, issued_at),
        "wait_seconds": CONFIRM_WAIT_SECONDS,
        "too_fast": too_fast,
    }


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
    return _render_invites(request, _require_owner(user), session)


@router.post("/admin/invites")
def create_invite(
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    days: Annotated[str, Form()] = "",
) -> object:
    owner = _require_owner(user)
    # 第一步只校验并弹出确认悬浮窗，**不生成**：真正的生成在 /confirm 里。
    return _render_invites(
        request, owner, session, _pending_confirm("create", days_valid=_parse_days(days))
    )


@router.post("/admin/invites/{code}/revoke")
def revoke_invite(code: str, request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    owner = _require_owner(user)
    # 第一步只校验：码要存在、且未使用 —— 用过的码是发放记录，400 和原来一样。
    invite = account_repository.find_invite(session, code)
    if invite is None:
        raise NotFound("这张邀请码不存在")
    if account_repository.invite_state(invite) != "unused":
        raise InvalidInput("用过的码不能删：它是发放记录（谁用了、什么时候），删掉就查不到了")
    return _render_invites(request, owner, session, _pending_confirm("revoke", code=code))


@router.post("/admin/invites/confirm")
def confirm_invite(
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    action: Annotated[str, Form()],
    token: Annotated[str, Form()],
    code: Annotated[str, Form()] = "",
    days: Annotated[str, Form()] = "",
) -> object:
    owner = _require_owner(user)
    if action not in ("create", "revoke"):
        raise InvalidInput(f"未知操作 {action!r}")
    issued_at = _issued_at(action, token)
    # 强制等待是**服务端**校验：提前提交不执行，而是把悬浮窗渲染回来、
    # 带上剩余秒数。签发时刻沿用原值 —— 用户已经等过的时间累计，不用从头再来。
    remaining = CONFIRM_WAIT_SECONDS - (int(time.time()) - issued_at)
    if remaining > 0:
        return _render_invites(
            request,
            owner,
            session,
            _pending_confirm(
                action,
                code=code,
                days_valid=_parse_days(days),
                issued_at=issued_at,
                too_fast=remaining,
            ),
        )
    if action == "create":
        account.new_invite(session, created_by=owner.id, days_valid=_parse_days(days))
    else:
        account.revoke_invite(session, code)
    session.commit()  # 302 之前必须提交（见 `app/deps.py::get_session`）
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
    # 口令与令牌的撤销都要在响应之前落库 —— 否则"重置完立刻用旧会话"还有几十毫秒的窗口
    session.commit()
    return RedirectResponse("/admin/users?reset=ok", status_code=302)
