"""注销与导出的测试（决策 7 / 21）。

这一组里**最值钱的是"先聚合再删"那条**：顺序反了不会报错，只会让聚合质量信号
永久丢失 —— 而 `question_point_stats` 正是挂载自修复唯一的输入。所以断言点选在
"删完之后统计里有没有那些计数"，而不是"函数返回了什么"。

另外两条同样重要：
· 注销必须**真的删掉身份与原文**（决策 21：硬删，不是匿名化）
· 导出必须**不含凭据**（口令哈希与令牌不是"我的数据"，带上只是多一处泄露面）
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.account import repository as account_repository
from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Attempt,
    CandidateProfile,
    Criterion,
    Domain,
    Evaluation,
    Interview,
    InviteCode,
    KnowledgePoint,
    Question,
    QuestionFeedback,
    QuestionPointStat,
    User,
    UserFavorite,
    UserToken,
)
from app.db.models import (
    Session_ as InterviewSession,
)
from app.interview import rules
from app.profile import service
from migrations._runner import migrate

ME = 2
OTHER = 3


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "profile.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        s.add_all(
            [
                User(id=ME, email="me@local", username="me", password_hash="h", role="user"),
                User(id=OTHER, email="o@local", username="o", password_hash="h", role="user"),
            ]
        )
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        for seq, text in enumerate(["可见性", "内存屏障"], start=1):
            s.add(Criterion(id=seq, point_id=1, seq=seq, text=text, shared=0))
        s.flush()
        s.add_all(
            [
                Question(id=1, kind="knowledge", stem="公共题", difficulty=3,
                         primary_point_id=1, origin="seed", visibility="public"),
                Question(id=2, kind="knowledge", stem="我的私有题", difficulty=3,
                         primary_point_id=1, origin="generated", owner_user_id=ME,
                         visibility="private"),
                Question(id=3, kind="knowledge", stem="别人的私有题", difficulty=3,
                         primary_point_id=1, origin="generated", owner_user_id=OTHER,
                         visibility="private"),
            ]
        )
        s.add(InviteCode(code="MINE", created_by=ME, used_by=ME, used_at="2026-01-01 00:00:00"))
        s.add(UserToken(token_hash="tok", user_id=ME, expires_at="2099-01-01 00:00:00"))
        s.add(CandidateProfile(user_id=ME, structured={"项目": ["A"]}))
        s.add(UserFavorite(user_id=ME, question_id=1))
        s.add(QuestionFeedback(question_id=1, user_id=ME, kind="unclear", detail="不清楚"))
        s.commit()
        yield s


def _interview_with_rounds(session: Session, *, question_id: int = 2) -> None:
    """建一场已完成的面试（默认答的是**自己的私有题**）。"""
    session.add(Interview(id=10, user_id=ME, mode="drill", status="finished", quota_charged=1))
    session.add(
        InterviewSession(id=10, interview_id=10, question_id=question_id, seq=1,
                         status="finished", max_rounds=3)
    )
    session.flush()
    session.add(
        Attempt(
            session_id=10, round_no=1, is_followup=0, input_mode="text",
            answer_text="答", hits=rules.HitSnapshot({1: "命中", 2: "未命中"}).to_json(),
        )
    )
    session.add(Evaluation(session_id=10, scores={"accuracy": 80}, total_score=80, review="还行"))
    session.commit()


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def test_export_contains_my_data(session: Session) -> None:
    _interview_with_rounds(session)
    data = service.export_user_data(session, ME)

    assert data["account"]["email"] == "me@local"
    assert [q["stem"] for q in data["private_questions"]] == ["我的私有题"]
    assert data["favorites"] == [1]
    assert data["profiles"][0]["structured"] == {"项目": ["A"]}
    assert len(data["interviews"]) == 1
    entry = data["interviews"][0]
    assert entry["report_summary"] is None or isinstance(entry["report_summary"], str)
    assert entry["sessions"][0]["attempts"][0]["answer_text"] == "答"
    assert entry["sessions"][0]["evaluation"]["total_score"] == 80


def test_export_does_not_contain_credentials(session: Session) -> None:
    """**凭据不是"我的数据"** —— 导出它们只会多一处泄露面。"""
    payload = json.dumps(service.export_user_data(session, ME), ensure_ascii=False)
    assert "password_hash" not in payload
    assert "token_hash" not in payload
    assert "tok" not in payload


def test_export_does_not_leak_other_users_data(session: Session) -> None:
    data = service.export_user_data(session, ME)
    assert "别人的私有题" not in json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 注销
# ---------------------------------------------------------------------------
def test_fold_hits_into_stats_counts_hits_and_misses(session: Session) -> None:
    """线 1 → 线 2：命中与未命中各加各的，**「未涉及」两条都不加**。"""
    session.add(Interview(id=11, user_id=ME, mode="drill", status="finished", quota_charged=1))
    session.add(InterviewSession(id=11, interview_id=11, question_id=1, seq=1, status="finished", max_rounds=3))
    session.flush()
    session.add(
        Attempt(
            session_id=11, round_no=1, is_followup=0, input_mode="text", answer_text="答",
            # 故意放一条「未涉及」：它不该进任何一列
            hits=rules.HitSnapshot({1: "命中", 2: "未涉及"}).to_json(),
        )
    )
    session.commit()

    touched = service.fold_hits_into_stats(session, ME)
    session.commit()
    assert touched == 1, "只有'命中'那一条该被计入（未涉及两条线都不计）"

    rows = session.execute(select(QuestionPointStat)).scalars().all()
    assert len(rows) == 1
    assert (rows[0].hit_count, rows[0].miss_count) == (1, 0)


def test_delete_folds_stats_before_deleting(session: Session) -> None:
    """**本文件最重要的一条**：统计必须在删之前累加。

    顺序反了不会报错 —— 只会让 `question_point_stats` 少掉这个人的数据，
    而挂载自修复唯一的输入就是它。所以断言"删完之后统计里还有计数"。

    这里**故意答公共题**（`question_id=1`）：统计挂在公共题上，于是它能活过注销 ——
    那正是这条数据线存在的意义（换个用户来考同一道题，这个信号仍然有效）。
    答自己的私有题时统计会随题一起消失，那是另一条测试。
    """
    _interview_with_rounds(session, question_id=1)
    report = service.delete_account(session, ME)
    session.commit()

    assert report.stats_rows_touched == 2, "命中与未命中各一条统计"
    rows = session.execute(select(QuestionPointStat)).scalars().all()
    assert {(r.hit_count, r.miss_count) for r in rows} == {(1, 0), (0, 1)}


def test_stats_on_a_deleted_private_question_go_away_too(session: Session) -> None:
    """统计挂在**将被删掉的私有题**上时必须一起清掉 —— 否则撞外键。

    这不是"少保留了一点信号"而是**必然**：题都没了，它的计数没有承载物。
    第一版就是在这里撞了 `FOREIGN KEY constraint failed`（测试抓到的）。
    """
    _interview_with_rounds(session, question_id=2)  # 自己的私有题
    report = service.delete_account(session, ME)
    session.commit()

    assert report.stats_rows_touched == 2, "仍然先聚合过（顺序不变）"
    assert session.execute(select(QuestionPointStat)).first() is None, "但随题一起消失"
    assert session.get(Question, 2) is None


def test_delete_removes_identity_and_content(session: Session) -> None:
    """硬删：身份、令牌、档案、面试与逐轮原文、私有题、收藏、额度都要没。"""
    _interview_with_rounds(session)
    report = service.delete_account(session, ME)
    session.commit()

    assert report.deleted is True
    assert session.get(User, ME) is None, "身份必须真的删掉（不是软删）"
    assert session.execute(select(UserToken).where(UserToken.user_id == ME)).first() is None
    assert session.execute(select(CandidateProfile).where(CandidateProfile.user_id == ME)).first() is None
    assert session.execute(select(Interview).where(Interview.user_id == ME)).first() is None
    assert session.execute(select(Attempt)).first() is None, "逐轮原文要删"
    assert session.execute(select(UserFavorite).where(UserFavorite.user_id == ME)).first() is None
    assert session.execute(select(Question).where(Question.owner_user_id == ME)).first() is None


def test_delete_keeps_public_questions_and_other_users(session: Session) -> None:
    _interview_with_rounds(session)
    service.delete_account(session, ME)
    session.commit()

    assert session.get(Question, 1) is not None, "公共题不该被删"
    assert session.get(Question, 3) is not None, "别人的私有题不该被删"
    assert session.get(User, OTHER) is not None


def test_delete_unlinks_feedback_instead_of_deleting_it(session: Session) -> None:
    """反馈是**内容质量信号**，不随谁报的而改变 —— 只断链，不删行。"""
    service.delete_account(session, ME)
    session.commit()

    rows = session.execute(select(QuestionFeedback)).scalars().all()
    assert len(rows) == 1, "反馈要留着（它是内容质量信号）"
    assert rows[0].user_id is None, "但必须断开与用户的关联"


def test_delete_clears_invite_code_self_references(session: Session) -> None:
    """`invite_codes` 自引用 users：不清就撞外键（v2 一条 CASCADE 都没有）。"""
    service.delete_account(session, ME)
    session.commit()

    invite = account_repository.find_invite(session, "MINE")
    assert invite is not None, "码本身要留（它是发放记录）"
    assert invite.created_by is None and invite.used_by is None


def test_delete_is_idempotent(session: Session) -> None:
    service.delete_account(session, ME)
    session.commit()
    again = service.delete_account(session, ME)
    assert again.deleted is False, "再删一次应当什么都不做，而不是报错"


def test_delete_does_not_touch_other_users_data(session: Session) -> None:
    _interview_with_rounds(session)
    session.add(Interview(id=20, user_id=OTHER, mode="drill", status="finished", quota_charged=1))
    session.add(InterviewSession(id=20, interview_id=20, question_id=3, seq=1, status="finished", max_rounds=3))
    session.flush()
    session.add(
        Attempt(session_id=20, round_no=1, is_followup=0, input_mode="text", answer_text="别人的答",
                hits=rules.HitSnapshot({1: "命中"}).to_json())
    )
    session.commit()

    service.delete_account(session, ME)
    session.commit()

    assert session.get(Interview, 20) is not None
    assert session.execute(select(Attempt).where(Attempt.session_id == 20)).first() is not None


def test_count_remaining_tells_the_user_what_will_be_deleted(session: Session) -> None:
    """**不许静默删数据**：删之前要能告诉用户"将删除什么"。"""
    _interview_with_rounds(session)
    counts = service.count_remaining(session, ME)
    assert counts["interviews"] == 1
    assert counts["private_questions"] == 1
    assert counts["favorites"] == 1
    assert counts["profiles"] == 1
