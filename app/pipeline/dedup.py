"""M9 去重：归一化哈希精确重复 → bge-m3 embedding 余弦相似度阈值（Phase 2 §9.1）。

降级：embedding 不可用（EmbedError）→ 仅哈希精确去重——宁重复勿丢题（防误删新题）。
调用方（M12）不变；embedder 注入（测试用 FakeEmbedder）。
"""
import hashlib
import logging
import re
import unicodedata

import numpy as np

from ..errors import EmbedError
from ..models import Question
from ..embed import _to_bytes, ensure_embeddings, from_bytes

logger = logging.getLogger(__name__)

SIM_THRESHOLD = 0.85  # 余弦 ≥ 阈值判重复；真实模型标定：60 题最大 0.797，0.85 零误杀（docs/语义去重方案.md）
_HASH_LIMIT = 200


def dedup(
    new_questions: list[Question],
    existing_questions: list[Question],
    embedder,
) -> list[Question]:
    """保留与库内旧题不重复的新题；不落库、不抛业务异常。"""
    if not new_questions or not existing_questions:
        return list(new_questions)
    try:
        return _dedup_embedding(new_questions, existing_questions, embedder)
    except EmbedError as e:
        logger.warning("dedup embedder failed, degrade to hash-only: %s", e)
        return _dedup_hash_only(new_questions, existing_questions)


def normalize_stem(stem: str) -> str:
    """纯函数：全角转半角、去空白与标点、小写，供哈希粗筛。"""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", stem).lower())


# --- embedding 路径 ---


def _dedup_embedding(
    new_questions: list[Question],
    existing_questions: list[Question],
    embedder,
) -> list[Question]:
    """归一化精确重复快速路径 → 余弦相似度全量比对（max_sim ≥ 阈值判重复）。"""
    normalized_existing = {normalize_stem(q.stem) for q in existing_questions}
    remaining = [
        q for q in new_questions if normalize_stem(q.stem) not in normalized_existing
    ]
    if not remaining:
        return []  # 全部字面重复，无需 embedding

    ensure_embeddings(existing_questions, embedder)  # 旧题向量补算（首次全量，之后增量）
    vectors = embedder.encode([q.stem for q in remaining])
    for q, v in zip(remaining, vectors):
        q.embedding = _to_bytes(v)  # 随 daily 落库持久化，避免下次重算
    new_vecs = np.asarray(vectors, dtype=np.float32)
    old_vecs = np.stack([from_bytes(q.embedding) for q in existing_questions])
    sims = new_vecs @ old_vecs.T  # 已 L2 归一化，点积即余弦

    kept: list[Question] = []
    for i, new_q in enumerate(remaining):
        if sims[i].max() >= SIM_THRESHOLD:
            continue
        kept.append(new_q)
    return kept


# --- 降级路径（仅哈希精确去重） ---


def _dedup_hash_only(
    new_questions: list[Question],
    existing_questions: list[Question],
) -> list[Question]:
    index: dict[str, list[Question]] = {}
    for old in existing_questions:
        index.setdefault(_stem_hash(old.stem), []).append(old)

    kept: list[Question] = []
    for new_q in new_questions:
        candidates = index.get(_stem_hash(new_q.stem), [])
        if any(_same_normalized(new_q.stem, c.stem) for c in candidates):
            continue  # 归一化后字面相同判重复；语义近似降级放过（宁重复勿丢题）
        kept.append(new_q)
    return kept


def _same_normalized(a: str, b: str) -> bool:
    return normalize_stem(a) == normalize_stem(b)


def _stem_hash(stem: str) -> str:
    return hashlib.sha1(
        normalize_stem(stem)[:_HASH_LIMIT].encode("utf-8")
    ).hexdigest()
