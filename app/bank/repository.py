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

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from app.db.models import Criterion, KnowledgePoint, Question, QuestionPoint, UserFavorite

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
    point_ids: list[int] | None = None,
    owner_only: bool = False,
    exclude_ids: list[int] | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Question], int]:
    """题库列表（分页）。返回 `(当页题目, 总数)`。

    分页不是装饰：目标是 2C2G（AGENTS.md §3.6「不许全量加载大表」）。

    `point_ids` / `exclude_ids` / `owner_only` 是给**首页推荐**与**我的私有题集**
    用的三个筛选项 —— 它们都只是往 `visible_to()` 后面接 `.where()`，所以推荐
    永远捞不出看不见的题（这里没有第二条查询路径）。
    """
    stmt = visible_to(viewer_id)
    if kind:
        stmt = stmt.where(Question.kind == kind)
    if point_id is not None:
        stmt = stmt.where(Question.primary_point_id == point_id)
    if point_ids is not None:
        # 空列表要显式处理：`IN ()` 在 SQL 里是语法错误，而"一道薄弱点都没有"很正常
        if not point_ids:
            return [], 0
        stmt = stmt.where(Question.primary_point_id.in_(point_ids))
    if owner_only:
        if viewer_id is None:
            return [], 0
        stmt = stmt.where(Question.owner_user_id == viewer_id)
    if exclude_ids:
        stmt = stmt.where(Question.id.not_in(exclude_ids))

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
    return criteria_of_point(session, question.primary_point_id)


