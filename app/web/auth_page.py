"""登录 / 注册 / 退出 / 我的（`web/` 层，一个文件 = 一个 URL —— ADR-0010）。

表单用**原生 POST + 重定向（302）**，不用 fetch（ADR-0004：状态住 URL 与服务端）。
失败时把错误信息渲染回同一页，而不是弹一个 JS 提示 —— 那样刷新就丢。

Cookie 的四个属性（v1 的 17 项加固之一，`docs/v1行为规格.md` §9）：

| 属性 | 为什么 |
|---|---|
| `httponly=True` | JS 读不到令牌 —— XSS 拿不到会话 |
| `samesite="lax"` | 跨站表单带不上 cookie（CSRF 的第一层纵深） |
| `secure` | 线上必须开；本机 http 开发默认关（配置项） |
| `max_age` | 与令牌 TTL 一致，否则 cookie 比令牌活得久（表现为"莫名 403"） |

CSRF 的第二层（自定义头）在 v1 是给 fetch 用的；原生表单提交用 SameSite 就够，
等真出现 fetch 再补 —— 不做没被要求的抽象。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.account import service as account
from app.config import Settings
from app.db.models import User
from app.deps import SESSION_COOKIE, get_current_user, get_session, get_settings
from app.errors import AppError
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]


def _set_session_cookie(response: RedirectResponse, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=int(account.repository.TOKEN_TTL.total_seconds()),
        path="/",
    )


@router.get("/login")
def login_form(request: Request, user: CurrentUserDep) -> object:
    if user is not None:
        return RedirectResponse("/me", status_code=302)
    return render(request, "login.html", {"error": None, "email": ""})


@router.post("/login")
def login_submit(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> object:
    try:
        token = account.login(session, email=email, password=password)
    except AppError as e:
        # 失败渲染回同一页（不重定向）：把已填的邮箱带回去，只丢口令
        return render(request, "login.html", {"error": e.message, "email": email})

    response = RedirectResponse("/me", status_code=302)
    _set_session_cookie(response, token, settings)
    return response


@router.get("/register")
def register_form(request: Request, user: CurrentUserDep) -> object:
    if user is not None:
        return RedirectResponse("/me", status_code=302)
    return render(request, "register.html", {"error": None, "email": "", "invite": ""})


@router.post("/register")
def register_submit(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    email: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    invite_code: Annotated[str, Form()] = "",
) -> object:
    try:
        account.register(
            session,
            email=email,
            username=username,
            password=password,
            invite_code=invite_code,
        )
        token = account.login(session, email=email, password=password)
    except AppError as e:
        return render(
            request,
            "register.html",
            {"error": e.message, "email": email, "invite": invite_code},
        )

    response = RedirectResponse("/me", status_code=302)
    _set_session_cookie(response, token, settings)
    return response


@router.post("/logout")
def logout(request: Request, session: SessionDep) -> RedirectResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        account.logout(session, token)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
def me_page(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    """个人状态页。MVP 只做"我是谁 + 今天还剩多少额度"。"""
    if user is None:
        return RedirectResponse("/login", status_code=302)
    return render(
        request,
        "me.html",
        {
            "user": user,
            "remaining": account.remaining_units(session, user.id),
            "daily": account.DAILY_UNITS,
        },
    )
