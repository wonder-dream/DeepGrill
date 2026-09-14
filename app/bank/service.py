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
    owner_only: bool = False,
    page: int = 1,
) -> tuple[list[QuestionCard], int]:
    """题库浏览（**不消耗额度点** —— 决策 13）。返回 `(卡片, 总条数)`。

    `owner_only=True` 是「我的私有题集」那个入口（首页三个入口之一）——
    它和浏览走**同一个**函数，因为"我的私有题集"就是"题库里属于我的那一部分"，
    另写一条查询路径只会多一处能漏掉可见性过滤的地方。
    """
    page = max(1, page)
    rows, total = repository.list_questions(
        session,
        viewer_id,
        kind=kind,
        point_id=point_id,
        owner_only=owner_only,
        offset=(page - 1) * PAGE_SIZE,
        limit=PAGE_SIZE,
    )
    names = repository.point_names(
        session, {q.primary_point_id for q in rows if q.primary_point_id is not None}
    )
    cards = [to_card(q, names.get(q.primary_point_id or -1, "")) for q in rows]
    return cards, total


#: 首页「今日推荐题」的条数。它是**即时计算**的，不是每日锁定的题单
#: （`docs/v2范围基线.md` 里点明了：`user_picks` / `questions.selected_at` 都已删掉，
#: 换成别的推荐算法时不会留下数据残骸）。
RECOMMEND_COUNT = 5


def recommend(
    session: Session,
    viewer_id: int,
    *,
    weak_point_ids: list[int],
    exclude_ids: set[int] | None = None,
    limit: int = RECOMMEND_COUNT,
) -> list[QuestionCard]:
    """按**薄弱知识点**推荐题目 —— 决策 3 的「今日推荐题」。

    ⚠️ 它**不自己算薄弱点**：那要读 `question_point_stats` / `attempts`，属于
    `knowledge` 领域（ADR-0005：领域互不 import）。掌握度由调用方（首页装配）
    算好、把**知识点 id** 递进来，这里只负责"在这些知识点下挑看得见的题"。

    `exclude_ids` 是"已经答过的题"，同样由调用方给 —— 判断"答过没答过"要看面试
    记录，那不是题库的事（`web/home_page.py` 把两边的结果拼起来）。

    一条都不命中时返回 `[]`（而不是抛异常）：首页要显示"还没有测出薄弱点"，
    那不是错误。
    """
    if not weak_point_ids:
        return []
    rows, _ = repository.list_questions(
        session,
        viewer_id,
        point_ids=list(weak_point_ids),
        exclude_ids=sorted(exclude_ids) if exclude_ids else None,
        limit=limit,
    )
    names = repository.point_names(
        session, {q.primary_point_id for q in rows if q.primary_point_id is not None}
    )
    return [to_card(q, names.get(q.primary_point_id or -1, "")) for q in rows]


def latest(session: Session, viewer_id: int | None, *, limit: int = RECOMMEND_COUNT) -> list[QuestionCard]:
    """最新几道可见的题 —— 首页在"还没有测出薄弱点"时的兜底。

    兜底也要**说清楚自己是兜底**（页面上写"还没测出薄弱点，先看这几道"），
    否则它会看起来像一条认真的推荐。
    """
    return browse(session, viewer_id, page=1)[0][:limit]


# ---------------------------------------------------------------------------
# 新增公共题（**写题目表只有 bank 自己** —— ADR-0005）
# ---------------------------------------------------------------------------
def author_public_question(
    session: Session,
    *,
    point_id: int,
    stem: str,
    kind: str,
    difficulty: int,
    reference_answer: str = "",
    answer_tier: str | None = None,
) -> Question:
    """由**系统**（离线生成管道）新建一道公共题。

    为什么这个函数住在 `bank` 而不是离线管道里：ADR-0005 的原话是"写题目表的只有
    `bank` 自己"。离线管道（跨领域编排）可以调它，但不该自己 `session.add(Question)`。

    ⚠️ `primary_point_id` **必填**：公共题必须有主知识点（基线：每道题必须有一个主
    知识点）—— 而"给某个知识点补题"这条路的入口本来就是那个知识点。
    考察点不在这里建：判分读的是 `criteria` 表里**该知识点**的判据，生成题不产生新判据。
    """
    question = Question(
        kind=kind,
        stem=stem,
        difficulty=difficulty,
        primary_point_id=point_id,
        origin="generated",
        visibility="public",
        owner_user_id=None,
        answer_tier=answer_tier or ("common" if reference_answer else "long_tail"),
        reference_answer=reference_answer or None,
    )
    session.add(question)
    session.flush()
    return question


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
