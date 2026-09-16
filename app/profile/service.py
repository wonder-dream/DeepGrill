"""个人数据：**导出**与**注销**（决策 7 / 21）。

这两件事是公开产品的法律底线（决策 7：「必须提供注销账号与数据入口」），也是
`docs/v2数据模型.md` §9 风险③ 那条"两条数据线"的落地处 —— 注销时必须**先把线 1
（`attempts.hits`，带 user_id）累加进线 2（`question_point_stats`，只有计数）**，
再删线 1。顺序反了，聚合信号就永久丢了。

## 注销的语义（决策 21，不是"匿名化"）

```
硬删：身份（users）· 令牌 · 额度流水 · 候选人档案 · 面试与逐轮原文 · 私有题集
保留：question_point_stats（只有计数、无 user_id）
只断链：question_feedback.user_id 置空（反馈是内容质量信号，不随谁报的而改变）
```

**合规依据是「用户要求删除」，不是「数据已匿名化」** —— 这一点在
`docs/v2数据模型.md` §9 风险③ 里写明：当前规模（20 人以内）用剩余计数做减法就能
反推出被删用户答了什么。所以隐私文案里不许声称匿名。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.db.models import (
    Attempt,
    CandidateProfile,
    Criterion,
    Evaluation,
    Interview,
    InviteCode,
    KnowledgePoint,
    KnowledgePointEdge,
    Question,
    QuestionFeedback,
    QuestionPoint,
    QuestionPointStat,
    QuotaLedger,
    ReportItem,
    User,
    UserFavorite,
    UserToken,
)
from app.db.models import (
    Session_ as InterviewSession,
)

logger = logging.getLogger(__name__)


@dataclass
class DeletionReport:
    """注销实际删掉了什么。**它要能被展示/记录** —— 不许静默删数据。"""

    interviews: int = 0
    sessions: int = 0
    attempts: int = 0
    evaluations: int = 0
    report_items: int = 0
    private_questions: int = 0
    favorites: int = 0
    tokens: int = 0
    quota_rows: int = 0
    profiles: int = 0
    feedback_unlinked: int = 0
    stats_rows_touched: int = 0
    private_anchors: int = 0
    deleted: bool = False

    def summary(self) -> str:
        return (
            f"删除了 {self.interviews} 场面试、{self.sessions} 个题会话、"
            f"{self.attempts} 轮作答、{self.evaluations} 条判分、"
            f"{self.report_items} 条报告快照、{self.private_questions} 道私有题、"
            f"{self.private_anchors} 个私有题集锚点、"
            f"{self.favorites} 条收藏、{self.tokens} 个令牌、"
            f"{self.quota_rows} 行额度、{self.profiles} 份档案；"
            f"{self.feedback_unlinked} 条反馈改为匿名，"
            f"{self.stats_rows_touched} 行聚合统计被更新"
        )


# ---------------------------------------------------------------------------
# 线 1 → 线 2：把命中记录累加进聚合表
# ---------------------------------------------------------------------------
def fold_hits_into_stats(session: Session, user_id: int) -> int:
    """把该用户的 `attempts.hits` 累加进 `question_point_stats`，返回影响的统计行数。

    **必须在删线 1 之前调用** —— 顺序反了，聚合信号永久丢失（那正是决策 21 要
    "先聚合再删"的原因）。

    命中状态是**三值**：「未涉及」既不算命中也不算未命中，所以两条计数都不加
    （见 `docs/v2数据模型.md` 的 hits 说明）。
    """
    rows = session.execute(
        select(Attempt.hits, InterviewSession.question_id)
        .join(InterviewSession, InterviewSession.id == Attempt.session_id)
        .join(Interview, Interview.id == InterviewSession.interview_id)
        .where(Interview.user_id == user_id)
    ).all()

    # criterion_id → point_id（考察点归属知识点）
    criterion_point: dict[int, int] = {
        int(cid): int(pid)
        for cid, pid in session.execute(select(Criterion.id, Criterion.point_id)).all()
    }

    touched = 0
    for raw, question_id in rows:
        hits = raw if isinstance(raw, dict) else {}
        for key, status in hits.items():
            try:
                criterion_id = int(key)
            except (TypeError, ValueError):
                continue
            point_id = criterion_point.get(criterion_id)
            if point_id is None:
                continue
            hit_inc = 1 if status == "命中" else 0
            miss_inc = 1 if status == "未命中" else 0
            if not (hit_inc or miss_inc):
                continue  # 「未涉及」两条线都不计
            row = session.execute(
                select(QuestionPointStat).where(
                    QuestionPointStat.question_id == question_id,
                    QuestionPointStat.point_id == point_id,
                    QuestionPointStat.criterion_id == criterion_id,
                )
            ).scalar_one_or_none()
            if row is None:
                row = QuestionPointStat(
                    question_id=question_id,
                    point_id=point_id,
                    criterion_id=criterion_id,
                    hit_count=0,
                    miss_count=0,
                )
                session.add(row)
            row.hit_count += hit_inc
            row.miss_count += miss_inc
            touched += 1
    return touched


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export_user_data(session: Session, user_id: int) -> dict[str, Any]:
    """导出这个人的全部数据（决策 23 的三项之一：导出我的数据）。

    形态是**一个 JSON 文件**，不是页面 —— 它要能被人下载、带走、存档。
    里面**不含口令哈希与令牌**：那些是凭据，不是"我的数据"，导出它们只会多一处泄露面。
    """
    user = session.get(User, user_id)
    if user is None:
        raise ValueError("用户不存在")

    interviews = list(
        session.execute(
            select(Interview).where(Interview.user_id == user_id).order_by(Interview.id)
        )
        .scalars()
        .all()
    )
    payload: dict[str, Any] = {
        "account": {
            "email": user.email,
            "username": user.username,
            "role": user.role,
            "created_at": user.created_at,
        },
        "quota": [
            {"day": r.day, "kind": r.kind, "units_used": r.units_used, "tokens_used": r.tokens_used}
            for r in session.execute(
                select(QuotaLedger).where(QuotaLedger.user_id == user_id).order_by(QuotaLedger.day)
            ).scalars()
        ],
        "profiles": [
            {"structured": p.structured, "source_note": p.source_note, "created_at": p.created_at}
            for p in session.execute(
                select(CandidateProfile).where(CandidateProfile.user_id == user_id)
            ).scalars()
        ],
        "interviews": [],
    }

    for interview in interviews:
        sessions = session.execute(
            select(InterviewSession)
            .where(InterviewSession.interview_id == interview.id)
            .order_by(InterviewSession.seq)
        ).scalars().all()
        entry: dict[str, Any] = {
            "id": interview.id,
            "mode": interview.mode,
            "status": interview.status,
            "plan": interview.plan,
            "quota_charged": interview.quota_charged,
            "started_at": interview.started_at,
            "ended_at": interview.ended_at,
            "report_summary": interview.report_summary,
            "report_body": interview.report_body,
            "sessions": [],
        }
        for ts in sessions:
            attempts = session.execute(
                select(Attempt)
                .where(Attempt.session_id == ts.id)
                .order_by(Attempt.round_no)
            ).scalars().all()
            # 取**一行**：一个题会话只有一条最终评分（迁移 0005 的唯一索引，决策 89）。
            # `order_by(id.desc()).first()` 而不是 `scalar_one_or_none()` 是刻意的
            # 冗余 —— 唯一索引保证不了"这个库跑过 0005"（老库、备份恢复回来的库），
            # 而两行会让 `scalar_one_or_none()` 抛 `MultipleResultsFound`：
            # **整个导出接口 500**，且用户没有任何自救办法。多取一行的排序，
            # 换掉一整类"永久 500"。
            evaluation = session.execute(
                select(Evaluation)
                .where(Evaluation.session_id == ts.id)
                .order_by(Evaluation.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            entry["sessions"].append(
                {
                    "seq": ts.seq,
                    "question_id": ts.question_id,
                    "status": ts.status,
                    "max_rounds": ts.max_rounds,
                    "attempts": [
                        {
                            "round_no": a.round_no,
                            "input_mode": a.input_mode,
                            "answer_text": a.answer_text,
                            "feedback_text": a.feedback_text,
                            "hits": a.hits,
                        }
                        for a in attempts
                    ],
                    "evaluation": (
                        None
                        if evaluation is None
                        else {
                            "scores": evaluation.scores,
                            "total_score": evaluation.total_score,
                            "review": evaluation.review,
                            "status": evaluation.status,
                        }
                    ),
                }
            )
        payload["interviews"].append(entry)

    payload["private_questions"] = [
        q.to_dict() for q in bank_repository.owned_questions(session, user_id)
    ]
    payload["favorites"] = [
        f.question_id
        for f in session.execute(
            select(UserFavorite).where(UserFavorite.user_id == user_id).order_by(UserFavorite.id)
        ).scalars()
    ]
    return payload


# ---------------------------------------------------------------------------
# 注销（硬删）
# ---------------------------------------------------------------------------
def delete_account(session: Session, user_id: int) -> DeletionReport:
    """硬删这个人的身份与原文，聚合判定留在 `question_point_stats`（决策 21）。

    ⚠️ **顺序不能变**：先聚合、再断外部引用、最后删主体。理由是每一步都依赖前一步
    的产物（见 `fold_hits_into_stats`），而 v2 **一条 `FK CASCADE` 都没有** ——
    顺序错了不是"少删了"，而是直接撞外键约束。
    """
    report = DeletionReport()
    user = session.get(User, user_id)
    if user is None:
        return report

    # ① 线 1 → 线 2（必须在删任何东西之前）
    report.stats_rows_touched = fold_hits_into_stats(session, user_id)

    interview_ids = list(
        session.execute(select(Interview.id).where(Interview.user_id == user_id)).scalars()
    )
    session_ids: list[int] = []
    if interview_ids:
        session_ids = list(
            session.execute(
                select(InterviewSession.id).where(InterviewSession.interview_id.in_(interview_ids))
            ).scalars()
        )

    # ② 断外部引用：`invite_codes` 自引用 users（不清就撞外键）；
    #    `question_feedback` 只置空 user_id —— 反馈是内容质量信号，不随谁报的而变。
    #    ⚠️ 收藏**不能**照抄这个做法：`user_favorites.user_id` 是 NOT NULL（收藏
    #    本身就是"我的数据"，没有匿名价值），只能删。这里写出来是因为两者长得很像。
    report.feedback_unlinked = (
        session.execute(
            update(QuestionFeedback)
            .where(QuestionFeedback.user_id == user_id)
            .values(user_id=None)
        ).rowcount
        or 0
    )

    # ③ 删子表（自下而上）
    if session_ids:
        report.attempts = session.execute(
            delete(Attempt).where(Attempt.session_id.in_(session_ids))
        ).rowcount or 0
        report.evaluations = session.execute(
            delete(Evaluation).where(Evaluation.session_id.in_(session_ids))
        ).rowcount or 0
    if interview_ids:
        report.report_items = session.execute(
            delete(ReportItem).where(ReportItem.interview_id.in_(interview_ids))
        ).rowcount or 0
        report.sessions = session.execute(
            delete(InterviewSession).where(InterviewSession.interview_id.in_(interview_ids))
        ).rowcount or 0
        report.interviews = session.execute(
            delete(Interview).where(Interview.id.in_(interview_ids))
        ).rowcount or 0

    report.favorites = session.execute(
        delete(UserFavorite).where(UserFavorite.user_id == user_id)
    ).rowcount or 0
    report.profiles = session.execute(
        delete(CandidateProfile).where(CandidateProfile.user_id == user_id)
    ).rowcount or 0
    report.quota_rows = session.execute(
        delete(QuotaLedger).where(QuotaLedger.user_id == user_id)
    ).rowcount or 0
    report.tokens = session.execute(
        delete(UserToken).where(UserToken.user_id == user_id)
    ).rowcount or 0

    # ⚠️ 私有题集删除前必须先清掉指向它的两处引用 —— 外键是**真的开着**的
    #    （v2 没有 CASCADE，所以顺序错了不是"少删了"，是直接撞约束）：
    #      · `question_point_stats`：它的 question_id 指向题
    #      · `user_favorites`：别人的收藏也可能指向这些题（题没了，收藏就该没）
    #    这一处是测试抓出来的：第一版直接删题，撞 `FOREIGN KEY constraint failed`。
    private_ids = bank_repository.owned_ids(session, user_id)
    if private_ids:
        session.execute(
            delete(QuestionPointStat).where(QuestionPointStat.question_id.in_(private_ids))
        )
        session.execute(
            delete(UserFavorite).where(UserFavorite.question_id.in_(private_ids))
        )
    report.private_questions = session.execute(
        delete(Question).where(Question.owner_user_id == user_id)
    ).rowcount or 0

    # ③.6 私有题集的**锚点**与它派生的考察点（决策 91）
    #      实测：注销之后锚点还在、挂在它下面的考察点也还在 —— 而那条考察点的文本
    #      是从用户简历里派生的，锚点名字里还带着 user id。`delete_account()` 原来
    #      从头到尾没碰过 `knowledge_points`。
    report.private_anchors = _delete_private_anchors(session, user_id)

    # ④ `invite_codes` 的两个自引用外键必须断掉，否则删 users 会撞约束
    session.execute(
        update(InviteCode).where(InviteCode.created_by == user_id).values(created_by=None)
    )
    session.execute(
        update(InviteCode).where(InviteCode.used_by == user_id).values(used_by=None)
    )

    # ⑤ 主体
    session.execute(delete(User).where(User.id == user_id))
    session.flush()
    report.deleted = True

    logger.info("已注销用户 %s：%s", user_id, report.summary())
    return report


def _delete_private_anchors(session: Session, user_id: int) -> int:
    """删掉这位用户的私有题集锚点，以及挂在它下面的一切（决策 91）。

    锚点是 `私有题集（用户 {id}）`（`offline/profile_pipeline.py::_private_anchor`）：
    一个 `status='draft'` 的私有容器 —— 它让"简历生成的题"立刻可判分，而不去污染
    公共知识地图。**认它靠 `owner_user_id`，不靠名字**：按名字匹配等于把一处格式
    约定复制成两处，改一处就会静默漏删。

    ⚠️ 顺序：先删**引用了这些行的东西**，再删考察点，最后删知识点。
    v2 没有一条 `FK CASCADE`，顺序错了不是"少删了"，是直接撞外键：

    · `question_point_stats`：它的 `criterion_id` / `point_id` 都指向这里。
      **决策 21 说"聚合判定保留"，但这一份留不住也没必要留** —— 它们的
      `question_id` 指向的是刚被删掉的私有题；决策 21 保留的是**公共题上**的聚合。
    · `question_points`：关联轴（挂载流程写的；私有锚点正常不会出现在里面，
      但它是外键，防御性地一起清）。
    · `knowledge_point_edges`：前置依赖边可能指向这个锚点（它是个 draft 节点，
      正常不会，但同样是外键）。
    · `criteria`：**简历派生的考察点文本就是在这里** —— 最后删它。

    返回删掉的锚点个数（`DeletionReport` 要把"删了什么"说出来，不许静默）。
    """
    anchors = list(
        session.execute(
            select(KnowledgePoint.id).where(KnowledgePoint.owner_user_id == user_id)
        ).scalars()
    )
    if not anchors:
        return 0

    criterion_ids = list(
        session.execute(select(Criterion.id).where(Criterion.point_id.in_(anchors))).scalars()
    )
    # 两个条件各自可能单独命中（按 point 或按 criterion），用 `or_` 一次删干净
    stat_filters = [QuestionPointStat.point_id.in_(anchors)]
    if criterion_ids:
        stat_filters.append(QuestionPointStat.criterion_id.in_(criterion_ids))
    session.execute(delete(QuestionPointStat).where(or_(*stat_filters)))
    session.execute(delete(QuestionPoint).where(QuestionPoint.point_id.in_(anchors)))
    session.execute(
        delete(KnowledgePointEdge).where(
            or_(
                KnowledgePointEdge.from_point_id.in_(anchors),
                KnowledgePointEdge.to_point_id.in_(anchors),
            )
        )
    )
    session.execute(delete(Criterion).where(Criterion.point_id.in_(anchors)))
    removed = session.execute(
        delete(KnowledgePoint).where(KnowledgePoint.id.in_(anchors))
    ).rowcount or 0
    session.flush()
    return removed


def count_remaining(session: Session, user_id: int) -> dict[str, int]:
    """注销前给用户看"将删除什么"。**不许静默删数据**（AGENTS.md §3.1）。"""
    def _count(model, column) -> int:
        return int(
            session.execute(select(func.count()).select_from(model).where(column == user_id)).scalar_one()
        )

    return {
        "interviews": _count(Interview, Interview.user_id),
        "private_questions": _count(Question, Question.owner_user_id),
        "favorites": _count(UserFavorite, UserFavorite.user_id),
        "profiles": _count(CandidateProfile, CandidateProfile.user_id),
    }


__all__ = [
    "DeletionReport",
    "count_remaining",
    "delete_account",
    "export_user_data",
    "fold_hits_into_stats",
]
