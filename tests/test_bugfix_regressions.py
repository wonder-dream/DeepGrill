"""接口缺陷修复的回归测试（对应 docs/接口缺陷清单-20260913.md）。

每条用例先复现原缺陷（注释给出修复前行为），再断言修复后的行为。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import commit, get_session, init_db
from app.models import Question, QuestionType, Source, SourceType, User
from app.web.routes import create_app
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


# --- P1-8：验证码冷却 ---


def test_send_code_failure_still_cools_down(monkeypatch, db):
    """SMTP 失败也占 60s 冷却（修复前失败不留 last_sent → 可无限重发）。"""
    from app import email_code

    def boom(email, code):
        raise RuntimeError("SMTP 未配置")

    monkeypatch.setattr(email_code, "send_smtp_code", boom)
    c = _client(db)
    assert c.post("/api/auth/send-code", json={"email": "x@163.com"}).status_code == 503
    assert c.post("/api/auth/send-code", json={"email": "x@163.com"}).status_code == 429