def criteria_of_point(session: Session, point_id: int) -> list[Criterion]:
    """某个知识点下的考察点（按 `seq` 排）。

    **给"还没建出题的"调用方**用（如补题管道：它要按知识点的判据出题）——
    与 `criteria_of_question` 是同一个查询，只是入口不同（那边先看题有没有主知识点）。
    """
    return list(
        session.execute(
            select(Criterion).where(Criterion.point_id == point_id).order_by(Criterion.seq)
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


def unmounted_public(session: Session) -> list[Question]:
    """还没挂载到知识点的**公共题** —— 给知识层管道用（提候选 / 挂载的输入）。

    为什么它也在仓储里：管道是写入方，但它同样不该自己 `select(Question)` ——
    题目查询只有一条通道这件事，靠"每次都觉得这次是例外"是守不住的
    （这条规则已经抓到过三次违规）。
    """
    return list(
        session.execute(
            select(Question)
            .where(Question.owner_user_id.is_(None), Question.primary_point_id.is_(None))
            .order_by(Question.id)
        )
        .scalars()
        .all()
    )


def unmounted_public_count(session: Session) -> int:
    """待定池有多大。**只数不取行** —— 库里几千道题时，`len(unmounted_public())`
    会把它们全拉进内存（AGENTS.md §3.6），而这个数字只用来显示。"""
    return int(
        session.execute(
            select(func.count())
            .select_from(Question)
            .where(Question.owner_user_id.is_(None), Question.primary_point_id.is_(None))
        ).scalar_one()
    )


def public_questions(session: Session) -> list[Question]:
    """全部**公共题**（不论挂没挂）—— 给知识层管道的提候选步骤用。"""
    return list(
        session.execute(
            select(Question).where(Question.owner_user_id.is_(None)).order_by(Question.id)
        )
        .scalars()
        .all()
    )


def public_visible_questions(
    session: Session, *, exclude_id: int | None = None
) -> list[Question]:
    """**真正在公共池里**的题（`visibility='public'`，不含 pending / hidden）。

    给两类**写入方**用（都不面向某个人的浏览）：

    · 晋升门禁的去重检查（"公共题库里有没有同一道题"）—— 用 `find_same_stem`，别用这个
    · 事后治理的重复检测（扫一遍公共题找同知识点的重复）—— 它是一次性的离线任务

    ⚠️ **它会一次把公共题全取回来**（几千行）。所以它只适合**离线任务**：
    面向请求的路径要用 `find_same_stem`（只取两列 + 提前退出，见下）。
    """
    stmt = select(Question).where(
        Question.owner_user_id.is_(None), Question.visibility == PUBLIC_VISIBILITY
    )
    if exclude_id is not None:
        stmt = stmt.where(Question.id != exclude_id)
    return list(session.execute(stmt.order_by(Question.id)).scalars().all())


def find_same_stem(
    session: Session,
    *,
    normalize,
    stem: str,
    exclude_id: int | None = None,
) -> int | None:
    """在公共池里找一道**归一化后题干相同**的题，返回它的 id（没有就 `None`）。

    ## 它为什么只取两列、以及为什么没有"分批流式"

    它挂在 `POST /bank/{id}/promote` 上（**面向请求**），而公共题有几千道。这里能做
    的两件事是：**只取两列**（`id` + `stem`，不是整行）与**提前退出**（找到就返回）。
    量级：三千道题 ≈ 0.2MB 的短字符串 —— 不是 §3.6 说的那种"每请求 O(N) 内存"的
    定时炸弹（那条讲的是把全表 embedding 拉成矩阵）。

    ⚠️ **不要写 `yield_per` 来"分批流式"**：SQLite 的驱动（pysqlite）本来就把结果集
    全缓冲在客户端游标里，`yield_per` 在这里**不减少内存** —— 加上它只会让读者以为
    内存是常数。真要省，得让 SQL 自己比：给 `questions` 加一列归一化题干（写入时
    维护 + 索引），那时这是一次索引查询。**现在不值得**：多一列就多一条不变量
    （每个写题干的路径都得维护它），而收益只是几千行短字符串的扫描。

    为什么不在 SQL 里复现归一化：那是 Python 侧的一套规则（标点表），写两遍必漂。
    """
    stmt = select(Question.id, Question.stem).where(
        Question.owner_user_id.is_(None), Question.visibility == PUBLIC_VISIBILITY
    )
    if exclude_id is not None:
        stmt = stmt.where(Question.id != exclude_id)
    for question_id, other_stem in session.execute(stmt.order_by(Question.id)):
        if normalize(other_stem) == stem:
            return int(question_id)
    return None


def point_question_counts(session: Session, point_ids: list[int]) -> dict[int, int]:
    """每个知识点下有多少道**公共**题（一次查询，不在循环里查）。

    给"这个知识点还缺题吗"用（离线生成管道的入口）。它只数不取行 —— 但要说清它数的
    是**公共题**：私有题再多也不该让一个知识点看起来"已经有题了"。
    """
    if not point_ids:
        return {}
    rows = session.execute(
        select(Question.primary_point_id, func.count())
        .where(
            Question.owner_user_id.is_(None),
            Question.primary_point_id.in_(point_ids),
        )
        .group_by(Question.primary_point_id)
    ).all()
    return {int(pid): int(n) for pid, n in rows}


def difficulty_counts(session: Session) -> dict[int, int]:
    """公共题按难度 1-5 的计数（一次查询，不取行）。

    给两个读者用：观测页的难度分布，以及 `offline.calibration` 的阈值标定
    （§未决 6 里"难度 1-5 的分布"那条 —— v1 的实测结论是**中间堆积、两端稀疏**）。
    它和 `point_question_counts` 一样只数不取 —— 标定工具会在大库上跑，不该为了
    一张直方图把整表拉进内存（AGENTS §3.6）。
    """
    rows = session.execute(
        select(Question.difficulty, func.count())
        .where(Question.owner_user_id.is_(None))
        .group_by(Question.difficulty)
    ).all()
    return {int(level): int(n) for level, n in rows}


def owned_ids(session: Session, user_id: int) -> list[int]:
    """这位用户**自己**的题 id 清单（**不带可见性条件**）。

    给两类**写入方**用，都不面向浏览：
    · 注销：删人之前要先清掉指向这些题的外键（`question_point_stats` / 收藏）
    · 晋升：把 `owner_user_id` 置空时按 id 操作

    公开面（列表 / 详情 / 抽题）仍必须走 `visible_to()`。
    """
    return list(
        session.execute(
            select(Question.id).where(Question.owner_user_id == user_id)
        ).scalars()
    )


def owned_questions(session: Session, user_id: int) -> list[Question]:
    """这位用户的私有题（含全部字段）—— 给"导出我的数据"用（决策 23）。

    它返回 ORM 行而不是自己拼 dict：导出要**全部字段**，而字段清单的唯一来源是
    映射本身（`_Row.to_dict()`），手抄一份必然随加列而漏。
    """
    return list(
        session.execute(
            select(Question).where(Question.owner_user_id == user_id).order_by(Question.id)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# 收藏夹（决策 63）
# ---------------------------------------------------------------------------
# 为什么这四个函数在**仓储**里而不是 `favorites.py` 里：收藏夹指向的是题，
# 而"题怎么查"只有这一条通道（AGENTS.md §3.5）。把 `select(UserFavorite)` 留在
# 领域服务里会在结构测试的豁免边上打洞 —— 那条测试抓的正是"题目相关的查询绕开了
# `visible_to()`"。这里每个返回题的函数都从 `visible_to()` 起手。
def list_favorites(
    session: Session, user_id: int, *, offset: int = 0, limit: int = 20
) -> tuple[list[Question], int]:
    """这位用户收藏的题（分页）。**仍然要过可见性**。

    收藏只是一根指针：题被标 `hidden` 或换成了别人的私有题之后，收藏夹不能再把它
    露出来（`visible_to()` 的注释里点了这个场景 —— 宁可"收藏里少一行"）。
    悬空行（题已被删）连 join 都过不去，自然也不会出现。
    """
    ids = select(UserFavorite.question_id).where(UserFavorite.user_id == user_id)
    stmt = visible_to(user_id).where(Question.id.in_(ids))
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(Question.id.desc()).offset(offset).limit(limit)
    ).scalars().all()
    return list(rows), int(total)


def favorited_ids(session: Session, user_id: int, question_ids: list[int]) -> set[int]:
    """这批题里哪些已被收藏 —— **列表页一次问完**，不在循环里逐题查（v1 的 N+1）。"""
    if not question_ids:
        return set()
    rows = session.execute(
        select(UserFavorite.question_id).where(
            UserFavorite.user_id == user_id,
            UserFavorite.question_id.in_(question_ids),
        )
    ).all()
    return {r[0] for r in rows}


def favorite_count(session: Session, user_id: int) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(UserFavorite).where(UserFavorite.user_id == user_id)
        ).scalar_one()
    )


def find_favorite(session: Session, user_id: int, question_id: int) -> UserFavorite | None:
    return session.execute(
        select(UserFavorite).where(
            UserFavorite.user_id == user_id, UserFavorite.question_id == question_id
        )
    ).scalar_one_or_none()


def add_favorite(session: Session, user_id: int, question_id: int) -> UserFavorite:
    row = UserFavorite(user_id=user_id, question_id=question_id)
    session.add(row)
    session.flush()
    return row


def remove_favorite(session: Session, user_id: int, question_id: int) -> bool:
    """取消收藏。返回"本来有没有" —— 界面要用它区分"取消成功"与"本来就没收藏"。

    ⚠️ 收藏是**用户自己的指针**，所以取消时**不带可见性条件**：题被隐藏之后，
    用户仍然应该能把自己的那根指针清掉（否则"收藏里那一行"会永远挂着却点不进去）。
    """
    result = session.execute(
        delete(UserFavorite).where(
            UserFavorite.user_id == user_id, UserFavorite.question_id == question_id
        )
    )
    session.flush()
    return bool(result.rowcount)
