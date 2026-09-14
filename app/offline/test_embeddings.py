"""嵌入层与缓存层的测试（ADR-0008 + 决策 44）。

三层各测各的失败方式：

· **接口层**：没有供应商时**明确失败**（不是返回空向量 —— 那会让聚类"跑通"了却毫无
  意义）；假实现必须**确定性**且"用词相近 → 向量相近"（否则聚类测试只能断言"没崩"）
· **HTTP 客户端**：重试纪律、4xx 不重试、**只认返回体的 index**（协议没保证顺序）、
  缺向量要炸（少一条会让"哪条文本对应哪个向量"整体错位）、用量进账本
· **缓存层**：命中不重复调用、**文本变了自动失效**、打包/解包往返无损、回收者
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Domain, Embedding, KnowledgePoint, Question
from app.llm.embeddings import (
    APIEmbeddings,
    EmbeddingError,
    EmbeddingUnavailable,
    FakeEmbeddings,
    NoProviderEmbeddings,
    fake_vector,
)
from app.offline import embedding_store
from migrations._runner import migrate


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------
def test_no_provider_fails_loudly() -> None:
    """没配供应商 → **明确失败**。返回空向量会让整条聚类"跑通"却毫无意义。"""
    with pytest.raises(EmbeddingUnavailable) as e:
        NoProviderEmbeddings().embed(["随便一段文本"])
    assert "embeddings" in str(e.value) or "嵌入" in str(e.value)


def test_fake_embeddings_are_deterministic() -> None:
    fake = FakeEmbeddings()
    assert fake.embed(["线程池的拒绝策略"]) == fake.embed(["线程池的拒绝策略"])


def test_fake_embeddings_put_similar_texts_close() -> None:
    """**这是它当代替品的前提**：用词重叠多的两段文本相似度高。

    没有这条性质，聚类、阈值、召回这些逻辑就只能测"没崩"——
    而它们恰恰是这一层存在的理由。
    """
    from app.offline.knowledge_pipeline import cosine

    fake = FakeEmbeddings()
    a, b, c = fake.embed(
        [
            "说说 volatile 的作用与边界",
            "volatile 的作用是什么？边界在哪？",
            "线程池的拒绝策略怎么选",
        ]
    )
    assert cosine(a, b) > cosine(a, c)
    assert cosine(a, b) > 0.3


def test_fake_vector_is_l2_normalized() -> None:
    import math

    vector = fake_vector("说说 volatile 的作用")
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)


def test_fake_vector_of_empty_text_is_all_zero() -> None:
    """空文本得到零向量 —— `cosine` 会把它当"自己一簇"，而不是让整轮炸掉。"""
    assert set(fake_vector("   ")) == {0.0}


def test_fake_embeddings_count_their_calls() -> None:
    """调用次数可数 —— 缓存的效果要能被断言（"重跑没再花钱"）。"""
    fake = FakeEmbeddings()
    fake.embed(["a", "b"])
    fake.embed(["c"])
    assert fake.calls == 2


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------
def _client(handler, **kw) -> APIEmbeddings:
    return APIEmbeddings(
        api_key="k",
        base_url="https://example.invalid/v1",
        model="m",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def _body(vectors: list[list[float]], *, indexes: list[int] | None = None) -> dict:
    """构造返回体。`indexes` 用来测"顺序被打乱"与"缺了一条"两种情况。

    ⚠️ `zip` 这里**故意不加 `strict=`**：其中一个用例就是要造出"少了一条向量"的
    畸形返回体（`indexes` 比 `vectors` 短），严格模式会在**测试自己**这里先炸。
    """
    idx = indexes if indexes is not None else list(range(len(vectors)))
    return {
        "data": [
            {"index": i, "embedding": v}
            for i, v in zip(idx, vectors)  # noqa: B905  （见上面的说明）
        ],
        "usage": {"prompt_tokens": 12},
    }


def test_api_reads_vectors_by_index_not_by_order() -> None:
    """**只认 `index`**：协议没保证返回顺序，按位置取会在某些供应商上静默错位。"""
    client = _client(lambda r: httpx.Response(200, json=_body([[1.0, 0.0], [0.0, 1.0]],
                                                             indexes=[1, 0])))
    assert client.embed(["第一段", "第二段"]) == [[0.0, 1.0], [1.0, 0.0]]


def test_api_batches_long_inputs() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(len(payload["input"]))
        return httpx.Response(200, json=_body([[0.1, 0.2]] * len(payload["input"])))

    client = _client(handler, batch_size=2)
    out = client.embed(["a", "b", "c", "d", "e"])
    assert len(out) == 5
    assert seen == [2, 2, 1]


def test_api_missing_vector_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """少一条向量 → 当场炸。否则"哪条文本对应哪个向量"会整体错位。"""
    monkeypatch.setattr("app.llm.embeddings.time.sleep", lambda _s: None)
    client = _client(lambda r: httpx.Response(200, json=_body([[1.0], [2.0]], indexes=[0])))
    with pytest.raises(EmbeddingError):
        client.embed(["a", "b"])


def test_api_retries_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.embeddings.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=_body([[1.0, 0.0]]))

    assert _client(handler).embed(["a"]) == [[1.0, 0.0]]
    assert calls["n"] == 2


def test_api_does_not_retry_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.embeddings.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="no such endpoint")

    with pytest.raises(EmbeddingError):
        _client(handler).embed(["a"])
    assert calls["n"] == 1, "4xx 是请求本身的问题（DeepSeek 上就是 404）"


def test_api_counts_usage() -> None:
    client = _client(lambda r: httpx.Response(200, json=_body([[1.0]])))
    client.embed(["a"])
    assert client.usage_total["calls"] == 1
    assert client.usage_total["prompt_tokens"] == 12


def test_api_requires_a_key() -> None:
    with pytest.raises(EmbeddingError):
        APIEmbeddings(api_key="", base_url="https://example.invalid", model="m")


# ---------------------------------------------------------------------------
# 缓存层
# ---------------------------------------------------------------------------
@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "embed.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile 的作用", difficulty=3,
                       primary_point_id=1, origin="seed", visibility="public"))
        s.commit()
        yield s


def test_pack_unpack_round_trip() -> None:
    vector = [0.5, -1.25, 3.0, 0.0]
    assert embedding_store.unpack_vector(embedding_store.pack_vector(vector)) == pytest.approx(
        vector
    )


def test_vectors_are_cached_and_reused(session: Session) -> None:
    fake = FakeEmbeddings()
    items = {"1": "说说 volatile 的作用"}

    first, report = embedding_store.vectors_for(
        session, items=items, kind=embedding_store.QUESTION, embeddings=fake
    )
    assert report.embedded == 1 and report.reused == 0

    second, report2 = embedding_store.vectors_for(
        session, items=items, kind=embedding_store.QUESTION, embeddings=fake
    )
    assert report2.reused == 1 and report2.embedded == 0
    # ⚠️ 用 `approx`：缓存里存的是 **float32**（1536 维下比 JSON 数组小五倍），
    # 所以第二次读回来的向量与第一次算出来的差在 1e-7 量级。这个差别对聚类
    # （阈值 0.86）毫无影响，但用 `==` 比会红 —— 那是测试写错了，不是实现错了。
    assert second["1"] == pytest.approx(first["1"], rel=1e-6)
    assert fake.calls == 1, "第二次不该再调供应商 —— 否则缓存只是装饰"


def test_changed_text_invalidates_the_cache(session: Session) -> None:
    """文本变了（题干改了）→ 自动重算。这是 `source_hash` 那一列的全部意义。"""
    fake = FakeEmbeddings()
    embedding_store.vectors_for(
        session, items={"1": "旧题干"}, kind=embedding_store.QUESTION, embeddings=fake
    )
    _, report = embedding_store.vectors_for(
        session, items={"1": "新题干"}, kind=embedding_store.QUESTION, embeddings=fake
    )
    assert report.embedded == 1 and report.reused == 0


def test_changed_model_invalidates_the_cache(session: Session) -> None:
    """换了嵌入模型 → 旧向量不能再用（不同模型的向量不可比）。"""
    first = FakeEmbeddings(model="model-a")
    embedding_store.vectors_for(
        session, items={"1": "文本"}, kind=embedding_store.QUESTION, embeddings=first
    )
    second = FakeEmbeddings(model="model-b")
    _, report = embedding_store.vectors_for(
        session, items={"1": "文本"}, kind=embedding_store.QUESTION, embeddings=second
    )
    assert report.embedded == 1 and report.reused == 0
    assert session.get(Embedding, (embedding_store.QUESTION, "1")).model == "model-b"


def test_question_text_includes_criteria(session: Session) -> None:
    """嵌入的是**题干 + 考察点**：换一组考察点意味着这道题考的东西变了。"""
    from app.bank import repository

    question = session.get(Question, 1)
    assert question is not None
    criteria = repository.criteria_of_question(session, question)
    text = embedding_store.question_text(question, criteria)
    assert "说说 volatile" in text and "可见性" in text


def test_mismatched_vector_count_is_an_error(session: Session) -> None:
    class Bad:
        model = "bad"

        def embed(self, texts):
            return [[1.0]]  # 少一条

    with pytest.raises(ValueError):
        embedding_store.vectors_for(
            session, items={"a": "1", "b": "2"}, kind=embedding_store.QUESTION, embeddings=Bad()
        )


def test_purge_by_age_and_capacity(session: Session) -> None:
    fake = FakeEmbeddings()
    for i in range(4):
        embedding_store.vectors_for(
            session, items={f"q{i}": f"文本 {i}"},
            kind=embedding_store.QUESTION, embeddings=fake,
        )
    session.flush()
    rows = list(session.execute(select(Embedding)).scalars())
    # 手工把时间拉开：created_at 是秒级，同一轮写入的时间戳是一样的
    for index, row in enumerate(rows):
        row.created_at = f"2026-01-0{index + 1} 00:00:00"
    session.flush()

    report = embedding_store.purge(session, ttl_days=9999, max_rows=2)
    assert report.by_capacity >= 2
    assert len(list(session.execute(select(Embedding)).scalars())) <= 2

    aged = embedding_store.purge(session, ttl_days=-1)
    assert aged.by_age >= 1
    assert list(session.execute(select(Embedding)).scalars()) == []


def test_purge_is_registered_as_an_offline_task(session: Session) -> None:
    from app.offline import jobs, tasks  # noqa: F401

    assert "purge_embeddings" in jobs.TASKS
    outcome = jobs.TASKS["purge_embeddings"].run(session, {})
    assert outcome is not None
    assert "嵌入缓存" in outcome["message"]
