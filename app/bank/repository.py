"""题库领域的数据访问（`bank` —— CONTEXT.md：**写题目表的只有它**）。

这个文件存在的唯一理由是**一条硬规则的强制点**（ADR-0005）：

> `questions` 同时装公共题库与所有人的私有题集。**任何题目查询都必须过滤
> `owner_user_id`**；业务代码不许直接 `select(Question)`。

所以路由、service、页面都不碰 `questions` 表 —— 只能调这里的函数。这样"漏一次
过滤就是数据泄露"（AGENTS.md §3.5）从"靠自觉"变成"没有别的路可走"。

## 可见性的确切规则

`visible_question_filter(viewer_id)` 是**唯一**定义"谁能看到哪道题"的地方：

| 情形 | 可见 |
|---|---|
| 公共题（`owner_user_id IS NULL`）且 `visibility='public'` | 所有人 |
| 自己的私有题（`owner_user_id = 我`） | 只有我（**不管 visibility**） |
| 别人的私有题 | 永不可见 |
| `visibility='hidden'`（质量信号差） | 不出现在任何浏览/抽题里 |
| `visibility='pending'`（晋升待门禁） | 不出现在公共浏览里 |

⚠️ **`hidden` 不进浏览**是有意的：它是"质量信号差"的标记，而把一个已知有问题的
题继续摆在题库里给人练，比"收藏夹/列表里少一行"坏得多。作者仍能通过自己的私有
题看到它（第二行）—— 否则就成了"题消失且没人知道为什么"。
"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.models import Criterion, KnowledgePoint, Question, QuestionPoint

#: 公共列表只认这一种可见性。`pending`（晋升待门禁）与 `hidden` 都不算公共。
PUBLIC_VISIBILITY = "public"


def visible_to(viewer_id: int | None) -> Select[tuple[Question]]:
    """**所有题目查询的起点**。返回一个已带可见性条件的 `SELECT`。

    调用方只许在它后面继续 `.where(...)` —— 不允许从 `select(Question)` 起手。
    """
    cond = (Question.owner_user_id.is_(None)) & (Question.visibility == PUBLIC_VISIBILITY)
    if viewer_id is not None:
        # 自己的题：不论 visibility 都能看到（含被标 hidden 的）
        cond = cond | (Question.owner_user_id == viewer_id)
    return select(Question).where(cond)


def find_question(session: Session, question_id: int, viewer_id: int | None):
    """取一道**对这位查看者可见**的题；不可见时返回 None。

    注意它不区分"不存在"与"不该看" —— 两者都返回 None。调用方一律按 404 处理：
    区分它们会把"某道题存在"这条信息泄露给不该看到它的人。
    """
    return session.execute(visible_to(viewer_id).where(Question.id == question_id)).scalar_one_or_none()


def list_questions(
    session: Session,
    viewer_id: int | None,
    *,
    kind: str | None = None,
    point_id: int | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Question], int]:
    """题库列表（分页）。返回 `(当页题目, 总数)`。

    分页不是装饰：目标是 2C2G（AGENTS.md §3.6「不许全量加载大表」）。
    """
    stmt = visible_to(viewer_id)
    if kind:
        stmt = stmt.where(Question.kind == kind)
    if point_id is not None:
        stmt = stmt.where(Question.primary_point_id == point_id)

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    rows = session.execute(
        stmt.order_by(Question.id.desc()).offset(offset).limit(limit)
    ).scalars().all()
    return list(rows), int(total)


def criteria_of_question(session: Session, question: Question) -> list[Criterion]:
    """这道题**该考什么** —— 读 `criteria` 表，不读 `questions.good_criteria`。

    这是决策 49 的硬规则：`good_criteria` 是**离线素材**（聚类输入），运行时判分、
    追问、掌握度只准读 `criteria`。违反它不会报错，只会让判分悄悄用上一份没人再
    维护的旧标准。
    """
    if question.primary_point_id is None:
        return []
    return list(
        session.execute(
            select(Criterion)
            .where(Criterion.point_id == question.primary_point_id)
            .order_by(Criterion.seq)
        )
        .scalars()
        .all()
    )


def point_names(session: Session, point_ids: set[int]) -> dict[int, str]:
    """批量取知识点名 —— 一次查询，不在循环里查（v1 有一堆 N+1，见快照 §6-P5）。"""
    if not point_ids:
        return {}
    rows = session.execute(
        select(KnowledgePoint.id, KnowledgePoint.name).where(
            KnowledgePoint.id.in_(sorted(point_ids))
        )
    ).all()
    return {r[0]: r[1] for r in rows}


def related_points(session: Session, question_id: int) -> list[int]:
    """这道题**还牵动**哪些知识点（决策 24，用于组卷与覆盖检查）。"""
    rows = session.execute(
        select(QuestionPoint.point_id).where(QuestionPoint.question_id == question_id)
    ).all()
    return [r[0] for r in rows]


def stem_exists(session: Session, stem: str) -> bool:
    """题干是否已存在（**不带可见性条件**）。

    给幂等脚本用的（种子数据、导入工具）：它们问的是"这行数据在不在"，不是
    "我能不能看到它" —— 所以它**故意**不经过 `visible_to()`。

    ⚠️ 它只能被**写入方**（`bank` 自己或编排层的一次性脚本）调用。**面向用户的
    查询一律不许用它** —— 那正是这条规则要拦的东西（AGENTS.md §3.5）。
    之所以放在这里而不是让调用方自己 `select(Question)`：题目查询只有这一条
    通道这件事，比"少写一个函数"重要（本项目刚被自己的结构测试抓到一次）。
    """
    return (
        session.execute(select(Question.id).where(Question.stem == stem).limit(1)).first()
        is not None
    )
