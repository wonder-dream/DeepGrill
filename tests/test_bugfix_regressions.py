"""接口缺陷修复的回归测试（对应 docs/接口缺陷清单-20260913.md）。

每条用例先复现原缺陷（注释给出修复前行为），再断言修复后的行为。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import commit, get_session, init_db
from app.models import Question, QuestionType, Source, SourceType, User
from tests.fakes import FakeEmbedder, FakeLLM
from tests.test_routes import add_today_question, make_client, make_config


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    with get_session() as session:
        yield session


def _fresh_get(model, pk):
    """用新会话查一行（避免测试会话 identity map 里被别处删掉的旧实例干扰）。"""
    with get_session() as s:
        return s.get(model, pk)


def _client(db, role="owner", username="testuser"):
    return make_client(db, FakeLLM([]), user_role=role, username=username)


# --- P2-14：stats 口径一致 ---


def test_stats_done_questions_excludes_failed_judgment(db):
    """判分失败的会话原本计入 done_questions 但不计入 answered_total → 口径不一致。"""
    from datetime import datetime

    from app.models import Judgment, Session, SessionKind, SessionStatus

    q = add_today_question(db, stem="判分失败口径测试题")
    user = db.scalars(select(User).where(User.email == "testuser@test.com")).first()
    s = Session(question_id=q.id, user_id=user.id, kind=SessionKind.open,
                status=SessionStatus.finished, ended_at=datetime.now())
    db.add(s)
    commit(db)
    db.refresh(s)
    db.add(Judgment(session_id=s.id, scores={"status": "failed"}, total_score=None,
                    review="判分失败", reference_answer="", weak_tags=[]))
    commit(db)

    c = _client(db)
    body = c.get("/api/stats").json()
    assert body["done_questions"] == body["answered_total"] == 0

