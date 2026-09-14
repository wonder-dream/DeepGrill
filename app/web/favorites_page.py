"""收藏夹的页面与动作（决策 63）—— 一个文件管一条功能线的 URL。

`web/` 层，一个文件 = 一条功能线（ADR-0010；`feedback_page.py` 是同一个先例：
它的 URL 横跨 `/bank`、`/me`、`/admin` 三个前缀，因为那是同一件事的三个面）。

三个 URL：

```
POST /bank/{id}/favorite          收藏（幂等 —— 连点两次不会出两行）
POST /bank/{id}/favorite/remove   取消收藏（幂等）
GET  /me/favorites                我的收藏（分页）
```

**都不消耗额度点**（决策 63）：收藏只是写一行指针，没有任何 LLM 调用。

动作是**表单 POST + 302 回原页**，不是 fetch（ADR-0004：状态住 URL 与服务端）——
于是刷新不会重复提交，而"到底收藏了没"永远以服务端为准。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.bank import favorites
from app.db.models import User
from app.deps import get_current_user, get_session
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]


@router.post("/bank/{question_id}/favorite")
def add_favorite(
    question_id: int, session: SessionDep, user: CurrentUserDep
) -> RedirectResponse:
    """收藏。未登录 → 回登录页（与题目反馈同一个处置：**不抛 403**，
    误触的人该被送到登录页，而不是撞一堵墙）。"""
    if user is None:
        return RedirectResponse("/login", status_code=302)
    favorites.add(session, user_id=user.id, question_id=question_id)
    return RedirectResponse(f"/bank/{question_id}?favorite=ok", status_code=302)


@router.post("/bank/{question_id}/favorite/remove")
def remove_favorite(
    question_id: int, session: SessionDep, user: CurrentUserDep
) -> RedirectResponse:
    if user is None:
        return RedirectResponse("/login", status_code=302)
    favorites.remove(session, user_id=user.id, question_id=question_id)
    return RedirectResponse(f"/bank/{question_id}?favorite=off", status_code=302)


@router.get("/me/favorites")
def my_favorites(
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    page: Annotated[int, Query(ge=1)] = 1,
) -> object:
    if user is None:
        return RedirectResponse("/login", status_code=302)
    cards, total = favorites.browse(session, user_id=user.id, page=page)
    return render(
        request,
        "my_favorites.html",
        {
            "user": user,
            "cards": cards,
            "total": total,
            "page": page,
            "has_prev": page > 1,
            "has_next": page * favorites.PAGE_SIZE < total,
        },
    )
