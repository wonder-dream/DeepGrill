"""事后治理页（`/admin/quality`）—— 质量信号的**处置**（决策 5 + ADR-0002）。

决策 5 把内容治理定成"自动门禁进池 + **事后治理**"。观测页负责"看得见"（它只读），
这一页负责"动手"：三个动作，各自对应一种判断。

| 动作 | 什么时候用 | 它改什么 |
|---|---|---|
| 结掉 | 看过了，确实有问题（也许已经改过题） | 只把这条标记置 `resolved` |
| 忽略 | 看过了，是误报 | 只把这条标记置 `dismissed` |
| 藏起来 | 这道题确实不该继续出现在题库里 | `visibility='hidden'` + 结掉这道题的所有标记 |

**为什么把"藏起来"和"结掉"分开**：前者改题库、后者只改标记。合成一个按钮的话，
"我只是确认了一下"与"我要让它下架"就分不开了 —— 而后者是不可逆的（对用户而言
题就消失了）。

只读那一半在 `/admin/observability`（决策 23），这里只做动作 —— 两个页面的职责
因此不重叠：一个回答"现在什么状态"，一个回答"我要处理这一条"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.bank import quality
from app.db.models import Question, User
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
        raise Forbidden("只有 owner 能处理题库质量")
    return user


@router.get("/admin/quality")
def quality_page(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    owner = _require_owner(user)
    flags = quality.open_flags(session)
    return render(
        request,
        "admin_quality.html",
        {
            "user": owner,
            "flags": flags,
            # 题干要显示出来 —— 只给一个 id 的治理页等于让人先自己去别处查
            "stems": {f.question_id: _stem(session, f.question_id) for f in flags},
        },
    )


def _stem(session: Session, question_id: int) -> str:
    """取题干。

    ⚠️ 这里**故意走 `session.get`**（而不是面向浏览的 `visible_to()`）：治理页要看
    包括刚被标 `hidden` 的题 —— 那正是这一页上一步做出来的状态。用浏览入口会让
    它变成"（题不见了）"，于是没人能确认自己刚才做了什么。
    """
    question = session.get(Question, question_id)
    return question.stem if question is not None else "（题不见了）"


@router.post("/admin/quality/{flag_id}/resolve")
def resolve_flag(flag_id: int, session: SessionDep, user: CurrentUserDep) -> RedirectResponse:
    _require_owner(user)
    quality.close(session, flag_id=flag_id, status=quality.RESOLVED)
    return RedirectResponse("/admin/quality?done=resolved", status_code=302)


@router.post("/admin/quality/{flag_id}/dismiss")
def dismiss_flag(flag_id: int, session: SessionDep, user: CurrentUserDep) -> RedirectResponse:
    _require_owner(user)
    quality.close(session, flag_id=flag_id, status=quality.DISMISSED)
    return RedirectResponse("/admin/quality?done=dismissed", status_code=302)


@router.post("/admin/quality/{flag_id}/hide")
def hide_flag(flag_id: int, session: SessionDep, user: CurrentUserDep) -> RedirectResponse:
    """藏起来 —— 治理里唯一改题库的动作（最保守的那种：不删数据）。"""
    _require_owner(user)
    quality.hide_question(session, flag_id=flag_id)
    return RedirectResponse("/admin/quality?done=hidden", status_code=302)
