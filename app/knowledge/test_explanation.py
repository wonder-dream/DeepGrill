"""讲解的按需生成与缓存（决策 67）。

四条断言，对应四种失败方式：

· **命中缓存就不再调模型** —— 否则"缓存"只是看起来在省钱的装饰
· **换了 prompt 或换了题干，缓存必须失效** —— 版本是算出来的，不是手填的
· **模型失败不写缓存、也不返回空讲解** —— 空讲解一旦落库会变成一个"可信的坏数据"，
  之后所有人打开都看到空的，而且缓存命中不报错
· **回收者真的清**（§3.2）—— 这张表随题目数增长，没有回收者就是泄漏
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Domain, Explanation, KnowledgePoint, Question
from app.knowledge import explanation
from app.llm import LLMCallError
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply


@pytest.fixture
def session(tmp_dir: Path) -> Session:
    db = tmp_dir / "explain.db"
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


def _question(session: Session) -> Question:
    return session.get(Question, 1)


def _rows(session: Session) -> list[Explanation]:
    return list(session.query(Explanation).all())


# ---------------------------------------------------------------------------
# 生成与缓存
# ---------------------------------------------------------------------------
def test_generates_once_then_reads_the_cache(session: Session) -> None:
    llm = FakeLLM().queue_text("volatile 讲的是可见性与有序性……")
    first = explanation.explain(session, question=_question(session), llm=llm)
    assert first.cached is False
    assert first.body.startswith("volatile 讲的是")

    # 换一个"没有配额"的替身：如果它被调用，会抛 LLMError（队列空）
    second = explanation.explain(session, question=_question(session), llm=FakeLLM())
    assert second.cached is True
    assert second.body == first.body
    assert len(_rows(session)) == 1


def test_cache_lookup_is_read_only(session: Session) -> None:
    """页面渲染路径会调 `cached()` —— 它**不许**有副作用（不生成、不写库）。"""
    assert explanation.cached(session, question=_question(session)) is None
    assert _rows(session) == []


def test_prompt_version_change_invalidates_the_cache(session: Session, monkeypatch) -> None:
    """改了 prompt 就必须重新生成 —— 否则"改了 prompt 为什么讲解没变"没有答案。"""
    llm = FakeLLM().queue_text("旧版讲解")
    explanation.explain(session, question=_question(session), llm=llm)
    assert explanation.cached(session, question=_question(session)) is not None

    monkeypatch.setattr(explanation, "PROMPT_VERSION", "explain-2")
    assert explanation.cached(session, question=_question(session)) is None

    llm2 = FakeLLM().queue_text("新版讲解")
    fresh = explanation.explain(session, question=_question(session), llm=llm2)
    assert fresh.cached is False and fresh.body == "新版讲解"
    assert len(_rows(session)) == 1, "旧版本要被清掉（留着只占容量）"


def test_stem_change_invalidates_the_cache(session: Session) -> None:
    """改了题干，旧讲解不该被当成新题干的讲解。"""
    explanation.explain(session, question=_question(session),
                        llm=FakeLLM().queue_text("针对旧题干"))
    _question(session).stem = "说说 volatile 与 synchronized 的区别"
    session.flush()

    assert explanation.cached(session, question=_question(session)) is None


def test_version_is_derived_not_hand_maintained(session: Session) -> None:
    """版本是**算出来的**（prompt 版本 + 题干哈希），不是有人手填的字符串。"""
    version = explanation.version_of(_question(session))
    assert version.startswith(explanation.PROMPT_VERSION)
    assert len(version.split(":")[1]) == 12, "题干哈希"


def test_whitespace_only_stem_change_does_not_invalidate(session: Session) -> None:
    """题干只多了一个空格不该让讲解作废（哈希前会 strip）。"""
    before = explanation.version_of(_question(session))
    _question(session).stem = "  说说 volatile 的作用  "
    assert explanation.version_of(_question(session)) == before


# ---------------------------------------------------------------------------
# 失败
# ---------------------------------------------------------------------------
def test_model_failure_raises_and_writes_nothing(session: Session) -> None:
    """失败**不写缓存**、不返回空讲解 —— 交给调用方显示"没生成出来"。"""
    llm = FakeLLM().queue(FakeReply(error=LLMCallError("模型挂了")))
    with pytest.raises(LLMCallError):
        explanation.explain(session, question=_question(session), llm=llm)
    assert _rows(session) == [], "失败不该留下任何东西（下次能重试）"


def test_empty_model_output_is_treated_as_a_failure(session: Session) -> None:
    """空输出**必须算失败**：一旦落库，它就是一个所有人都会读到的"可信的坏数据"。"""
    llm = FakeLLM().queue_text("   ")
    with pytest.raises(Exception):  # LLMError
        explanation.explain(session, question=_question(session), llm=llm)
    assert _rows(session) == []


def test_prompt_renders_without_leftover_placeholders(session: Session) -> None:
    """prompt 里的每个占位符都要被填上（未填的会抛 —— 这里确认它不会）。"""
    from app.llm import prompts

    text = prompts.load("knowledge/explain_question.md").render(
        stem="说点什么", kind="knowledge", criteria="1. 可见性", reference_answer=""
    )
    assert "{{" not in text
    assert "说点什么" in text and "可见性" in text


# ---------------------------------------------------------------------------
# 回收者（§3.2）
# ---------------------------------------------------------------------------
def test_purge_removes_only_rows_past_the_ttl(session: Session) -> None:
    explanation.explain(session, question=_question(session), llm=FakeLLM().queue_text("新"))
    old = _rows(session)[0]
    old.created_at = (datetime.now(UTC) - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    session.add(Question(id=2, kind="knowledge", stem="另一道题", difficulty=3,
                         origin="seed", visibility="public"))
    session.flush()
    explanation.explain(session, question=session.get(Question, 2),
                        llm=FakeLLM().queue_text("另一条的讲解"))

    report = explanation.purge(session, ttl_days=180)
    assert report.by_age == 1
    remaining = _rows(session)
    assert len(remaining) == 1 and remaining[0].question_id == 2


def test_purge_enforces_the_capacity_cap(session: Session) -> None:
    """容量上限：超了就丢最旧的 —— 否则这张表只增不减。"""
    for qid in range(1, 5):
        if qid != 1:
            session.add(Question(id=qid, kind="knowledge", stem=f"题 {qid}", difficulty=3,
                                 origin="seed", visibility="public"))
    session.flush()
    for qid in range(1, 5):
        explanation.explain(session, question=session.get(Question, qid),
                            llm=FakeLLM().queue_text(f"讲解 {qid}"))
        _rows(session)[-1].created_at = f"2026-01-0{qid} 00:00:00"
    session.flush()

    report = explanation.purge(session, ttl_days=9999, max_rows=2)
    assert report.by_capacity == 2
    left = {r.question_id for r in _rows(session)}
    assert left == {3, 4}, "留最新的两个"


def test_purge_is_idempotent(session: Session) -> None:
    """幂等 —— 所以它能做成会被重跑的离线任务。"""
    explanation.explain(session, question=_question(session), llm=FakeLLM().queue_text("x"))
    # `ttl_days=-1` = 全部算过期（用 0 不行：created_at 与现在同一秒，`<` 比不出来）
    first = explanation.purge(session, ttl_days=-1)
    second = explanation.purge(session, ttl_days=-1)
    assert first.total >= 1
    assert second.total == 0


def test_purge_is_registered_as_an_offline_task(session: Session) -> None:
    """它在**队列的注册表**里 —— 回收者要真的有人跑，不能只是个函数。"""
    from app.offline import jobs, tasks  # noqa: F401  （tasks 在 import 时注册）

    assert "purge_explanations" in jobs.TASKS
    assert jobs.TASKS["purge_explanations"].idempotent is True
    result = jobs.TASKS["purge_explanations"].run(session, {})
    assert "讲解缓存" in result["message"]
