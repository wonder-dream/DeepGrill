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

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.account import service as account
from app.bank import favorites, pages, promotion, service
from app.db.models import User
from app.deps import get_current_user, get_llm, get_session, rate_limit_interviewer
from app.knowledge import explanation
from app.llm import LLMError
from app.web import llm_usage
from app.web.templating import render

logger = logging.getLogger(__name__)

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
    mine: Annotated[int, Query(ge=0, le=1)] = 0,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = service.PAGE_SIZE,
    diff: Annotated[str, Query()] = "",
    domain: Annotated[int | None, Query(ge=1)] = None,
    point: Annotated[int | None, Query(ge=1)] = None,
    q: Annotated[str, Query(max_length=100)] = "",
) -> object:
    """题库列表。`?mine=1` 就是首页那个「我的私有题集」入口（决策 3）。

    它**复用同一个函数**、只多一个筛选条件：另写一条"只查我的题"的路径，等于多
    一处能漏掉可见性过滤的地方（AGENTS.md §3.5）。筛选条件（题型/难度档/领域/
    知识点/关键词）全在 URL 上 —— ADR-0004，状态住 URL 与服务端。
    """
    data = pages.bank_list(
        session,
        _viewer(user),
        kind=kind,
        page=page,
        page_size=size,
        diff=diff,
        domain_id=domain,
        point_id=point,
        keyword=q.strip(),
        owner_only=bool(mine),
    )
    return render(
        request,
        "bank_list.html",
        # 浏览**永远**可用（决策 13），所以这里只带额度状态用于提示，不做拦截。
        {
            "data": data,
            "mine": bool(mine),
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
    return _render_detail(request, session, data, user, notice=None)


@router.post("/bank/{question_id}/explain")
def explain_question(
    request: Request,
    question_id: int,
    session: SessionDep,
    user: CurrentUserDep,
    llm=Depends(get_llm),
    _rate_limit: None = Depends(rate_limit_interviewer),
) -> object:
    """按需生成讲解（决策 67）。

    三件事：

    ① **只对可见的题生成** —— 题目行来自 `service.detail()`（它过 `visible_to()`），
       于是"别人的私有题"在这里自动 404，不必另写一套判断。
    ② **不扣额度点**（它属于题库那一边，基线的额度列里没有它），
       但这次调用的 token **要进账本**（决策 14）—— 否则"钱花在哪"答不出来。
    ③ **失败要说出来**：模型挂了就把错误渲染回详情页（含一句"讲解没生成出来"），
       而不是静默地什么都不显示（§3.1）。
    """
    me = user
    if me is None:
        return RedirectResponse("/login", status_code=302)

    data = service.detail(session, question_id, _viewer(user))
    before = llm_usage.snapshot(llm)
    try:
        # 生成完就跳回详情页（那一页会从缓存里读出来显示），所以这里不要返回值 ——
        # 留一个没人用的 `result` 变量只会让人以为后面还会用到它。
        explanation.explain(session, question=data.question, llm=llm)
    except LLMError as e:
        llm_usage.record(session, me.id, llm, before)
        logger.warning("题目 %s 的讲解没生成出来：%s", question_id, e)
        return _render_detail(
            request, session, data, user,
            notice=f"讲解没生成出来：{e}",
            status_code=502,
        )

    llm_usage.record(session, me.id, llm, before)
    # 跳回详情页之前提交：那一页要**读缓存**显示刚生成的讲解，而依赖的提交发生在
    # 响应之后（见 `app/deps.py::get_session`）
    session.commit()
    return RedirectResponse(f"/bank/{question_id}?explain=ok", status_code=302)


@router.post("/bank/{question_id}/promote")
def promote_question(
    request: Request,
    question_id: int,
    session: SessionDep,
    user: CurrentUserDep,
) -> object:
    """晋升自己的私有题到公共题库（决策 9）。

    **门禁失败不是错误页**：它返回 200 + 详情页 + 一条"哪一条没过"的说明 ——
    用户可以改完再试。做成 4xx 的话，"我该怎么改"就没地方显示了。

    过闸之后题会进**公共待定池**（`primary_point_id=NULL`）：挂到人审过的知识点上是
    知识层管道的事（决策 46），而在此之前它没有考察点（页面上写着这件事）。
    """
    me = user
    if me is None:
        return RedirectResponse("/login", status_code=302)

    data = service.detail(session, question_id, _viewer(user))
    result = promotion.promote(session, question=data.question, user_id=me.id)
    if not result.passed:
        return _render_detail(
            request, session, data, user,
            notice=f"门禁没过：{result.summary()}",
            status_code=200,
        )
    session.commit()  # 302 之前必须提交（见 `app/deps.py::get_session`）
    return RedirectResponse(f"/bank/{question_id}?promoted=ok", status_code=302)


def _render_detail(request: Request, session: Session, data, user, *, notice, status_code: int = 200) -> object:
    """详情页的渲染（GET 与两条"动作失败"的路共用，免得几处慢慢分叉）。"""
    cached_explanation = explanation.cached(session, question=data.question)
    return render(
        request,
        "bank_detail.html",
        {
            "data": data,
            "can_start": user is not None,
            "quota": account.quota_state(session, user.id) if user else None,
            "is_favorited": False if user is None else favorites.is_favorited(
                session, user_id=user.id, question_id=data.question.id
            ),
            # 只有**自己的私有题**才有晋升按钮（决策 9 是"用户主动晋升自己的题"）
            "is_mine": user is not None and data.question.owner_user_id == user.id,
            "explanation": cached_explanation,
            "explain_notice": notice,
        },
        status_code=status_code,
    )
