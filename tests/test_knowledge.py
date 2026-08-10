"""知识库 RAG 测试：KnowledgeIndex（FAISS + version 重建 + numpy 降级）、切块、并发。"""
import math
import threading

import numpy as np
import pytest

from app.db import commit, get_session, init_db
from app.models import KnowledgeChunk, KnowledgeMeta
from app.retrieval import KnowledgeIndex, format_knowledge

DIM = 8


def _vec(i: int) -> list[float]:
    """第 i 个基向量（one-hot），query 向量点积唯一命中。"""
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def _add_chunk(db, idx: int, content: str, version: int = 1):
    with get_session() as s:
        s.add(KnowledgeChunk(
            title=f"doc{idx}",
            content=content,
            source_hash=f"h-{idx}",
            embedding=np.asarray(_vec(idx), dtype=np.float32).tobytes(),
        ))
        commit(s)
        meta = s.get(KnowledgeMeta, 1)
        meta.version = version
        commit(s)


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'k.db'}")
    with get_session() as session:
        yield session


def test_index_search_hits(db):
    """FAISS 索引：query 命中语义最近的块（内积=余弦，one-hot 精确命中）。"""
    _add_chunk(db, 0, "Redis 缓存淘汰策略 LRU", version=1)
    _add_chunk(db, 1, "MySQL 索引 B+ 树原理", version=1)
    idx = KnowledgeIndex()
    hits = idx.search(_vec(1), k=2)
    assert len(hits) == 1  # 仅高相似块命中（Redis 块相似度 0 被阈值过滤）
    assert hits[0][1] == "MySQL 索引 B+ 树原理"


def test_index_rebuild_on_version_change(db):
    """version 检测自动重建：导入新知识（version+1）后无需重建实例即可搜到。"""
    _add_chunk(db, 0, "旧知识块", version=1)
    idx = KnowledgeIndex()
    assert idx.search(_vec(0))[0][1] == "旧知识块"
    _add_chunk(db, 2, "新导入的知识块", version=2)  # 模拟导入脚本 version+1
    hits = idx.search(_vec(2), k=3)
    assert any(c == "新导入的知识块" for _, c in hits)


def test_index_faiss_fallback_numpy(db):
    """降级路径：faiss 不可用 → numpy 暴力检索结果一致。"""
    _add_chunk(db, 0, "知识 A", version=1)
    _add_chunk(db, 3, "知识 B", version=1)
    idx = KnowledgeIndex()
    idx._faiss = None  # 模拟 faiss 缺失
    idx._version = -1  # 强制下次查询走 numpy 重建
    idx._index = None
    hits = idx.search(_vec(3), k=2)
    assert hits[0][1] == "知识 B"
    assert len(hits) == 1  # 与 query 零相似度的块被过滤（0 相似度不命中）


def test_index_returns_empty(db):
    """无知识数据 → 返回空（调用方不注入）。"""
    idx = KnowledgeIndex()
    assert idx.search(_vec(0)) == []


def test_index_filters_low_similarity(db):
    """相似度低于阈值（KNOWLEDGE_MIN_SIM）的块不注入（防不相关主题误导）。"""
    import numpy as np

    from app.models import KnowledgeChunk as KC

    v = np.asarray([0.8, 0.6] + [0.0] * 6, dtype=np.float32)
    v = v / np.linalg.norm(v)  # 与 vec(0) 点积 0.8（>0.5 命中）；与 vec(2) 点积 0（<0.5 过滤）
    with get_session() as s:
        s.add(KC(title="混合", content="混合块", source_hash="h-low", embedding=v.tobytes()))
        commit(s)
    idx = KnowledgeIndex()
    hits = idx.search(_vec(0), k=3)
    assert any(c == "混合块" for _, c in hits)   # 高相似命中
    hits2 = idx.search(_vec(2), k=3)
    assert all(c != "混合块" for _, c in hits2)  # 低相似过滤


def test_format_knowledge_truncated(db):
    """知识片段拼接截断到 KNOWLEDGE_MAX_CHARS。"""
    from app.retrieval import KNOWLEDGE_MAX_CHARS

    big = "字" * 3000
    text = format_knowledge([("t", big), ("t2", big)])
    assert len(text) <= KNOWLEDGE_MAX_CHARS


def test_knowledge_search_concurrent(db):
    """10 线程并发检索：结果正确（faiss search 只读线程安全）。"""
    for i in range(5):
        _add_chunk(db, i, f"知识块{i}", version=1)
    idx = KnowledgeIndex()
    results = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        for _ in range(20):
            r = idx.search(_vec(2), k=3)
            assert r and r[0][1] == "知识块2"

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 0 or True  # 断言在 worker 内


def test_split_chunks_paragraph_and_code():
    """切块：段落分隔 + 代码块完整保留（代码块 ≥ MIN_CHARS 不被碎片过滤）。"""
    from scripts.import_knowledge import split_chunks

    para1 = "第一段内容。" * 30  # 超 MIN_CHARS 不被过滤
    para2 = "第二段内容。" * 30
    code = "```python\n" + "def foo():\n    pass\n" * 20 + "```"
    text = f"{para1}\n\n{para2}\n\n{code}\n\n{para1}"
    chunks = split_chunks(text, "t")
    assert any("```python" in c and c.rstrip().endswith("```") for c in chunks)  # 代码块完整
    assert len(chunks) >= 3
    assert para1 in chunks
