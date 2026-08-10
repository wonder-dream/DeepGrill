"""检索模块（Phase 2 §9.3）：同类题检索（text/vector/hybrid）+ 高分回答参考组装 + 知识库 RAG。

- search_questions：统一检索接口，评分归一化 0-1 降序返回 top-k
- build_reference：检索同类题 → 组装高分回答片段（真实回答优先，reference_answer 兜底）
- KnowledgeIndex：知识库向量索引（FAISS IndexFlatIP 内存层 + SQLite 数据层），
  version 检测自动重建（导入即生效），降级链 faiss → numpy 暴力 → 空结果
- 冷启动：库内无高分数据时 build_reference 返回 None（judge 降级不注入）
"""
import logging
import threading

import numpy as np
from sqlalchemy import select

from .db import get_session
from .embed import ensure_embeddings, from_bytes
from .errors import EmbedError
from .models import Attempt, Judgment, KnowledgeChunk, KnowledgeMeta, Question, Session, SessionStatus

logger = logging.getLogger(__name__)

HIGH_SCORE = 80  # 高分阈值（total_score ≥ 80 视为高分回答）
TOP_K = 3  # 参考注入条数
REF_LEN_LIMIT = 500  # 每条参考截断字符数
HYBRID_WEIGHT = 0.8  # hybrid 融合权重（评估标定：scripts/eval_retrieval.py，MRR 最优）

KNOWLEDGE_K = 5  # 知识注入块数
KNOWLEDGE_MAX_CHARS = 4000  # 知识片段拼接上限（防爆上下文）


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


class KnowledgeIndex:
    """知识库向量索引：FAISS IndexFlatIP 内存层 + SQLite 数据层。

    - 进程内单例（Embedder 单例同模式）；查询前比对 knowledge_meta.version，
      导入脚本 version+1 后下次查询自动重建（1-2s 一次性，无需重启）
    - 降级链：faiss 不可用/构建失败 → numpy 暴力余弦 → 返回空（调用方不注入）
    - Index.search 只读线程安全，10 并发无锁压力
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._version = -1
        self._index = None  # faiss.Index 或 numpy 矩阵（降级）
        self._ids: list[int] = []
        self._faiss = None
        try:
            import faiss  # noqa: F401

            self._faiss = faiss
        except ImportError:
            logger.warning("faiss 不可用，知识库检索降级 numpy 暴力")

    def search(self, query_vec, k: int = KNOWLEDGE_K) -> list[tuple[str, str]]:
        """检索相关知识块，返回 [(title, content)]；异常/无数据返回空。"""
        try:
            with self._lock:
                with get_session() as session:
                    meta = session.get(KnowledgeMeta, 1)
                    version = meta.version if meta is not None else 0
                    if version != self._version:
                        self._rebuild(session)
                        self._version = version
                if self._index is None or not self._ids:
                    return []
                q = np.asarray(query_vec, dtype=np.float32)
                if self._faiss is not None:
                    scores, idxs = self._index.search(q.reshape(1, -1), k)
                    ids = [self._ids[i] for i in idxs[0] if i >= 0]
                else:
                    sims = self._index @ q
                    order = np.argsort(-sims)[:k]
                    ids = [self._ids[i] for i in order if i < len(self._ids) and sims[i] > 0]
                if not ids:
                    return []
                with get_session() as session:
                    chunks = [session.get(KnowledgeChunk, i) for i in ids]
                out = []
                for c in chunks:
                    if c is not None and c.content:
                        out.append((c.title or "", c.content))
                return out
        except Exception as e:
            logger.warning("knowledge search failed: %s", e)
            return []

    def _rebuild(self, session) -> None:
        chunks = session.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.embedding.is_not(None))
        ).all()
        if not chunks:
            self._index = None
            self._ids = []
            return
        vectors = np.array([from_bytes(c.embedding) for c in chunks], dtype=np.float32)
        if self._faiss is not None:
            idx = self._faiss.IndexFlatIP(vectors.shape[1])
            idx.add(vectors)
            self._index = idx
        else:
            self._index = vectors
        self._ids = [c.id for c in chunks]
        logger.info("knowledge index rebuilt: %d chunks (faiss=%s)", len(chunks), self._faiss is not None)


def knowledge_search(query_vec, k: int = KNOWLEDGE_K) -> list[tuple[str, str]]:
    """知识检索便捷入口（进程内单例）。"""
    return _knowledge_index.search(query_vec, k)


def format_knowledge(chunks: list[tuple[str, str]]) -> str:
    """知识片段拼接为 prompt 段（标题+内容，总长严格不超过 KNOWLEDGE_MAX_CHARS）。"""
    parts = []
    total = 0
    for title, content in chunks:
        room = KNOWLEDGE_MAX_CHARS - total
        if room <= 0:
            break
        block = f"- [{title}] {content}" if title else f"- {content}"
        if len(block) > room:
            block = block[:room]
        parts.append(block)
        total += len(block)
    text = "\n\n".join(parts)
    return text[:KNOWLEDGE_MAX_CHARS]  # 分隔符兜底截断


_knowledge_index = KnowledgeIndex()
