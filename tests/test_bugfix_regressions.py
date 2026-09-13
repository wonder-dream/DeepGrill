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


# --- P1-9：并发建同名标签/分类 → 409 而非 500 ---


def test_duplicate_category_conflict_409(db):
    c = _client(db)
    assert c.post("/api/admin/categories", json={"name": "并发分类"}).status_code == 200
    resp = c.post("/api/admin/categories", json={"name": "并发分类"})
    assert resp.status_code == 409


def test_duplicate_tag_conflict_409(db):
    c = _client(db)
    assert c.post("/api/admin/tags", json={"name": "并发标签", "category_id": 1}).status_code == 200
    assert c.post("/api/admin/tags", json={"name": "并发标签", "category_id": 1}).status_code == 409

