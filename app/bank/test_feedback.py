"""反馈闭环的测试（v1 的工单形态，§未决 12 定案）。

这一组里最值钱的是**那条唯一约束**：它由 `0002` 迁移创建，而"同一人对同一题
只许一条待处理反馈"是**数据库层面的事实**，不靠应用层先查再写
（先查再写在并发下会漏）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bank import feedback, repository as bank_repository
from app.db import create_db_engine, create_session_factory
from app.db.models import Question, QuestionFeedback, User
from app.errors import InvalidInput, NotFound
from migrations._runner import migrate

ME = 2
OTHER = 3


@pytest.fixture
def session(tmp_dir: Path) -> Session:
    db = tmp_dir / "feedback.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        s.add_all(
            [
                User(id=ME, email="me@local", username="me", password_hash="h", role="user"),
                User(id=OTHER, email="o@local", username="o", password_hash="h", role="user"),
            ]
        )
        s.flush()
        s.add(Question(id=1, kind="knowledge", stem="公共题", difficulty=3, origin="seed",
                       visibility="public"))
        s.add(Question(id=2, kind="knowledge", stem="我的私有题", difficulty=3, origin="generated",
                       owner_user_id=ME, visibility="private"))
        s.commit()
        yield s


def test_submit_creates_an_open_ticket(session: Session) -> None:
    row = feedback.submit(session, question_id=1, user_id=ME, kind="unclear", detail="题干歧义")
    session.commit()
    assert row.status == "open"
    assert row.kind == "unclear"
    assert row.detail == "题干歧义"


def test_submit_rejects_an_unknown_kind(session: Session) -> None:
    with pytest.raises(InvalidInput):
        feedback.submit(session, question_id=1, user_id=ME, kind="没这个类型")


def test_submit_requires_the_question_to_be_visible(session: Session) -> None:
    """**不泄露存在性**：别人的私有题与不存在的题给同一个回应。"""
    session.add(Question(id=3, kind="knowledge", stem="别人的私有题", difficulty=3,
                         origin="generated", owner_user_id=OTHER, visibility="private"))
    session.commit()

    with pytest.raises(NotFound):
        feedback.submit(session, question_id=3, user_id=ME, kind="wrong")
    with pytest.raises(NotFound):
        feedback.submit(session, question_id=999, user_id=ME, kind="wrong")


def test_duplicate_submission_is_refused_by_the_database(session: Session) -> None:
    """**`uq_feedback_open` 是这一组的核心**（0002 迁移建的）。

    同一时刻只许一条待处理反馈 —— 而这是**数据库**保证的，不是应用层先查再写。
    """
    feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    session.flush()
    with pytest.raises(InvalidInput) as e:
        feedback.submit(session, question_id=1, user_id=ME, kind="wrong")
    assert "已经报过" in str(e.value)
    session.rollback()


def test_can_report_again_after_it_is_resolved(session: Session) -> None:
    """**部分**唯一索引的意义：旧反馈被处理之后可以再报一次。

    问题没修好、或者又发现了新问题 —— 这两种都是真实情况，而全表唯一会把它挡住。
    """
    first = feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    session.commit()
    feedback.resolve(session, feedback_id=first.id, action="resolved")
    session.commit()

    again = feedback.submit(session, question_id=1, user_id=ME, kind="wrong")
    session.commit()
    assert again.id != first.id
    assert len(session.execute(select(QuestionFeedback)).scalars().all()) == 2


def test_different_users_can_each_report(session: Session) -> None:
    feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    feedback.submit(session, question_id=1, user_id=OTHER, kind="unclear")
    session.commit()
    assert len(session.execute(select(QuestionFeedback)).scalars().all()) == 2


def test_different_questions_are_independent(session: Session) -> None:
    feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    feedback.submit(session, question_id=2, user_id=ME, kind="unclear")
    session.commit()
    assert len(session.execute(select(QuestionFeedback)).scalars().all()) == 2


def test_duplicate_feedback_points_at_knowledge_points(session: Session) -> None:
    """v1 的 `duplicate_question_ids` 在 v2 里**指向知识点**（数据模型已改）。

    用户说的是"这题和我见过的那个**知识点**重复"，不是"和某道题重复"。
    """
    row = feedback.submit(
        session, question_id=1, user_id=ME, kind="duplicate", duplicate_point_ids=[7, 8]
    )
    session.commit()
    assert row.duplicate_question_ids == [7, 8]


def test_resolve_and_dismiss(session: Session) -> None:
    row = feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    session.commit()

    done = feedback.resolve(session, feedback_id=row.id, action="resolved", note="已改题干")
    session.commit()
    assert done.status == "resolved"
    assert "已改题干" in done.detail, "管理员的说明要留痕"

    with pytest.raises(InvalidInput):
        # 已处理的反馈不许再处理一次 —— 否则"处理过"这件事会被覆盖成新状态
        feedback.resolve(session, feedback_id=row.id, action="dismissed")


def test_resolve_rejects_unknown_action(session: Session) -> None:
    row = feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    session.commit()
    with pytest.raises(InvalidInput):
        feedback.resolve(session, feedback_id=row.id, action="deleted")


def test_resolve_unknown_id_is_not_found(session: Session) -> None:
    with pytest.raises(NotFound):
        feedback.resolve(session, feedback_id=999, action="resolved")


def test_open_queue_only_lists_unhandled(session: Session) -> None:
    a = feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    b = feedback.submit(session, question_id=2, user_id=ME, kind="wrong")
    session.commit()
    feedback.resolve(session, feedback_id=a.id, action="resolved")
    session.commit()

    queue = feedback.open_feedback(session)
    assert [f.id for f in queue] == [b.id]


def test_my_feedback_shows_status_to_the_reporter(session: Session) -> None:
    """报的人要能看到"我报的有没有被处理" —— 否则闭环只对管理员成立。"""
    a = feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    session.commit()
    feedback.resolve(session, feedback_id=a.id, action="dismissed")
    session.commit()

    mine = feedback.my_feedback(session, ME)
    assert len(mine) == 1
    assert mine[0].status == "dismissed"


def test_counts_summarise_the_queue(session: Session) -> None:
    a = feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    feedback.submit(session, question_id=2, user_id=ME, kind="wrong")
    session.commit()
    feedback.resolve(session, feedback_id=a.id, action="resolved")
    session.commit()

    assert feedback.counts(session) == {"open": 1, "resolved": 1, "dismissed": 0}


def test_admin_sees_a_public_question_but_not_a_private_one(session: Session) -> None:
    """管理员判断的是**内容质量**，所以他只该看到公共题（私有题是别人的东西）。"""
    public = feedback.submit(session, question_id=1, user_id=ME, kind="unclear")
    session.commit()
    assert feedback.question_of(session, public) is not None

    private = QuestionFeedback(question_id=2, user_id=ME, kind="unclear", status="open")
    session.add(private)
    session.commit()
    assert feedback.question_of(session, private) is None


def test_bank_repository_is_still_the_only_question_entry(session: Session) -> None:
    """反馈模块也不许自己 `select(Question)` —— 它走的是仓储入口。

    这条不是形式主义：`submit()` 的可见性检查**必须**用同一个入口，否则它会成为
    绕过私有题隔离的第三条路。
    """
    assert bank_repository.find_question(session, 1, ME) is not None
    assert bank_repository.find_question(session, 2, OTHER) is None
