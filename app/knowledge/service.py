"""掌握度的聚合（**决策 24 的结论，全项目最容易算错的一处**）。

```
每一格 = 该知识点下「被考过的考察点」里，候选人答到了多少
        （不是分数，也不是题量）
```

三条推论决定了这个文件的形状（`docs/v2范围基线.md` §掌握度的定义）：

1. **「没考过」是空格，「考了但没答」是 0%** —— 两者含义完全不同。所以分母是
   **被考过的**考察点（命中 + 未命中），而 `未涉及` 必须被排除。
2. **一道综合题会同时填充多个格子** —— 所以掌握度**只由命中记录推导**，
   与"这道题挂在哪个节点下"无关（`CONTEXT.md` 的「主知识点」词条）。
3. **不需要给「题目 ↔ 知识点」加权** —— 格子装命中率而非分数，权重问题被消解。

## 为什么靠 `criteria` 表把考察点归到知识点

`attempts.hits` 是 `criterion_id → 状态` 的映射，而 `criteria.point_id` 就是
"这条考察点属于哪个知识点"。所以"填充多个格子"是**天然**的：一条 hit 记录
落在哪个格子，由它自己属于哪个知识点决定 —— 与题目挂在哪个节点下无关。
这也正是决策 24 要的效果（综合题牵动的知识点不再被系统性低估）。

## 口径：命中率按"考察点去重"

同一道题的同一考察点在同一个知识点上只算一次。理由：重复考同一个考察点不该
把它在分母里放大，否则"多刷几遍同一道题"会拉低掌握度，读起来像惩罚练习。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Attempt,
    Criterion,
    Interview,
    KnowledgePoint,
    Session_ as InterviewSession,
)
from app.interview import rules


@dataclass
class MasteryCell:
    """一个知识点在矩阵里的一格。"""

    point_id: int
    point_name: str
    covered: int  # 被考过的考察点数（分母）
    hit: int      # 其中答到的（分子）

    @property
    def rate(self) -> float | None:
        """`None` = **没考过（空格）**，不是 0。

        这个区分是产品要求（基线里写明），所以它必须体现在**返回类型**上 ——
        返回 0 会让"没考过"与"考了全错"在同一格里显示，而两者对候选人的含义
        完全不同。
        """
        if self.covered == 0:
            return None
        return self.hit / self.covered

    @property
    def percent(self) -> str:
        r = self.rate
        return "—" if r is None else f"{round(r * 100)}%"

    @property
    def missed(self) -> int:
        return self.covered - self.hit


@dataclass
class MasteryMatrix:
    cells: list[MasteryCell] = field(default_factory=list)

    def by_point(self) -> dict[int, MasteryCell]:
        return {c.point_id: c for c in self.cells}

    def weak_points(self, *, limit: int = 5) -> list[MasteryCell]:
        """**薄弱点** = 答不到的考察点最多的那些知识点（`CONTEXT.md`「薄弱点」）。

        只算**被考过**的格子：没考过的不是"薄弱"，是"还没测到"。

        排序用**未命中条数**而非命中率，两个键的次序是有意的：一个只被考过 1 条、
        答错 1 条的知识点命中率是 0%，但它不该排在"考了 3 条答对 1 条"的前面 ——
        后者才是真的弱。命中率只用来打破并列。
        """
        measured = [c for c in self.cells if c.covered > 0 and c.missed > 0]
        measured.sort(key=lambda c: (c.missed, c.rate or 0.0), reverse=True)
        return measured[:limit]


def mastery_matrix(
    session: Session, user_id: int, *, exclude_interview_ids: set[int] | None = None
) -> MasteryMatrix:
    """某位候选人的掌握度矩阵。**个人状态页与面试报告共用它**（决策 17）。

    `exclude_interview_ids` 用来算"**这一场之前**"的快照 —— 报告要显示矩阵的
    **变化**（②），而"之前"这个时点事后无法重建（ADR-0003：主体必须在 finish 时算）。
    实现上是"用同一套聚合、只是少算几场"，而不是维护一份历史副本 —— 历史副本
    会随用户注销而失去意义，而重算是确定的。

    两条查询，都不随数据量爆炸：

    ① **全部知识点**（几十到几百行 —— 它必须全取，否则"没考过"的格子就不存在，
       而产品要求它显示为**空格**）
    ② **该用户全部轮次的 hits**（行数 = 轮次数，而不是全表）
    """
    points = session.execute(select(KnowledgePoint.id, KnowledgePoint.name)).all()
    if not points:
        return MasteryMatrix()

    criterion_point = dict(
        session.execute(select(Criterion.id, Criterion.point_id)).all()
    )

    covered: dict[int, set[int]] = {}
    hits: dict[int, set[int]] = {}
    for raw in _hit_blobs_for_user(session, user_id, exclude_interview_ids=exclude_interview_ids):
        for cid, status in _statuses(raw).items():
            point_id = criterion_point.get(cid)
            if point_id is None:
                # 考察点被删了、而历史 hits 还留着它 —— 不是错误，是 ADR-0002 说的
                # "知识点定义会随人审而变"。跳过它，别让整个矩阵因此炸掉。
                continue
            if status == rules.NOT_COVERED:
                continue  # ← 「没考过」不进分母。整个算法的关键一行。
            covered.setdefault(point_id, set()).add(cid)
            if status == rules.HIT:
                hits.setdefault(point_id, set()).add(cid)

    return MasteryMatrix(
        cells=[
            MasteryCell(
                point_id=pid,
                point_name=name,
                covered=len(covered.get(pid, set())),
                hit=len(hits.get(pid, set())),
            )
            for pid, name in points
        ]
    )


def _hit_blobs_for_user(
    session: Session, user_id: int, *, exclude_interview_ids: set[int] | None = None
) -> list[object]:
    """该用户全部轮次的 `hits` 列值（**JsonText 已反序列化，所以是 dict**）。

    v1 的 `review_tags` 是把全表拉回 Python 做 `Counter`（快照 §6-P5 点名的
    N+1/全表加载之一）。这里只取需要的**一列**，且按 user 过滤。
    """
    stmt = (
        select(Attempt.hits)
        .join(InterviewSession, InterviewSession.id == Attempt.session_id)
        .join(Interview, Interview.id == InterviewSession.interview_id)
        .where(Interview.user_id == user_id)
    )
    if exclude_interview_ids:
        stmt = stmt.where(Interview.id.not_in(sorted(exclude_interview_ids)))
    return list(session.execute(stmt).scalars().all())


def prerequisite_gaps(session: Session, point_ids: set[int], *, limit: int = 5) -> list[str]:
    """**该补的前置知识点** —— 沿 `knowledge_point_edges` 往前走一跳。

    这是 ADR-0003 的 ④：不调 LLM，纯查图。只走一跳是有意的（决策 48：前置边
    是全链最弱一环，"限深 ≤2" 是对幻觉的防御，而复习推荐只需要 1-2 跳）。
    """
    from app.db.models import KnowledgePointEdge

    if not point_ids:
        return []
    rows = session.execute(
        select(KnowledgePoint.name)
        .join(KnowledgePointEdge, KnowledgePointEdge.from_point_id == KnowledgePoint.id)
        .where(KnowledgePointEdge.to_point_id.in_(sorted(point_ids)))
        .distinct()
        .limit(limit)
    ).all()
    return [r[0] for r in rows]


def _statuses(raw: str | dict | None) -> dict[int, str]:
    """把 `hits` 解析成 `criterion_id → 状态`。

    ⚠️ **它同时接受 dict 与 JSON 字符串**，这不是防御性编程而是必需：
    `Attempt.hits` 是 `JsonText` 列，走 ORM / 列查询时 SQLAlchemy 已经把它反
    序列化成 **dict**；而裸 `text()` 查询拿到的是字符串。第一版只处理字符串，
    于是 `json.loads(dict)` 抛 TypeError 被吞掉、静默返回空 —— 表现为"整个矩阵
    全是空格"，一个不会报错的错。

    损坏的 JSON 不该让整个矩阵崩掉：跳过它（`attempts` 是我们自己的数据，
    真坏了要能在页面上看见"少了一场"，而不是 500）。
    """
    data: object = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, str] = {}
    for key, value in data.items():
        try:
            out[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return out
