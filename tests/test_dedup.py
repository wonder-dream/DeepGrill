import math

import numpy as np
import pytest

from app.db import commit, get_session, init_db
from app.errors import EmbedError
from app.models import Question, QuestionType, Source, SourceType
from app.pipeline.dedup import SIM_THRESHOLD, dedup, normalize_stem
from tests.fakes import FakeEmbedder

DIM = 64


@pytest.fixture
def file_db(tmp_path):
    """dedup 会经 ensure_embeddings 落库，需文件 SQLite（内存库多连接会丢表）。"""
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    with get_session() as session:
        yield session


def q(stem, qid=None):
    return Question(id=qid, source_id=1, type=QuestionType.knowledge, stem=stem)


def persist(db, question):
    """预置旧题入库（merge 需真实行，source_id 有 NOT NULL 约束）。"""
    source = Source(type=SourceType.manual, source_hash=f"h-{question.stem}")
    db.add(source)
    commit(db)
    db.refresh(source)
    question.source_id = source.id
    db.add(question)
    commit(db)
    db.refresh(question)
    return question


def vec(*xs):
    """按给定坐标构造归一化向量（坐标主导，其余 0）。"""
    xs = list(xs)
    while len(xs) < DIM:
        xs.append(0.0)
    norm = math.sqrt(sum(x * x for x in xs))
    return [x / norm for x in xs]


def angled_vec(x, y):
    """与 x 轴夹角 cos = x/|v| 的归一化向量，用于构造指定余弦相似度。"""
    return vec(x, y)


# --- normalize_stem ---


def test_normalize_stem_fullwidth_whitespace_punct():
    assert normalize_stem(" 讲一下ＨａｓｈＭａｐ 底层原理！ ") == "讲一下hashmap底层原理"
    assert normalize_stem("Redis 持久化? AOF") == "redis持久化aof"


# --- happy（embedding 路径） ---


def test_exact_duplicate_dropped(file_db):
    old = persist(file_db, q("讲一下 HashMap 底层原理"))
    new = [q("讲一下 ＨａｓｈＭａｐ　底层原理")]
    embedder = FakeEmbedder()
    assert dedup(new, [old], embedder) == []
    assert embedder.calls == []  # 归一化精确重复走快速路径，无需 embedding


def test_semantic_duplicate_dropped(file_db):
    """语义重复：两题向量一致（同知识点不同措辞的模拟）→ 判重丢弃。"""
    old = persist(file_db, q("旧题：Redis 持久化"))
    new = [q("新题：Redis 的 AOF 和 RDB 怎么选")]
    embedder = FakeEmbedder(vectors={
        "旧题：Redis 持久化": vec(1),
        "新题：Redis 的 AOF 和 RDB 怎么选": vec(1),
    })
    assert dedup(new, [old], embedder) == []


def test_different_topic_kept(file_db):
    old = persist(file_db, q("旧题：JVM 调优"))
    new = [q("新题：Kafka 消费堆积")]
    embedder = FakeEmbedder(vectors={"旧题：JVM 调优": vec(1), "新题：Kafka 消费堆积": vec(0, 1)})
    assert dedup(new, [old], embedder) == new


def test_similarity_threshold_boundary(file_db):
    """边界：相似度 0.84（< 阈值）保留；0.86（≥ 阈值）判重。"""
    old = persist(file_db, q("旧题"))
    embedder = FakeEmbedder(vectors={
        "旧题": vec(1),
        "相似 0.84": angled_vec(0.84, math.sqrt(1 - 0.84**2)),
        "相似 0.86": angled_vec(0.86, math.sqrt(1 - 0.86**2)),
    })
    kept = dedup([q("相似 0.84"), q("相似 0.86")], [old], embedder)
    assert [k.stem for k in kept] == ["相似 0.84"]
    assert SIM_THRESHOLD == 0.85


# --- edge ---


def test_empty_inputs():
    embedder = FakeEmbedder()
    assert dedup([], [q("旧题", 1)], embedder) == []
    new = [q("新题")]
    assert dedup(new, [], embedder) == new
    assert embedder.calls == []


def test_existing_embeddings_reused_no_encode(file_db):
    """旧题已有 embedding 时不重新 encode（增量性能）。"""
    old = persist(file_db, q("旧题"))
    old.embedding = np.asarray(vec(1), dtype=np.float32).tobytes()
    new = [q("新题")]
    embedder = FakeEmbedder(vectors={"新题": vec(0, 1)})
    dedup(new, [old], embedder)
    assert embedder.calls == [["新题"]]  # 只 encode 新题，未重复算旧题
    assert dedup(new, [old], embedder) == new  # 正交向量不判重


def test_missing_old_embeddings_backfilled(file_db):
    """旧题 embedding 为 NULL 时自动补算（首次全量）。"""
    old = persist(file_db, q("旧题"))
    new = [q("新题")]
    embedder = FakeEmbedder(vectors={"旧题": vec(1), "新题": vec(0, 1)})
    kept = dedup(new, [old], embedder)
    assert kept == new
    assert old.embedding is not None  # 补算并写回对象


# --- fail（降级：embedding 不可用 → 仅哈希精确去重） ---


def test_embedder_unavailable_degrades_to_hash_only(file_db):
    old = persist(file_db, q("讲一下 HashMap"))
    new = [q("讲一下  HashMap"), q("完全不同的题")]
    embedder = FakeEmbedder(error=EmbedError("model load failed"))
    assert dedup(new, [old], embedder) == [new[1]]
    assert len(embedder.calls) == 1


def test_degrade_keeps_semantic_similar_without_embedding(file_db):
    """降级时语义近似（非字面相同）放过——宁重复勿丢题。"""
    old = persist(file_db, q("讲一下 HashMap 底层原理"))
    new = [q("讲讲 HashMap 的实现细节")]
    embedder = FakeEmbedder(error=EmbedError("down"))
    assert dedup(new, [old], embedder) == new


def test_multiple_new_duplicates_of_same_old_all_dropped(file_db):
    old = persist(file_db, q("旧题：Redis 持久化"))
    new = [q("新题 A：Redis AOF"), q("新题 B：Redis RDB")]
    embedder = FakeEmbedder(vectors={
        "旧题：Redis 持久化": vec(1),
        "新题 A：Redis AOF": vec(1),
        "新题 B：Redis RDB": vec(1),
    })
    assert dedup(new, [old], embedder) == []


# --- 幂等 ---


def test_dedup_idempotent(file_db):
    old_a = persist(file_db, q("旧题 A"))
    old_b = persist(file_db, q("旧题 B"))
    old = [old_a, old_b]
    new = [q("旧题 A"), q("完全不同的新题")]
    embedder = FakeEmbedder()
    first = dedup(new, old, embedder)
    second = dedup(new, old, embedder)
    assert [x.stem for x in first] == [x.stem for x in second]
