"""我的（`/me`）—— 一个文件 = 一个 URL（ADR-0010）。

它同时是**个人状态页**（决策 17：主页面的可视化形式就是掌握度矩阵）与面试历史的
入口。跨领域装配的典型：额度来自 `account`、矩阵来自 `knowledge`、面试列表来自
`interview` —— 这正是 `web/` 存在的理由（ADR-0005：跨领域动作放能依赖两者的上层），
而**规则一条都不在这里**。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.account import service as account
from app.db.models import Interview, User
from app.deps import get_current_user, get_session
from app.knowledge import service as knowledge
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
        },
    )
