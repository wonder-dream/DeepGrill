"""Embedding 模块（Phase 2 §9.1）：bge-m3 本地向量化。

- Embedder：lazy 加载 SentenceTransformer（首次 encode 才下载/加载模型），GPU 优先，
  batch encode + L2 归一化（bge-m3 建议）；任何失败抛 EmbedError 由调用方降级
- ensure_embeddings：补算 Question.embedding 为 NULL 的题（写回对象 + 落库）
- 向量序列化：numpy float32 bytes（~4KB/题）
"""
import logging

import numpy as np

from .db import commit, get_session
from .errors import EmbedError
from .models import Question

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-m3"
BATCH_SIZE = 32


def _to_bytes(vector) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_bytes(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class Embedder:
    """bge-m3 封装；lazy 加载，设备自动选择（GPU 优先）。"""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self._model_name = model_name
        self._model = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        """文本 → L2 归一化向量；空输入返回 []。"""
        if not texts:
            return []
        try:
            embeddings = self._load().encode(
                texts,
                batch_size=BATCH_SIZE,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except EmbedError:
            raise
        except Exception as e:
            logger.exception("embedding encode failed")
            raise EmbedError(f"embedding encode failed: {e}") from e
        return [v.tolist() for v in embeddings]

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer

                logger.info("loading embedding model %s (首次加载需下载模型)", self._model_name)
                self._model = SentenceTransformer(self._model_name)
            except Exception as e:
                raise EmbedError(
                    f"cannot load embedding model {self._model_name}: {e}"
                ) from e
        return self._model


def ensure_embeddings(questions: list[Question], embedder) -> None:
    """补算 embedding 为 NULL 的题；已落库的写回 DB，未落库（无 id）仅写内存对象。

    失败抛 EmbedError（由 dedup 降级）。
    """
    missing = [q for q in questions if not q.embedding]
    if not missing:
        return
    vectors = embedder.encode([q.stem for q in missing])
    for q, vec in zip(missing, vectors):
        q.embedding = _to_bytes(vec)
    if not any(q.id is not None for q in missing):
        return  # 全为未落库对象（批内新题），仅写内存对象
    with get_session() as session:
        for q in missing:
            if q.id is not None:
                session.merge(q)
        commit(session)
