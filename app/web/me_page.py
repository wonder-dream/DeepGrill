"""我的（`/me`）—— 一个文件 = 一个 URL（ADR-0010）。

它同时是**个人状态页**（决策 17：主页面的可视化形式就是掌握度矩阵）与面试历史的
入口。跨领域装配的典型：额度来自 `account`、矩阵来自 `knowledge`、面试列表来自
`interview` —— 这正是 `web/` 存在的理由（ADR-0005：跨领域动作放能依赖两者的上层），
而**规则一条都不在这里**。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.account import service as account
from app.db.models import Interview, User
from app.deps import SESSION_COOKIE, get_current_user, get_session
from app.errors import Forbidden, InvalidInput
from app.knowledge import service as knowledge
from app.profile import service as profile_service
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]


@router.get("/me")
def me_page(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    if user is None:
        return RedirectResponse("/login", status_code=302)

    recent = list(
        session.execute(
            select(Interview)
            .where(Interview.user_id == user.id)
            .order_by(Interview.id.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    return render(
        request,
        "me.html",
        {
            "user": user,
            "remaining": account.remaining_units(session, user.id),
            "daily": account.DAILY_UNITS,
            "matrix": knowledge.mastery_matrix(session, user.id),
            "recent": recent,
            "counts": profile_service.count_remaining(session, user.id),
        },
    )


@router.get("/me/export")
def export_me(session: SessionDep, user: CurrentUserDep) -> Response:
    """导出我的数据（决策 23）。**一个 JSON 文件**，能下载、能带走。

    不渲染成页面：导出的用途是存档与迁移，不是浏览。
    """
    if user is None:
        return RedirectResponse("/login", status_code=302)

    payload = profile_service.export_user_data(session, user.id)
    filename = f"deepgrill-export-{user.id}.json"
    return JSONResponse(
        payload,
        headers={
            # 用 ASCII 文件名：HTTP 头里放非 ASCII 需要额外编码，而这里的名字
            # 不需要为了好看去冒那个险（邮箱可能含非 ASCII 字符）。
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/me/delete")
def delete_me(
    session: SessionDep,
    user: CurrentUserDep,
    confirm: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """注销账号与数据（决策 7 / 21：**硬删**）。

    三重防误触，因为这是不可逆的：
    ① 必须输入确认短语 `DELETE`
    ② 必须重输口令（复用登录的校验，不另存一份）
    ③ **owner 不能自己删** —— 删掉之后没人能进后台了

    删完立刻清 cookie：令牌行已经随事务删掉，但让浏览器也放掉它更干净。
    """
    if user is None:
        return RedirectResponse("/login", status_code=302)

    if user.role == "owner":
        raise Forbidden("owner 账号不能自行注销 —— 删掉之后没人能进后台")

    if confirm.strip() != "DELETE":
        raise InvalidInput("确认短语不正确（需要输入 DELETE）")

    if not account.verify_login(session, email=user.email, password=password):
        raise Forbidden("口令不正确")

    profile_service.delete_account(session, user.id)
    session.commit()

    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
