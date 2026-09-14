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

from app.bank import pages, service
from app.deps import get_session
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

#: MVP：还没有登录，每个请求都是"匿名查看者"。
#: 它**不是**"没有权限" —— 匿名能看到全部公共题（产品面向公众，决策 6）。
ANONYMOUS: int | None = None


@router.get("/bank")
def bank_list(
    request: Request,
    session: SessionDep,
    kind: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> object:
    data = pages.bank_list(session, ANONYMOUS, kind=kind, page=page)
    return render(request, "bank_list.html", {"data": data})


@router.get("/bank/{question_id}")
def bank_detail(request: Request, question_id: int, session: SessionDep) -> object:
    # 不可见与不存在都由 service 抛 NotFound → main 的异常处理器渲染错误页
    data = service.detail(session, question_id, ANONYMOUS)
    return render(request, "bank_detail.html", {"data": data})
