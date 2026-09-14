"""题目反馈：用户报问题 → 管理员处理（v1 的工单形态，§未决 12 定案）。

**它住在 `bank` 领域**：反馈说的是"这道题有问题"，所以它天然属于题库的内容治理。
`CONTEXT.md` 把题库的质量责任定为"靠反馈闭环承担"，这就是那个闭环。

## 两条来自 v1 的契约（这次恢复回来）

① **`status` 三态**（open / resolved / dismissed）—— 没有它，用户报的问题会沉进表里：
   管理员既看不到"哪些还没处理"，也无法标记"已处理"。**只有入口没有出口**。
② **`uq_feedback_open` 部分唯一索引**（`0002` 迁移）—— 同一时刻同一人对同一题
   只该有一条待处理反馈。注意它是**部分**索引：旧反馈被处理之后可以再报一次
   （问题没修好、或者又发现了新问题）。
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.db.models import Question, QuestionFeedback
from app.errors import InvalidInput, NotFound

logger = logging.getLogger(__name__)

#: v1 的五类（`docs/v2数据模型.md` 沿用）。kind 的合法值同时在 DB 的 CHECK 里 ——
#: 两处都要有：这里给用户一句人话，那里保证不合法值进不去。
KINDS = ("wrong", "unclear", "duplicate", "not_interview", "other")

KIND_LABELS = {
    "wrong": "答案或评分标准有错",
    "unclear": "题干表述不清楚",
    "duplicate": "和别的知识点重复",
    "not_interview": "这题不适合面试",
    "other": "其他问题",
}


def submit(
    session: Session,
    *,
    question_id: int,
    user_id: int | None,
    kind: str,
    detail: str = "",
    duplicate_point_ids: list[int] | None = None,
) -> QuestionFeedback:
    """提交一条反馈。

    三件事必须做对：

    ① **题必须对这位用户可见** —— 走 `bank/repository.find_question()`，
       否则"这道题存在吗"会变成一个可探测的接口（AGENTS.md §3.5）
    ② **`duplicate` 指向知识点而不是题**（v1 的 `duplicate_question_ids` 语义已改）：
       用户说的是"这题和我见过的那个**知识点**重复"
    ③ **重复提交靠数据库约束**（`uq_feedback_open`），不靠这里先查一遍 ——
       先查再写在并发下会漏（两个请求同时查到"没有"）
    """
    if kind not in KINDS:
        raise InvalidInput(f"反馈类型不合法：{kind}")
    if user_id is not None and bank_repository.find_question(session, question_id, user_id) is None:
        # 不可见与不存在都给同一个回应 —— 不泄露存在性
        raise NotFound("这道题不存在或不可见")

    feedback = QuestionFeedback(
        question_id=question_id,
        user_id=user_id,
        kind=kind,
        detail=(detail or "").strip() or None,
        duplicate_question_ids=list(duplicate_point_ids or []),
        status="open",
    )
    session.add(feedback)
    try:
        session.flush()
    except Exception as exc:  # noqa: BLE001
        # 唯一约束说"你已经报过这条了"。**说清楚**，而不是给用户一个 500。
        if "uq_feedback_open" in str(exc) or "UNIQUE" in str(exc).upper():
            raise InvalidInput("你已经报过这道题了，管理员处理完之后可以再报") from exc
        raise
    return feedback


def my_feedback(session: Session, user_id: int, *, limit: int = 50) -> list[QuestionFeedback]:
    """我看过的反馈（含状态）—— 用户要知道自己报的有没有被处理。"""
    return list(
        session.execute(
            select(QuestionFeedback)
            .where(QuestionFeedback.user_id == user_id)
            .order_by(QuestionFeedback.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def open_feedback(session: Session, *, limit: int = 100) -> list[QuestionFeedback]:
    """待处理的反馈（管理员看的队列）。"""
    return list(
        session.execute(
            select(QuestionFeedback)
            .where(QuestionFeedback.status == "open")
            .order_by(QuestionFeedback.id)
            .limit(limit)
        )
        .scalars()
        .all()
    )


def resolve(session: Session, *, feedback_id: int, action: str, note: str = "") -> QuestionFeedback:
    """处理一条反馈：`resolved` 或 `dismissed`（v1 的两个动作）。

    ⚠️ v1 的 `resolve` 与 `dismiss` 两个 handler **逐行相同**（快照 §6-P5 点名的
    重复代码）。这里是一个函数带一个动作参数 —— 它们的差别只有那个字符串。
    """
    if action not in ("resolved", "dismissed"):
        raise InvalidInput(f"未知的处理动作：{action}")
    row = session.get(QuestionFeedback, feedback_id)
    if row is None:
        raise NotFound("这条反馈不存在")
    if row.status != "open":
        raise InvalidInput(f"这条反馈已经处理过了（{row.status}）")

    row.status = action
    if note.strip():
        existing = row.detail or ""
        row.detail = f"{existing}\n[管理员] {note.strip()}".strip()
    session.flush()
    return row


def counts(session: Session) -> dict[str, int]:
    """按状态计数 —— 后台首页要显示"还有多少没处理"。"""
    rows = session.execute(
        select(QuestionFeedback.status, func.count()).group_by(QuestionFeedback.status)
    ).all()
    out = {k: 0 for k in ("open", "resolved", "dismissed")}
    for status, n in rows:
        out[str(status)] = int(n)
    return out


def question_of(session: Session, feedback: QuestionFeedback) -> Question | None:
    """反馈指向的题（管理员要看着题判断）。**这里不缺可见性过滤**：管理员看的是
    内容质量，不是"谁有权看这道题" —— 但他也只该看到公共题。"""
    question = session.get(Question, feedback.question_id)
    if question is None or question.owner_user_id is not None:
        return None
    return question
