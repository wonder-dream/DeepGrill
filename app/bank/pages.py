"""题库领域的页面数据装配（ADR-0005：`pages.py` 只调**本领域**的 service）。

它回答的是"题库的数据该怎么呈现"：列表每行的展示形状、详情页要显示哪几段。
跨领域的页面（如首页要同时要题库与掌握度）在 `web/` 层组合 —— 领域不知道页面
长什么样，只管把数据整理成页面要的形状。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.bank import repository, service
from app.bank.service import QuestionCard

#: 难度的离散档（筛选表单用）：简单 1-2 / 中等 3 / 困难 4-5。
DIFF_TIERS: dict[str, tuple[int, int]] = {
    "easy": (1, 2),
    "mid": (3, 3),
    "hard": (4, 5),
}


@dataclass
class BankListPage:
    cards: list[QuestionCard]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_prev: bool
    has_next: bool
    kind: str | None
    # 筛选表单：当前选中的值 + 下拉选项（选项由服务端收窄，级联的 JS 只是提速）
    diff: str
    domain_id: int | None
    point_id: int | None
    keyword: str
    domains: list = field(default_factory=list)
    points: list = field(default_factory=list)
    #: 全部知识点（id/name/domain_id）的 JSON —— 级联 JS 从 data-points 读它
    points_json: str = "[]"
    owner_only: bool = False


def bank_list(
    session: Session,
    viewer_id: int | None,
    *,
    kind: str | None = None,
    owner_only: bool = False,
    page: int = 1,
    page_size: int = service.PAGE_SIZE,
    diff: str = "",
    domain_id: int | None = None,
    point_id: int | None = None,
    keyword: str = "",
) -> BankListPage:
    """题库列表页的数据。分页与筛选状态由服务端算好（ADR-0004：状态住 URL 与服务端）。"""
    lo, hi = DIFF_TIERS.get(diff, (None, None))
    cards, total = service.browse(
        session,
        viewer_id,
        kind=kind,
        owner_only=owner_only,
        page=page,
        page_size=page_size,
        difficulty_min=lo,
        difficulty_max=hi,
        domain_id=domain_id,
        point_id=point_id,
        keyword=keyword or None,
    )
    domains, all_points = repository.filter_options(session)
    return BankListPage(
        cards=cards,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-total // page_size)),
        has_prev=page > 1,
        has_next=page * page_size < total,
        kind=kind,
        diff=diff,
        domain_id=domain_id,
        point_id=point_id,
        keyword=keyword,
        domains=domains,
        points=(
            [p for p in all_points if p.domain_id == domain_id] if domain_id else all_points
        ),
        points_json=json.dumps(
            [{"id": p.id, "name": p.name, "domain_id": p.domain_id} for p in all_points],
            ensure_ascii=False,
        ),
        owner_only=owner_only,
    )
