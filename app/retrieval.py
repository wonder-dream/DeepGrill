"""检索模块（Phase 2 §9.3）：同类题检索（text/vector/hybrid）+ 高分回答参考组装。

- search_questions：统一检索接口，评分归一化 0-1 降序返回 top-k
- build_reference：检索同类题 → 组装高分回答片段（真实回答优先，reference_answer 兜底）
- 冷启动：库内无高分数据时 build_reference 返回 None（judge 降级不注入）
"""
import logging

import numpy as np
from sqlalchemy import select

from .db import get_session
from .embed import ensure_embeddings, from_bytes
from .errors import EmbedError
from .models import Attempt, Judgment, Question, Session, SessionStatus

logger = logging.getLogger(__name__)

HIGH_SCORE = 80  # 高分阈值（total_score ≥ 80 视为高分回答）
TOP_K = 3  # 参考注入条数
REF_LEN_LIMIT = 500  # 每条参考截断字符数
HYBRID_WEIGHT = 0.8  # hybrid 融合权重（评估标定：scripts/eval_retrieval.py，MRR 最优）


def search_questions(
    query_stem: str,
    questions: list[Question],
    method: str = "hybrid",
    k: int = TOP_K,
    embedder=None,
    vector_weight: float = HYBRID_WEIGHT,
) -> list[Question]:
    """按 method 检索与 query_stem 最相关的 top-k 题（评分降序）。

    method: "text"（词表标签 + 2-gram 关键词）| "vector"（bge-m3 余弦）|
    "hybrid"（vector_weight × vector + (1-vector_weight) × text；权重由评估标定）
    """
    if not questions:
        return []
    query_vec = None
    if method in ("vector", "hybrid") and embedder is not None:
        ensure_embeddings(questions, embedder)  # 批量补算 NULL 向量
        query_vec = np.asarray(embedder.encode([query_stem])[0], dtype=np.float32)
    scores = []
    for q in questions:
        if method == "text":
            s = _text_score(query_stem, q)
        elif method == "vector":
            s = _vector_score(query_vec, q)
        else:
            s = vector_weight * _vector_score(query_vec, q) + (
                1 - vector_weight
            ) * _text_score(query_stem, q)
        scores.append((s, q))
    scores.sort(key=lambda t: t[0], reverse=True)
    return [q for s, q in scores[:k] if s > 0]


def build_reference(
    question: Question,
    questions: list[Question],
    method: str = "hybrid",
    k: int = TOP_K,
    embedder=None,
) -> str | None:
    """检索同类题 → 组装高分回答片段；空检索/无高分数据/embedding 失败返回 None（judge 降级）。"""
    try:
        hits = search_questions(question.stem, questions, method, k, embedder)
    except EmbedError as e:
        logger.warning("reference retrieval embedder failed, skip reference: %s", e)
        return None
    if not hits:
        return None
    with get_session() as session:
        texts = []
        for hit in hits:
            text = _high_score_answer(session, hit)
            if text:
                texts.append(f"- {hit.stem}\n  {text}")
    if not texts:
        return None
    return "\n\n".join(texts)


def _text_score(query_stem: str, q: Question) -> float:
    """词表标签重叠 + 题干 2-gram 关键词重叠（0-1 归一化）。"""
    q_stem = q.stem or ""
    if not query_stem or not q_stem:
        return 0.0
    gram_q = _bigrams(query_stem)
    gram_h = _bigrams(q_stem)
    if not gram_q or not gram_h:
        return 0.0
    overlap = len(gram_q & gram_h) / len(gram_q)
    tags = set(q.tags or [])
    tag_hit = 1.0 if tags & set(query_stem.split()) else 0.0
    return max(overlap, 0.3 * tag_hit)


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _vector_score(query_vec, q: Question) -> float:
    """bge-m3 余弦（query_vec 由调用方 encode 一次，向量已由 ensure_embeddings 补齐）。"""
    if query_vec is None or not q.embedding:
        return 0.0
    return float(query_vec @ from_bytes(q.embedding))


def _high_score_answer(session, question: Question) -> str | None:
    """该题最新高分 Judgment 的真实回答（attempts 拼接），无则 reference_answer 兜底。"""
    rows = session.execute(
        select(Session, Judgment)
        .join(Judgment, Judgment.session_id == Session.id)
        .where(
            Session.question_id == question.id,
            Session.status == SessionStatus.finished,
            Judgment.total_score >= HIGH_SCORE,
        )
        .order_by(Judgment.total_score.desc())
        .limit(1)
    ).all()
    if not rows:
        return None
    s, j = rows[0]
    attempts = session.scalars(
        select(Attempt)
        .where(Attempt.session_id == s.id)
        .order_by(Attempt.round_no)
    ).all()
    if attempts:
        text = "\n".join(a.answer_text for a in attempts)
    else:
        text = j.reference_answer or ""
    if not text.strip():
        return None
    return text[:REF_LEN_LIMIT] + ("..." if len(text) > REF_LEN_LIMIT else "")
