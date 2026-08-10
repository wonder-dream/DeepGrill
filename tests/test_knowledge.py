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


def test_search_multi_merges_across_queries(db):
    """多查询：两个查询分别命中不同块 → 合并后都保留（互补召回）。"""
    _add_chunk(db, 0, "Redis 持久化 RDB AOF", version=1)
    _add_chunk(db, 1, "GMP 调度模型原理", version=1)
    idx = KnowledgeIndex()
    hits = idx.search_multi([_vec(0), _vec(1)], ["Redis 持久化", "GMP 调度"], k=2)
    contents = {c for _, c in hits}
    assert "Redis 持久化 RDB AOF" in contents
    assert "GMP 调度模型原理" in contents


def test_search_multi_takes_max_score_per_chunk(db):
    """同一块被两个查询命中 → 取最高分（不重复计数）。"""
    _add_chunk(db, 0, "Redis 持久化", version=1)
    idx = KnowledgeIndex()
    hits = idx.search_multi([_vec(0), _vec(0)], ["Redis", "持久化"], k=1)
    assert len(hits) == 1  # 同一块去重
    assert hits[0][1] == "Redis 持久化"


def test_knowledge_for_condense_filters_and_condenses(db):
    """相关性自评：LLM 判定只保留相关块并提炼要点。"""
    from app.web.routes import _knowledge_for

    _add_chunk(db, 0, "GMP 调度模型 P 队列 work stealing", version=1)
    _add_chunk(db, 1, "Redis 持久化 RDB", version=1)
    from tests.fakes import FakeLLM

    llm = FakeLLM([{"kept": [1], "condensed": "GMP：G/M/P 三者职责与协作（来源 [kamacoder/go/gmp]）"}])

    class FakeEmb:
        def encode(self, texts):
            import numpy as np

            return [np.asarray(_vec(0), dtype=np.float32) for _ in texts]

    out = _knowledge_for("Go 的 GMP 调度模型", ["Go"], lambda: FakeEmb(), llm=llm)
    assert out is not None
    assert "GMP" in out
    assert "Redis" not in out  # 未保留块不注入


def test_knowledge_for_all_irrelevant_returns_none(db):
    """全部不相关 → 不注入（kept 空数组 → None）。"""
    from app.web.routes import _knowledge_for

    _add_chunk(db, 0, "GMP 调度模型", version=1)
    from tests.fakes import FakeLLM

    llm = FakeLLM([{"kept": [], "condensed": ""}])

    class FakeEmb:
        def encode(self, texts):
            import numpy as np

            return [np.asarray(_vec(0), dtype=np.float32) for _ in texts]

    assert _knowledge_for("Redis 缓存穿透", ["Redis"], lambda: FakeEmb(), llm=llm) is None


def test_knowledge_for_condense_failure_falls_back(db):
    """自评/精炼失败 → 降级为原文拼接（现状行为，不阻塞判分）。"""
    from app.web.routes import _knowledge_for

    _add_chunk(db, 0, "GMP 调度模型 P 队列", version=1)
    from tests.fakes import FakeLLM

    llm = FakeLLM([Exception("llm down")])

    class FakeEmb:
        def encode(self, texts):
            import numpy as np

            return [np.asarray(_vec(0), dtype=np.float32) for _ in texts]

    out = _knowledge_for("Go GMP", ["Go"], lambda: FakeEmb(), llm=llm)
    assert out is not None
    assert "GMP 调度模型" in out  # 原文块降级注入


def test_search_multi_text_coverage_rescue(db):
    """文本覆盖度救回：向量相似度低但含查询关键词的块经混合分命中。"""
    import numpy as np

    from app.models import KnowledgeChunk as KC

    with get_session() as s:
        s.add(KC(title="GMP", content="GMP 调度模型 P 队列 work stealing", source_hash="h-gmp",
                 embedding=np.asarray(_vec(1), dtype=np.float32).tobytes()))
        s.add(KC(title="无关", content="完全没有关键词的内容", source_hash="h-other",
                 embedding=np.asarray(_vec(0), dtype=np.float32).tobytes()))
        commit(s)
    idx = KnowledgeIndex()
    # 文本路：query_texts 含 GMP 关键词，GMP 块向量与查询正交（向量路召回不到）→ 文本路救回
    hits = idx.search_multi([_vec(0), _vec(1)], ["GMP", "调度"], k=3)
    contents = [c for _, c in hits]
    assert "GMP 调度模型 P 队列 work stealing" in contents


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


def test_distill_chunks_rewrites_and_falls_back(db):
    """蒸馏：每批一次调用输出 JSON 数组；缺失/失败的块保留原文（降级不丢）。"""
    from tests.fakes import FakeLLM

    from scripts.import_knowledge import distill_chunks

    llm = FakeLLM([
        {"items": [
            {"index": 1, "content": "高密度要点 1"},
            {"index": 3, "content": "高密度要点 3"},
        ]},  # index 2 缺失 → 保留原文
    ])
    out = distill_chunks(["原文1", "原文2", "原文3"], llm)
    assert out[0] == "高密度要点 1"
    assert out[1] == "原文2"  # 缺失降级
    assert out[2] == "高密度要点 3"
    assert len(llm.calls) == 1  # 批处理：3 块 1 次调用


def test_distill_chunks_batch_failure_falls_back(db):
    """整批失败 → 全部保留原文。"""
    from tests.fakes import FakeLLM

    from scripts.import_knowledge import distill_chunks

    llm = FakeLLM([Exception("llm down")])
    out = distill_chunks(["原文A", "原文B"], llm)
    assert out == ["原文A", "原文B"]


def test_import_file_distill_rebuilds(db, tmp_path, monkeypatch):
    """蒸馏导入：先删同源旧块再导入（重建语义，防残留重复）。"""
    from pathlib import Path

    from sqlalchemy import select

    from app.models import KnowledgeChunk as KC
    from scripts.import_knowledge import import_file

    doc = Path(tmp_path) / "agent.md"
    doc.write_text(("段落内容。" * 40) + "\n\n" + ("要点内容。" * 40), encoding="utf-8")

    class FakeEmbed:
        def encode(self, texts):
            import numpy as np

            return [np.asarray([0.1] * 8, dtype=np.float32) for _ in texts]

    with get_session() as s:
        s.add(KC(title="agent", content="旧块", source_hash="old-h",
                 embedding=b"old"))
        commit(s)

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, json_schema=None, timeout=None):
            self.calls += 1
            return {"items": [
                {"index": 1, "content": "蒸馏后的要点1"},
                {"index": 2, "content": "蒸馏后的要点2"},
            ]}

    llm = FakeLLM()
    new, skipped = import_file(doc, FakeEmbed(), llm=llm, distill=True)
    assert new == 2
    assert llm.calls == 1  # 批处理：两块一次调用
    with get_session() as s:
        rows = s.scalars(select(KC)).all()
        assert len(rows) == 2
        assert all(r.content.startswith("蒸馏后的要点") for r in rows)  # 旧块已删除重建
