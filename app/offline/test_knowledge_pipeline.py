"""知识层构建管道的测试（决策 26 / 44 / 46）。

三条最要紧的性质：

· **判据① 是可执行的**："写不出考察点就不是知识点"由 `Candidate.unusable_reason`
  挡住，被挡下的候选进 `skipped`（**不静默丢弃**）
· **挂不上不新建**（决策 46）：模型给 `null` 或给一个不存在的 id，题就留在待定池，
  **绝不允许它悄悄改知识地图**
· **挂载不覆盖已确认的挂载**：改挂载是自修复的职责，一次性装配不该顺手改掉
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Domain, KnowledgePoint, Question
from app.llm import LLMCallError
from app.offline import knowledge_pipeline as kp
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "kp.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        for qid, stem in (
            (1, "说说 volatile 的作用，它能保证原子性吗？"),
            (2, "volatile 为什么能保证可见性？底层怎么实现的？"),
            (3, "线程池的核心线程数和最大线程数有什么区别？"),
        ):
            s.add(
                Question(id=qid, kind="knowledge", stem=stem, difficulty=3,
                         origin="seed", visibility="public")
            )
        s.commit()
        yield s


def _cand(name: str, criteria: list[str], qids: list[int] | None = None) -> dict:
    return {"name": name, "definition": f"考 {name}", "exclusions": "不考别的",
            "criteria": criteria, "question_ids": qids or [1]}


def test_propose_extracts_and_folds_duplicates(session: Session) -> None:
    """① 提候选 + 折叠同名（归一化："volatile " 与 "Volatile" 是同一条）。"""
    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(
        FakeReply(data={"points": [
            _cand("volatile", ["可见性与有序性", "底层内存屏障"], [1]),
            _cand("Volatile ", ["不保证原子性"], [2]),          # 归一化后同名
            _cand("线程池参数", ["核心与最大线程数", "拒绝策略"], [3]),
        ]})
    )
    result = kp.propose_points(questions, llm=llm)

    assert not result.llm_failed
    names = sorted(c.name for c in result.candidates)
    assert names == ["volatile", "线程池参数"], "同名候选要折成一条"
    volatile = next(c for c in result.candidates if kp.normalize_name(c.name) == "volatile")
    assert sorted(volatile.question_ids) == [1, 2], "折叠要并集题目"
    assert set(volatile.criteria) == {"可见性与有序性", "底层内存屏障", "不保证原子性"}


def test_normalize_name_is_idempotent_and_shape_insensitive() -> None:
    assert kp.normalize_name("Volatile") == kp.normalize_name(" volatile ")
    assert kp.normalize_name("线程池（参数）") == kp.normalize_name("线程池参数")
    assert kp.normalize_name("A-B") == kp.normalize_name("a b")


def test_candidate_without_enough_criteria_is_not_a_point(session: Session) -> None:
    """**判据① 的机器可判部分**：写不出考察点就不是知识点。"""
    bad = kp.Candidate(name="Java 并发", criteria=["理解并发"])
    assert bad.unusable_reason is not None
    assert "考察点" in bad.unusable_reason

    good = kp.Candidate(name="volatile", criteria=["可见性与有序性", "底层内存屏障"])
    assert good.unusable_reason is None


def test_apply_review_creates_confirmed_points_and_criteria(session: Session) -> None:
    """人审通过 → 建 `confirmed` 知识点 + 考察点（人审过才是 confirmed，决策 26）。"""
    candidates = [
        kp.Candidate(name="volatile", definition="考 volatile 的语义",
                     criteria=["可见性与有序性", "底层内存屏障"], question_ids=[1]),
        kp.Candidate(name="线程池参数", criteria=["核心与最大线程数", "拒绝策略"], question_ids=[3]),
    ]
    result = kp.apply_review(
        session,
        candidates=candidates,
        decisions=[kp.Decision(0, "approve"), kp.Decision(1, "approve")],
        domain_name="Java 并发",
    )
    session.commit()

    assert result.created_points == 2
    points = session.execute(select(KnowledgePoint)).scalars().all()
    assert {p.name for p in points} == {"volatile", "线程池参数"}
    assert all(p.status == "confirmed" for p in points)
    assert all(p.origin == "proposed" for p in points)
    volatile = next(p for p in points if p.name == "volatile")
    criteria = session.execute(select(Criterion).where(Criterion.point_id == volatile.id)).scalars().all()
    assert len(criteria) == 2
    # 题目被挂上了（只填空的）
    assert session.get(Question, 1).primary_point_id == volatile.id


def test_apply_review_reports_what_machine_rules_blocked(session: Session) -> None:
    """被判据① 挡下的候选进 `skipped` —— **不静默丢弃**。"""
    candidates = [kp.Candidate(name="Java 并发", criteria=["理解并发"], question_ids=[1])]
    result = kp.apply_review(
        session, candidates=candidates,
        decisions=[kp.Decision(0, "approve")], domain_name="Java 并发",
    )
    assert result.created_points == 0
    assert result.skipped and "Java 并发" in result.skipped[0]


def test_apply_review_can_rename_and_merge(session: Session) -> None:
    """人审能改名、能合并 —— 合并时被并的题目要保全。"""
    candidates = [
        kp.Candidate(name="volatile", criteria=["可见性", "内存屏障"], question_ids=[1]),
        kp.Candidate(name="volatile 语义", criteria=["不保证原子性"], question_ids=[2]),
    ]
    result = kp.apply_review(
        session,
        candidates=candidates,
        decisions=[
            kp.Decision(0, "approve", name="volatile"),
            kp.Decision(1, "merge_into", merge_into=0),
        ],
        domain_name="Java 并发",
    )
    session.commit()

    assert result.created_points == 1 and result.merged == 1
    point = session.execute(select(KnowledgePoint)).scalars().one()
    assert point.name == "volatile"
    # 被合并那条的题也要挂到目标上（题目不能丢）
    assert session.get(Question, 1).primary_point_id == point.id
    assert session.get(Question, 2).primary_point_id == point.id


def test_apply_review_reject_creates_nothing(session: Session) -> None:
    candidates = [kp.Candidate(name="volatile", criteria=["可见性", "内存屏障"], question_ids=[1])]
    result = kp.apply_review(
        session, candidates=candidates,
        decisions=[kp.Decision(0, "reject")], domain_name="Java 并发",
    )
    session.commit()
    assert result.created_points == 0 and result.rejected == 1
    assert session.execute(select(KnowledgePoint)).scalars().all() == []


def test_mount_assigns_only_known_points(session: Session) -> None:
    session.add(Domain(id=1, name="Java 并发"))
    session.add(KnowledgePoint(id=7, domain_id=1, name="volatile", status="confirmed"))
    session.commit()

    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(
        FakeReply(data={"assignments": [
            {"question_id": 1, "point_id": 7, "confidence": "high"},
            {"question_id": 2, "point_id": 7, "confidence": "medium"},
        ]})
    )
    result = kp.mount_questions(session, questions=questions[:2], llm=llm)
    session.commit()

    assert result.mounted == 2
    assert session.get(Question, 1).primary_point_id == 7


def test_mount_leaves_unknown_targets_for_review_and_never_creates_points(session: Session) -> None:
    """**决策 46**：挂不上就进待定池，**绝不允许自动新建知识点**。"""
    session.add(Domain(id=1, name="Java 并发"))
    session.add(KnowledgePoint(id=7, domain_id=1, name="volatile", status="confirmed"))
    session.commit()

    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(
        FakeReply(data={"assignments": [
            {"question_id": 1, "point_id": 999},   # 不存在的知识点
            {"question_id": 2, "point_id": None},  # 模型自己说挂不上
            # 题 3 干脆没出现在结果里
        ]})
    )
    result = kp.mount_questions(session, questions=questions, llm=llm)
    session.commit()

    assert result.mounted == 0
    assert result.left_for_review == 3
    assert len(session.execute(select(KnowledgePoint)).scalars().all()) == 1, "不许新建知识点"
    assert all(q.primary_point_id is None for q in session.execute(select(Question)).scalars())


def test_mount_does_not_overwrite_existing_mounts(session: Session) -> None:
    """改挂载是**自修复**的职责；一次性装配不该顺手改掉已确认的挂载。"""
    session.add(Domain(id=1, name="Java 并发"))
    session.add(KnowledgePoint(id=7, domain_id=1, name="volatile", status="confirmed"))
    session.add(KnowledgePoint(id=8, domain_id=1, name="线程池参数", status="confirmed"))
    session.commit()
    q = session.get(Question, 1)
    assert q is not None
    q.primary_point_id = 8
    session.commit()

    llm = FakeLLM().queue(FakeReply(data={"assignments": [{"question_id": 1, "point_id": 7}]}))
    result = kp.mount_questions(session, questions=[q], llm=llm)
    session.commit()

    assert result.mounted == 0, "已挂载的题不该被重新挂"
    assert session.get(Question, 1).primary_point_id == 8


def test_mount_failure_sends_the_batch_to_review(session: Session) -> None:
    session.add(Domain(id=1, name="Java 并发"))
    session.add(KnowledgePoint(id=7, domain_id=1, name="volatile", status="confirmed"))
    session.commit()

    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(FakeReply(error=LLMCallError("模型挂了")))
    result = kp.mount_questions(session, questions=questions, llm=llm)

    assert result.mounted == 0
    assert result.left_for_review == len(questions), "整批进待定池，而不是静默丢掉"
    assert all(q.primary_point_id is None for q in session.execute(select(Question)).scalars())


def test_mount_without_confirmed_points_does_nothing(session: Session) -> None:
    """一个已确认的知识点都没有时**不调模型** —— 没有可挂的目标，调了也是浪费。"""
    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM()
    result = kp.mount_questions(session, questions=questions, llm=llm)
    assert llm.calls == []
    assert result.mounted == 0


def test_propose_failure_is_reported_not_swallowed(session: Session) -> None:
    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(FakeReply(error=LLMCallError("模型挂了")))
    result = kp.propose_points(questions, llm=llm)
    assert result.llm_failed is True
    assert result.note


def test_prerequisite_edges_are_deliberately_not_implemented() -> None:
    """决策 48：从名字推边基本在猜，**故意留空**。

    这条测试的意义是让"为什么没有前置边"这件事**可执行地被看见** ——
    将来有人实现它时，这条会红，于是他必须去读 `derive_edges` 的 docstring
    （那里写了决策 48 要求的两类有依据的边）。
    """
    assert kp.derive_edges() == []
