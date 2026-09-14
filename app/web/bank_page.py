"""题库页面的路由（`web/` 层，ADR-0010：**一个文件 = 一个 URL**）。

它在 `web/` 而不是 `bank/` 里，因为路由是"HTTP 那一边的事"：`bank` 领域只提供
`service` / `pages`，不知道页面长什么样（CONTEXT.md 的 `web` 词条：唯一同时认识
多个领域的模块，**不含业务规则**）。

AGENTS.md §3.4：路由层只做参数校验、鉴权、调 service、返回响应 —— 业务规则一行
都不许写在这里（v1 的 `bank_questions` 就是 124 行内联逻辑长出来的）。

本页目前**没有认证**（账号域还没做），所以查看者是显式的 `None`（= 只看到公共题）。
`bank/repository.visible_to()` 已按 viewer 参数化，接上登录时改的只是取值的这一处。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.account import service as account
from app.bank import favorites, pages, service
from app.db.models import User
from app.deps import get_current_user, get_session
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]


def _viewer(user: User | None) -> int | None:
    """把"当前用户"折成仓储要的 `viewer_id`。

    匿名（None）**不是**"没有权限" —— 匿名能看到全部公共题（面向公众，决策 6）；
    登录之后多看到的是**自己的私有题集**。
    """
    return user.id if user is not None else None


@router.get("/bank")
def bank_list(
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    kind: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> object:
    data = pages.bank_list(session, _viewer(user), kind=kind, page=page)
    return render(
        request,
        "bank_list.html",
        # 浏览**永远**可用（决策 13），所以这里只带额度状态用于提示，不做拦截。
        {
            "data": data,
            "quota": account.quota_state(session, user.id) if user else None,
            # 一页的收藏状态**一次问完**（决策 63）—— 不在模板里逐题查库
            "favorited": (
                favorites.favorited_ids(
                    session, user_id=user.id, question_ids=[c.id for c in data.cards]
                )
                if user
                else set()
            ),
        },
    )


@router.get("/bank/{question_id}")
def bank_detail(
    request: Request, question_id: int, session: SessionDep, user: CurrentUserDep
) -> object:
    # 不可见与不存在都由 service 抛 NotFound → main 的异常处理器渲染错误页
    data = service.detail(session, question_id, _viewer(user))
    return render(
        request,
        "bank_detail.html",
        {
            "data": data,
            "can_start": user is not None,
            "quota": account.quota_state(session, user.id) if user else None,
            "is_favorited": (
                favorites.is_favorited(session, user_id=user.id, question_id=question_id)
                if user
                else False
            ),
        },
    )
