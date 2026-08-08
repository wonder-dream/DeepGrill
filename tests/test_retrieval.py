import math

import pytest

from app.db import commit, get_session, init_db
from app.models import (
    Attempt,
    Judgment,
    Question,
    QuestionType,
    Session,
    SessionKind,
    SessionStatus,
    Source,
    SourceType,
)
from app.retrieval import (
    HIGH_SCORE,
    TOP_K,
    build_reference,
    search_questions,
)
from tests.fakes import FakeEmbedder

DIM = 64


@pytest.fixture
def file_db(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    with get_session() as session:
        yield session


def q(stem, tags=None):
    return Question(
        source_id=1, type=QuestionType.knowledge, stem=stem, tags=tags or []
    )


def vec(*xs):
    xs = list(xs)
    while len(xs) < DIM:
        xs.append(0.0)
    norm = math.sqrt(sum(x * x for x in xs))
    return [x / norm for x in xs]


def persist_question(db, stem, tags=None):
    source = Source(type=SourceType.manual, source_hash=f"h-{stem}")
    db.add(source)
    commit(db)
    db.refresh(source)
    qq = Question(
        source_id=source.id, type=QuestionType.knowledge, stem=stem, tags=tags or []
    )
    db.add(qq)
    commit(db)
    db.refresh(qq)
    return qq


def add_high_score_session(db, question, answer_text="高分回答内容", score=90):
    s = Session(question_id=question.id, kind=SessionKind.open, status=SessionStatus.finished)
    db.add(s)
    commit(db)
    db.refresh(s)
    db.add(Attempt(session_id=s.id, round_no=0, answer_text=answer_text))
    db.add(
        Judgment(
            session_id=s.id,
            scores={"accuracy": 90, "completeness": 90, "clarity": 90, "depth": 90},
            total_score=score,
            reference_answer="模型参考答案",
        )
    )
    commit(db)


# --- search_questions ---


def test_text_search_by_shared_terms():
    pool = [
        q("讲一下 KV Cache 的原理", ["KV Cache"]),
        q("讲一下 Redis 持久化", ["Redis"]),
        q("设计一个短链接系统", ["系统设计"]),
    ]
    hits = search_questions("请讲讲 KV Cache 是什么", pool, method="text", k=2)
    assert hits[0].stem == "讲一下 KV Cache 的原理"
    assert len(hits) == 1  # 其余题零重叠分被过滤


def test_vector_search_by_embedding():
    pool = [
        q("旧题 A：RAG 检索优化", ["RAG"]),
        q("旧题 B：JVM 调优", ["JVM"]),
    ]
    embedder = FakeEmbedder(
        vectors={
            "旧题 A：RAG 检索优化": vec(1),
            "旧题 B：JVM 调优": vec(0, 1),
            "查询：RAG 召回策略": vec(1),
        }
    )
    hits = search_questions(
        "查询：RAG 召回策略", pool, method="vector", k=1, embedder=embedder
    )
    assert hits[0].stem == "旧题 A：RAG 检索优化"


def test_hybrid_combines_text_and_vector():
    pool = [
        q("讲一下 KV Cache", ["KV Cache"]),
        q("讲一下 Redis", ["Redis"]),
    ]
    embedder = FakeEmbedder(
        vectors={
            "讲一下 KV Cache": vec(1),
            "讲一下 Redis": vec(0, 1),
            "查询：KV Cache 缓存": vec(1),
        }
    )
    hits = search_questions("查询：KV Cache 缓存", pool, method="hybrid", k=2, embedder=embedder)
    assert hits[0].stem == "讲一下 KV Cache"


def test_search_empty_pool():
    assert search_questions("任何", []) == []
    assert search_questions("任何", [], method="vector", embedder=FakeEmbedder()) == []


def test_text_search_no_match_returns_empty():
    pool = [q("讲一下 Redis 持久化", ["Redis"])]
    assert search_questions("如何设计微服务网关", pool, method="text") == []


# --- build_reference ---


def test_build_reference_uses_high_score_attempts(file_db):
    old = persist_question(file_db, "讲一下 KV Cache 的原理", ["KV Cache"])
    add_high_score_session(file_db, old, answer_text="真实高分回答：缓存复用计算")
    current = q("请讲讲 KV Cache 的实现", ["KV Cache"])
    embedder = FakeEmbedder(
        vectors={
            "讲一下 KV Cache 的原理": vec(1),
            "请讲讲 KV Cache 的实现": vec(1),
        }
    )
    ref = build_reference(current, [old], method="hybrid", embedder=embedder)
    assert ref is not None
    assert "真实高分回答" in ref  # 真实回答优先
    assert "模型参考答案" not in ref


def test_build_reference_falls_back_to_reference_answer(file_db):
    old = persist_question(file_db, "讲一下 KV Cache 的原理", ["KV Cache"])
    s = Session(question_id=old.id, kind=SessionKind.open, status=SessionStatus.finished)
    file_db.add(s)
    commit(file_db)
    file_db.refresh(s)
    file_db.add(
        Judgment(
            session_id=s.id,
            scores={"accuracy": 90, "completeness": 90, "clarity": 90, "depth": 90},
            total_score=90,
            reference_answer="模型生成的标准参考答案",
        )
    )
    commit(file_db)
    current = q("请讲讲 KV Cache 的实现", ["KV Cache"])
    embedder = FakeEmbedder(
        vectors={
            "讲一下 KV Cache 的原理": vec(1),
            "请讲讲 KV Cache 的实现": vec(1),
        }
    )
    ref = build_reference(current, [old], method="hybrid", embedder=embedder)
    assert "模型生成的标准参考答案" in ref


def test_build_reference_none_without_high_score(file_db):
    old = persist_question(file_db, "讲一下 KV Cache 的原理", ["KV Cache"])
    s = Session(question_id=old.id, kind=SessionKind.open, status=SessionStatus.finished)
    file_db.add(s)
    commit(file_db)
    file_db.add(
        Judgment(
            session_id=s.id,
            scores={"accuracy": 50, "completeness": 50, "clarity": 50, "depth": 50},
            total_score=55,
            reference_answer="低分回答",
        )
    )
    commit(file_db)
    current = q("请讲讲 KV Cache 的实现", ["KV Cache"])
    embedder = FakeEmbedder(
        vectors={
            "讲一下 KV Cache 的原理": vec(1),
            "请讲讲 KV Cache 的实现": vec(1),
        }
    )
    assert build_reference(current, [old], method="hybrid", embedder=embedder) is None
