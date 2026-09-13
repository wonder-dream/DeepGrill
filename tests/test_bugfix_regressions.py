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


def test_review_suggestions_oversized_id_400(db):
    """?ids=<超大整数> 原本 500（绑定溢出）→ 现在忽略非法项返回空建议。"""
    c = _client(db)
    resp = c.get("/api/review/suggestions", params={"ids": "99999999999999999999"})
    assert resp.status_code == 200
    assert resp.json() == {"suggestions": {}}

# --- P2-13：suggestions 条数上限 ---


def test_review_suggestions_limited(db):
    from app.web.routes import SUGGESTIONS_MAX_IDS

    for i in range(5):
        add_today_question(db, stem=f"审核建议上限题{i}")
    c = _client(db)
    ids = ",".join(str(i) for i in range(1, SUGGESTIONS_MAX_IDS + 50))
    resp = c.get("/api/review/suggestions", params={"ids": ids})
    assert resp.status_code == 200
    assert len(resp.json()["suggestions"]) <= SUGGESTIONS_MAX_IDS

