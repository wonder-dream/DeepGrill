"""题库领域的页面数据装配（ADR-0005：`pages.py` 只调**本领域**的 service）。

它回答的是"题库的数据该怎么呈现"：列表每行的展示形状、详情页要显示哪几段。
跨领域的页面（如首页要同时要题库与掌握度）在 `web/` 层组合 —— 领域不知道页面
长什么样，只管把数据整理成页面要的形状。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.bank import service
from app.bank.service import QuestionCard


@dataclass
class BankListPage:
    cards: list[QuestionCard]
    total: int
    page: int
    has_prev: bool
    has_next: bool
    kind: str | None


def bank_list(
    session: Session, viewer_id: int | None, *, kind: str | None = None, page: int = 1
) -> BankListPage:
    """题库列表页的数据。分页状态由服务端算好（ADR-0004：状态住 URL 与服务端）。"""
    cards, total = service.browse(session, viewer_id, kind=kind, page=page)
    return BankListPage(
        cards=cards,
        total=total,
        page=page,
        has_prev=page > 1,
        has_next=page * service.PAGE_SIZE < total,
        kind=kind,
    )
