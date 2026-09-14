"""知识层与自修复的离线任务（ADR-0006：任务函数必须**幂等**）。

这些函数跑两遍结果一样，因为队列的重跑是常态（心跳超时回退、手动重跑）。
它们只做"读一批 → 算 → 写结果"，重算覆盖原结果 —— 这正是幂等的形状。

## 挂载自修复为什么住在这里

它要同时碰「知识点的考察点命中统计」与「这道题挂在哪个知识点下」—— **横跨两个
领域**。而 `bank` 与 `knowledge` 互不 import（ADR-0005），所以这个动作只能住在能
依赖两者的编排层。CONTEXT.md 记着这个别扭感是**边界清晰的代价**，不是失误。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import KnowledgePoint, Question, QuestionFlag, QuestionPointStat
from app.offline.jobs import JobSpec, register

logger = logging.getLogger(__name__)

#: 一条考察点"大家普遍答不到"的判据：累计考过 N 次、且未命中率超过阈值。
#: **这两个数字是待标定的**（v1 的相似度阈值教训：语料变了就要重测）——
#: 所以它们在这里显式写成常量，而不是散在判断里。
MIN_SAMPLES = 5
SUSPECT_MISS_RATE = 0.8


def self_repair_stats(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """挂载自修复的**信号**：把"长期答不到本知识点考察点"的题标成可疑。

    ⚠️ 它**只标记，不改挂载** —— `question_flags` 是给人看的仪表板（ADR-0002：
    "矛盾记录是这套结构唯一白送的能力"）。自动改挂载会让"为什么这道题换了知识点"
    变成一件没人能解释的事。

    幂等：先清掉自己上一次生成的 `suspect_mount` 未处理记录，再重建 ——
    跑两遍结果一样（不会累积）。
    """
    deleted = session.query(QuestionFlag).filter(
        QuestionFlag.kind == "suspect_mount", QuestionFlag.status == "open"
    ).delete(synchronize_session=False)

    rows = session.execute(
        select(
            QuestionPointStat.question_id,
            QuestionPointStat.point_id,
            QuestionPointStat.hit_count,
            QuestionPointStat.miss_count,
        )
    ).all()

    # 按题聚合：这道题的这些考察点累计考了多少、答到多少
    per_question: dict[int, list[tuple[int, int, int]]] = {}
    for question_id, point_id, hit, miss in rows:
        per_question.setdefault(question_id, []).append((point_id, hit, miss))

    flagged: list[dict[str, Any]] = []
    for question_id, stats in per_question.items():
        total = sum(h + m for _, h, m in stats)
        if total < MIN_SAMPLES:
            continue  # 样本不够 —— 判据③ 的局限：样本不足时不要乱动
        miss = sum(m for _, _, m in stats)
        rate = miss / total if total else 0.0
        if rate < SUSPECT_MISS_RATE:
            continue
        question = session.get(Question, question_id)
        if question is None or question.primary_point_id is None:
            continue
        point = session.get(KnowledgePoint, question.primary_point_id)
        session.add(
            QuestionFlag(
                question_id=question_id,
                kind="suspect_mount",
                detail=(
                    f"累计考过 {total} 次，未命中 {miss} 次（{round(rate * 100)}%）—— "
                    f"可能挂错了知识点（现挂在「{point.name if point else '?'}」下）"
                ),
                status="open",
            )
        )
        flagged.append({"question_id": question_id, "miss_rate": round(rate, 2)})

    session.flush()
    return {
        "message": f"标记 {len(flagged)} 道可疑挂载（清理了 {deleted} 条旧记录）",
        "flagged": flagged,
        "deleted_previous": deleted,
    }


def purge_finished_jobs(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """让"回收者"也能被队列自己触发的入口（AGENTS.md §3.2）。

    它本身是幂等的（删过就没有了）。
    """
    from app.offline import jobs as jobs_module

    purged = jobs_module.purge_finished(session)
    return {"message": f"回收 {purged} 条终态任务记录", "purged": purged}


def purge_explanations(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """清掉过期与超量的**讲解缓存**（决策 67 + AGENTS.md §3.2）。

    为什么讲解缓存要挂在这里：它是**长期驻留的库表状态**（题目数 × 版本数），
    而 §3.2 的要求是"任何新增状态都要有回收者" —— 没有它，这张表只会一直长。
    `purge()` 是幂等的（删过就没有了），所以可以被队列重试。
    """
    from app.knowledge import explanation as explanation_module

    report = explanation_module.purge(session)
    return {
        "message": f"讲解缓存：{report}",
        "by_age": report.by_age,
        "by_capacity": report.by_capacity,
    }


def flag_duplicate_questions(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """事后治理的**检测**：把"同知识点下题干相同"的题标出来（决策 5 + ADR-0002）。

    它**只标记、不改题库** —— 自动合并会让"这道题为什么不见了"没人能解释。
    处置由人在治理页做（`/admin/quality`）。
    """
    from app.bank import quality as quality_module

    return quality_module.flag_duplicate_questions(session, payload)


def purge_embeddings(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """回收**嵌入缓存**（ADR-0008 + AGENTS.md §3.2）。

    它在库里的体量比讲解缓存大一个量级（每行 ~6KB 的向量），所以更需要回收者：
    TTL 清长期没用到的，容量上限兜底。`purge()` 幂等，可被队列重跑。
    """
    from app.offline import embedding_store

    report = embedding_store.purge(session)
    return {
        "message": f"嵌入缓存：{report}",
        "by_age": report.by_age,
        "by_capacity": report.by_capacity,
    }


def generate_public_questions(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """给缺题的知识点补公共题（决策 4 的"生成题"那条来源）。

    `payload` 可选：`{"point_id": N, "count": M}` 只补某个点；不给就扫一遍缺题的
    （`per_point` / `limit` 可调）。它是**幂等**的：题干已存在就跳过，所以队列重跑安全。

    成本：这是一次**离线**调用，没有哪个用户该为它付额度点，所以账本里没有它的位置 ——
    用量记在这条任务报告里（`tokens`），观测页的「离线任务报告」看得到。
    """
    from app.config import Settings
    from app.deps import get_llm
    from app.offline import generation

    # ⚠️ 必须**显式传 Settings**：`get_llm(settings=Depends(get_settings))` 是给
    # FastAPI 依赖注入用的 —— 直接 `get_llm()` 拿到的是那个 `Depends` 对象本身
    # （实测报 `'Depends' object has no attribute 'llm_api_key'`）。
    llm = get_llm(Settings())
    point_id = payload.get("point_id")
    if point_id is not None:
        from app.db.models import KnowledgePoint

        point = session.get(KnowledgePoint, int(point_id))
        if point is None:
            return {"message": f"知识点 {point_id} 不存在", "created": 0}
        reports = [
            generation.generate_for_point(
                session,
                point=point,
                count=int(payload.get("count") or generation.DEFAULT_COUNT),
                llm=llm,
            )
        ]
    else:
        reports = generation.generate_for_missing(
            session,
            llm=llm,
            per_point=int(payload.get("per_point") or generation.DEFAULT_COUNT),
            limit=int(payload["limit"]) if payload.get("limit") else None,
        )

    created = sum(r.created for r in reports)
    tokens = sum(r.tokens for r in reports)
    return {
        "message": f"补题：{len(reports)} 个知识点、新增 {created} 道"
        + (f"、{tokens} token" if tokens else ""),
        "created": created,
        "tokens": tokens,
        "points": [r.to_dict() for r in reports],
    }


register(
    JobSpec(
        name="self_repair_stats",
        run=self_repair_stats,
        idempotent=True,
        description="扫描考察点命中统计，把长期答不到的题标成挂载可疑（只标记不改挂载）",
    )
)
register(
    JobSpec(
        name="purge_finished_jobs",
        run=purge_finished_jobs,
        idempotent=True,
        description="回收终态任务记录（TTL 见 jobs.FINISHED_TTL）",
    )
)
register(
    JobSpec(
        name="purge_explanations",
        run=purge_explanations,
        idempotent=True,
        description="回收讲解缓存（TTL 与容量上限见 knowledge.explanation）",
    )
)
register(
    JobSpec(
        name="flag_duplicate_questions",
        run=flag_duplicate_questions,
        idempotent=True,
        description="事后治理的检测：标记同知识点下的重复题（只标记，处置由人做）",
    )
)
register(
    JobSpec(
        name="purge_embeddings",
        run=purge_embeddings,
        idempotent=True,
        description="回收嵌入缓存（TTL 与容量上限见 offline.embedding_store）",
    )
)
register(
    JobSpec(
        name="generate_public_questions",
        run=generate_public_questions,
        idempotent=True,
        description="给缺题的已确认知识点补公共题（过自动门禁；题干重复则跳过）",
    )
)
