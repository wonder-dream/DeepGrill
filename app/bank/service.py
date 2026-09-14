"""题库领域的业务规则（ADR-0005：模块级函数，`db` 当第一个参数显式传入）。

只做两件 MVP 需要的事：**列题**与**看一道题**。规则本身很少 —— 大部分"什么能看"
已经在 `repository.visible_to()` 里（那是唯一强制点）。这里负责的是**形状**：
把行组装成页面/服务要的东西，并决定"取不到时怎么办"。

判据（ADR-0005）：*它影响数据内容，还是只影响数据形状？* 影响内容 → service；
只影响形状 → pages。所以"该不该 404"属于内容侧，放这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.bank import repository
from app.db.models import Question
from app.errors import NotFound

#: 题库列表每页条数。分页不是装饰（AGENTS.md §3.6）。
PAGE_SIZE = 20


@dataclass
class QuestionCard:
    """列表里的一行。**只有列表要用的字段** —— 不把整行（含题干）塞进列表。"""

    id: int
    kind: str
    stem: str
    difficulty: int
    point_name: str
    is_private: bool
    answer_tier: str | None = None


@dataclass
class QuestionDetail:
    """一道题的详情（含**该考什么**）。"""

    question: Question
    point_name: str
    criteria: list[str] = field(default_factory=list)
    related_point_ids: list[int] = field(default_factory=list)


def to_card(q: Question, point_name: str) -> QuestionCard:
    """把一行题变成列表卡片。

    公开（无下划线）是因为 `favorites.py` 也要它 —— 收藏夹列的是同一种卡片，
    抄一份"列表行长什么样"就是第二个会漂的真相。
    """
    return QuestionCard(
        id=q.id,
        kind=q.kind,
        stem=q.stem,
        difficulty=q.difficulty,
        point_name=point_name,
        is_private=q.owner_user_id is not None,
        answer_tier=q.answer_tier,
    )


def browse(
    session: Session,
    viewer_id: int | None,
    *,
    kind: str | None = None,
    point_id: int | None = None,
    page: int = 1,
) -> tuple[list[QuestionCard], int]:
    """题库浏览（**不消耗额度点** —— 决策 13）。返回 `(卡片, 总条数)`。"""
    page = max(1, page)
    rows, total = repository.list_questions(
        session,
        viewer_id,
        kind=kind,
        point_id=point_id,
        offset=(page - 1) * PAGE_SIZE,
        limit=PAGE_SIZE,
    )
    names = repository.point_names(
        session, {q.primary_point_id for q in rows if q.primary_point_id is not None}
    )
    cards = [to_card(q, names.get(q.primary_point_id or -1, "")) for q in rows]
    return cards, total


def detail(session: Session, question_id: int, viewer_id: int | None) -> QuestionDetail:
    """看一道题。不可见或不存在都抛 `NotFound`（**不区分**，那会泄露存在性）。"""
    question = repository.find_question(session, question_id, viewer_id)
    if question is None:
        raise NotFound(f"题目 {question_id} 不存在或不可见")

    names = repository.point_names(
        session, {question.primary_point_id} if question.primary_point_id else set()
    )
    return QuestionDetail(
        question=question,
        point_name=names.get(question.primary_point_id or -1, ""),
        criteria=[c.text for c in repository.criteria_of_question(session, question)],
        related_point_ids=repository.related_points(session, question_id),
    )
