"""收藏夹（决策 63）—— 用户自己的指针，题本身不动。

决策 63 的原话：收藏是**用户自己的指针**（`user_favorites`），**动作幂等**
（`UNIQUE(user_id, question_id)`）、**不消耗额度点**。它几乎零成本，补上的却是
"回头再练这道题"这个真实动作。

三件在这里定下来的事：

① **收藏一道看不见的题要被拒**，而且与"这道题不存在"**同一种回应**。否则
   `POST /bank/1234/favorite` 会变成一个探测接口：345 号题存不存在，看返回码就知道。
   判据用 `repository.find_question(..., viewer_id=我)` —— 也就是**同一条可见性
   规则**，不另写一套。

② **幂等由两条一起保证**：库里 `UNIQUE(user_id, question_id)`（并发的第二下会撞
   约束），以及这里先查一次（正常路径下的第二下不该产生一次数据库异常）。
   收藏按钮会被人连点，这不是边界情况。

③ **取消收藏不带可见性条件**（见 `repository.remove_favorite`）：题被标 `hidden`
   之后用户仍然要能清掉自己那根指针，否则收藏夹里会留一行点不进去的东西。

配额：**不消耗额度点** —— 它没有任何 LLM 调用，`COST` 表里没有它，这里也不能
凭空扣。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.bank import repository, service
from app.bank.service import QuestionCard, to_card
from app.errors import NotFound

#: 收藏夹也是列表，也要分页（AGENTS.md §3.6：2C2G 上不许全量加载）。
PAGE_SIZE = 20


def _card(session: Session, rows: list) -> list[QuestionCard]:
    names = repository.point_names(
        session, {q.primary_point_id for q in rows if q.primary_point_id is not None}
    )
    return [to_card(q, names.get(q.primary_point_id or -1, "")) for q in rows]


def add(session: Session, *, user_id: int, question_id: int) -> bool:
    """收藏。返回"这次是不是真的新增了"（重复点 → False，**不报错**）。"""
    if repository.find_question(session, question_id, user_id) is None:
        # 不可见与不存在同一种回应（见模块头 ①）
        raise NotFound(f"题目 {question_id} 不存在或不可见")
    if repository.find_favorite(session, user_id, question_id) is not None:
        return False
    repository.add_favorite(session, user_id, question_id)
    return True


def remove(session: Session, *, user_id: int, question_id: int) -> bool:
    """取消收藏。返回"本来有没有"（本来就没有 → False，也不报错）。"""
    return repository.remove_favorite(session, user_id, question_id)


def is_favorited(session: Session, *, user_id: int, question_id: int) -> bool:
    return repository.find_favorite(session, user_id, question_id) is not None


def favorited_ids(session: Session, *, user_id: int, question_ids: list[int]) -> set[int]:
    """列表页批量标记 —— 一次查询问完一页，不逐题查。"""
    return repository.favorited_ids(session, user_id, question_ids)


def count(session: Session, *, user_id: int) -> int:
    return repository.favorite_count(session, user_id)


def browse(
    session: Session, *, user_id: int, page: int = 1
) -> tuple[list[QuestionCard], int]:
    """我的收藏（分页）。返回 `(卡片, 总条数)`。

    `offset_for`（而不是自己算）是刻意的：越界页码必须变成 400，而不是让
    SQLite 的参数绑定抛 `OverflowError` → 500（见 `service.MAX_OFFSET`）。
    """
    page = max(1, page)
    rows, total = repository.list_favorites(
        session, user_id, offset=service.offset_for(page, page_size=PAGE_SIZE), limit=PAGE_SIZE
    )
    return _card(session, rows), total
