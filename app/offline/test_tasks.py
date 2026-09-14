"""离线任务的测试（自修复信号 + 回收者入口）。

两条要紧的：
· **自修复只标记、不改挂载** —— 自动改挂载会让"这道题为什么换了知识点"没人能解释
· **幂等** —— 重跑是常态（心跳超时回退），跑两遍不能累积出两批标记
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Criterion,
    Domain,
    KnowledgePoint,
    Question,
    QuestionFlag,
    QuestionPointStat,
)
from app.offline import jobs, tasks
from migrations._runner import migrate


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "tasks.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Question(id=1, kind="knowledge", stem="题一", difficulty=3,
                       primary_point_id=1, origin="seed", visibility="public"))
        s.add(Question(id=2, kind="knowledge", stem="题二", difficulty=3,
                       primary_point_id=1, origin="seed", visibility="public"))
        s.commit()
        yield s


def _stats(session: Session, question_id: int, hit: int, miss: int) -> None:
    session.add(
        QuestionPointStat(
            question_id=question_id, point_id=1, criterion_id=1,
            hit_count=hit, miss_count=miss,
        )
    )
    session.commit()


def test_worker_entry_registers_tasks() -> None:
    """`python -m app.offline.worker` 必须真的注册到任务 —— 否则它空转。"""
    assert "self_repair_stats" in jobs.TASKS
    assert jobs.TASKS["self_repair_stats"].idempotent is True


def test_flags_a_question_whose_criteria_are_never_hit(session: Session) -> None:
    """样本够 + 未命中率高 → 标成挂载可疑。"""
    _stats(session, question_id=1, hit=0, miss=10)   # 10 次全没答到
    result = tasks.self_repair_stats(session, {})
    session.commit()

    assert result["flagged"] and result["flagged"][0]["question_id"] == 1
    flags = session.execute(select(QuestionFlag)).scalars().all()
    assert len(flags) == 1
    assert flags[0].kind == "suspect_mount"
    assert "未命中" in flags[0].detail
    assert "volatile" in flags[0].detail, "要指出它现在挂在哪个知识点下"


def test_does_not_flag_with_too_few_samples(session: Session) -> None:
    """**判据③ 的局限**：样本不够时不要乱动（决策 26 明说它需要样本量）。"""
    _stats(session, question_id=1, hit=0, miss=2)    # 只考过 2 次
    result = tasks.self_repair_stats(session, {})
    assert result["flagged"] == []


def test_does_not_flag_a_healthy_question(session: Session) -> None:
    _stats(session, question_id=1, hit=8, miss=2)    # 未命中率 20%
    assert tasks.self_repair_stats(session, {})["flagged"] == []


def test_self_repair_never_changes_the_mount(session: Session) -> None:
    """**只标记，不改挂载** —— question_flags 是给人看的仪表板（ADR-0002）。"""
    _stats(session, question_id=1, hit=0, miss=10)
    before = session.get(Question, 1).primary_point_id
    tasks.self_repair_stats(session, {})
    session.commit()
    assert session.get(Question, 1).primary_point_id == before


def test_self_repair_is_idempotent(session: Session) -> None:
    """重跑不能累积标记 —— 队列的回退重跑是常态。"""
    _stats(session, question_id=1, hit=0, miss=10)
    tasks.self_repair_stats(session, {})
    session.commit()
    first = len(session.execute(select(QuestionFlag)).scalars().all())

    result = tasks.self_repair_stats(session, {})
    session.commit()
    second = len(session.execute(select(QuestionFlag)).scalars().all())

    assert first == second == 1
    assert result["deleted_previous"] == 1, "先清掉自己上一批，再重建"


def test_purge_task_reports_what_it_removed(session: Session) -> None:
    result = tasks.purge_finished_jobs(session, {})
    assert "回收" in result["message"]
    assert result["purged"] == 0
