"""掌握度矩阵的测试 —— **全项目最容易算错的一处**。

矩阵的每一格是"该知识点下被考过的考察点里答到了多少"。三条产品要求必须在测试
里钉死，因为它们都是"算错了也不会报错、只是数字不对"的类型：

· **「没考过」是空格，不是 0%** —— 两者对候选人的含义完全不同（基线里写明）
· **「未涉及」不进分母** —— 这是"被考过"的定义，也是整个算法的关键一行
· **一道综合题填充多个格子** —— 掌握度只由命中记录推导，与题目挂在哪个节点下无关
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Attempt,
    Criterion,
    Domain,
    Interview,
    KnowledgePoint,
    Question,
    Session_ as InterviewSession,
    User,
)
from app.interview import rules
from app.knowledge import service
from migrations._runner import migrate

ME = 2
OTHER = 77


@pytest.fixture
def session(tmp_dir: Path) -> Session:
    db = tmp_dir / "knowledge.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        s.add(User(id=ME, email="me@local", username="me", password_hash="h", role="user"))
        s.add(User(id=OTHER, email="o@local", username="o", password_hash="h", role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        s.add(Domain(id=2, name="系统设计"))

        volatile = KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed")
        lock = KnowledgePoint(id=2, domain_id=1, name="synchronized", status="confirmed")
        s.add_all([volatile, lock])
        s.flush()
        for seq, text in enumerate(["可见性", "内存屏障", "不保证原子性"], start=1):
            s.add(Criterion(point_id=1, seq=seq, text=text, shared=0))
        s.add(Criterion(point_id=2, seq=1, text="锁的粒度", shared=0))
        s.flush()

        s.add_all(
            [
                Question(id=1, kind="knowledge", stem="volatile?", difficulty=3,
                         primary_point_id=1, origin="seed", visibility="public"),
                Question(id=2, kind="knowledge", stem="synchronized?", difficulty=3,
                         primary_point_id=2, origin="seed", visibility="public"),
            ]
        )
        s.commit()
        yield s


def _interview(session: Session, user_id: int, *, interview_id: int, question_id: int):
    session.add(Interview(id=interview_id, user_id=user_id, mode="drill", status="active",
                          quota_charged=1))
    ts = InterviewSession(id=interview_id, interview_id=interview_id, question_id=question_id,
                          seq=1, status="active", max_rounds=3)
    session.add(ts)
    session.flush()
    return ts


def _round(session: Session, ts, round_no: int, statuses: dict[int, str]) -> None:
    """写一轮的**累积快照**（历史数据可能来自"第一轮就全判定"或"逐轮累积"）。"""
    session.add(
        Attempt(
            session_id=ts.id,
            round_no=round_no,
            is_followup=0,
            input_mode="text",
            answer_text="答",
            hits=rules.HitSnapshot(statuses).to_json(),
        )
    )
    session.flush()


def test_not_yet_tested_is_a_blank_not_zero(session: Session) -> None:
    """**「没考过」是空格**：`rate is None` 而不是 0.0。

    返回 0 会把"还没测到"显示成"全错" —— 这正是基线点名要求区分的那件事。
    """
    matrix = service.mastery_matrix(session, ME)
    cell = matrix.by_point()[1]
    assert cell.covered == 0
    assert cell.rate is None
    assert cell.percent == "—"


def test_not_covered_is_excluded_from_the_denominator(session: Session) -> None:
    """**「未涉及」不进分母** —— 整个算法的关键一行。

    3 条考察点里只有 1 条被真正考到（命中），另两条「未涉及」。
    所以这一格应当是 1/1 = 100%，而不是 1/3 = 33%。
    """
    ts = _interview(session, ME, interview_id=10, question_id=1)
    _round(session, ts, 1, {1: "命中", 2: "未涉及", 3: "未涉及"})
    session.commit()

    cell = service.mastery_matrix(session, ME).by_point()[1]
    assert (cell.covered, cell.hit) == (1, 1)
    assert cell.percent == "100%"


def test_asked_but_not_answered_is_zero_percent(session: Session) -> None:
    """**「考了但没答」是 0%** —— 与空格可区分。"""
    ts = _interview(session, ME, interview_id=11, question_id=1)
    _round(session, ts, 1, {1: "未命中", 2: "未命中", 3: "未涉及"})
    session.commit()

    cell = service.mastery_matrix(session, ME).by_point()[1]
    assert (cell.covered, cell.hit) == (2, 0)
    assert cell.percent == "0%"
    assert cell.rate == 0.0


def test_criteria_are_deduplicated_across_rounds(session: Session) -> None:
    """同一考察点被考两次不放大分母（只在**并集**里算一次）。

    跨轮取并集的行为本身也在测：第一轮未命中、第二轮命中 → 该考察点算**命中**
    （答到了就是答到了，这也是累积快照的方向性）。
    """
    ts = _interview(session, ME, interview_id=12, question_id=1)
    _round(session, ts, 1, {1: "未命中", 2: "未涉及", 3: "未涉及"})
    # 第二轮累积快照：第 1 条变成命中
    _round(session, ts, 2, {1: "命中", 2: "未涉及", 3: "未涉及"})
    session.commit()

    cell = service.mastery_matrix(session, ME).by_point()[1]
    assert (cell.covered, cell.hit) == (1, 1), "同一个考察点跨轮只算一次，且以最终状态为准"


def test_matrix_is_per_user(session: Session) -> None:
    """矩阵是**每个人的视图** —— 别人的作答不进我的格子。"""
    theirs = _interview(session, OTHER, interview_id=20, question_id=1)
    _round(session, theirs, 1, {1: "命中", 2: "命中", 3: "命中"})
    session.commit()

    assert service.mastery_matrix(session, ME).by_point()[1].covered == 0
    assert service.mastery_matrix(session, OTHER).by_point()[1].hit == 3


def test_composite_question_fills_multiple_cells(session: Session) -> None:
    """**一道题填充多个格子**（基线里的验收标准，用真题 1065 的语义）。

    实现上它是"同一份 hit 记录按知识点归集"：只要该题的 `hits` 里含别的知识点
    的考察点，那些格子就会被填上 —— 与"这道题挂在哪个节点下"无关（决策 24）。
    """
    ts = _interview(session, ME, interview_id=30, question_id=1)
    # 一道题同时牵动 volatile(1,2,3) 与 synchronized(4) 的考察点
    _round(session, ts, 1, {1: "命中", 2: "命中", 3: "未命中", 4: "命中"})
    session.commit()

    by_point = service.mastery_matrix(session, ME).by_point()
    assert (by_point[1].covered, by_point[1].hit) == (3, 2)
    assert (by_point[4 if 4 in by_point else 2].covered, by_point[4 if 4 in by_point else 2].hit) == (1, 1), (
        "第 4 条考察点属于 synchronized，它必须填到那个格子上"
    )


def test_weak_points_rank_by_miss_count_not_rate(session: Session) -> None:
    """薄弱点排序用**未命中条数**，不是命中率。

    只被考 1 条且答错的知识点命中率 0%，但它不该排在"考了 3 条答对 1 条"的前面 ——
    后者才是真的弱。这条排序规则如果写反，页面会永远推荐那个只被考过一次的点。
    """
    ts1 = _interview(session, ME, interview_id=40, question_id=1)
    _round(session, ts1, 1, {1: "命中", 2: "未命中", 3: "未命中"})  # volatile: 1/3
    ts2 = _interview(session, ME, interview_id=41, question_id=2)
    _round(session, ts2, 1, {4: "未命中"})  # synchronized: 0/1
    session.commit()

    weak = service.mastery_matrix(session, ME).weak_points()
    assert weak[0].point_name == "volatile", "未命中 2 条的应当排在未命中 1 条的前面"
    assert weak[0].percent == "33%"


def test_mastered_points_are_not_listed_as_weak(session: Session) -> None:
    ts = _interview(session, ME, interview_id=50, question_id=2)
    _round(session, ts, 1, {4: "命中"})
    session.commit()
    assert service.mastery_matrix(session, ME).weak_points() == []


def test_corrupt_hits_json_does_not_crash_the_matrix(session: Session) -> None:
    """坏掉的 JSON 只该让那一轮不被计入，不该让整个页面 500。"""
    ts = _interview(session, ME, interview_id=60, question_id=1)
    session.add(
        Attempt(
            session_id=ts.id, round_no=1, is_followup=0, input_mode="text",
            answer_text="答", hits="{这不是 JSON",
        )
    )
    session.commit()
    assert service.mastery_matrix(session, ME).by_point()[1].covered == 0
