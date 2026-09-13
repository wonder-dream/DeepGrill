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


# --- P0-4/5、P1-6：admin 改题的 null 语义 ---


def test_admin_update_null_rejected_400(db):
    """stem/difficulty/criteria 传 null 原本 500（且 stem 那次绕开异常处理器）；现在 400。"""
    q = add_today_question(db, stem="空值语义测试题")
    c = _client(db)
    for body in ({"stem": None}, {"difficulty": None}, {"good_criteria": None},
                 {"bad_criteria": None}, {"tags": None}, {"reviewed": None}):
        resp = c.put(f"/api/admin/questions/{q.id}", json=body)
        assert resp.status_code == 400, (body, resp.status_code, resp.text[:120])
        assert "detail" in resp.json()


def test_attribute_error_becomes_400_json(db):
    """请求体类型畸形时不再返回纯文本 500：统一 400 + JSON detail。"""
    c = _client(db)
    resp = c.put("/api/admin/questions/1", json={"stem": 123})
    assert resp.status_code == 400, resp.text[:120]
    assert isinstance(resp.json().get("detail"), str)


def test_admin_update_reviewed_null_does_not_unpublish(db):
    """reviewed=null 原本 200 且把题目静默下架；现在 400 且 reviewed_at 不变。"""
    q = add_today_question(db, stem="下架保护测试题")
    assert q.reviewed_at is not None
    c = _client(db)
    assert c.put(f"/api/admin/questions/{q.id}", json={"reviewed": None}).status_code == 400
    db.expire_all()
    assert db.get(Question, q.id).reviewed_at is not None


def test_admin_update_difficulty_null_keeps_row_intact(db):
    """difficulty=null 原本 500（NOT NULL 违规）；现在 400 且原值保留。"""
    q = add_today_question(db, stem="难度空值测试题")
    before = q.difficulty
    c = _client(db)
    assert c.put(f"/api/admin/questions/{q.id}", json={"difficulty": None}).status_code == 400
    db.expire_all()
    assert db.get(Question, q.id).difficulty == before


def test_admin_update_ok_paths_still_work(db):
    """省略字段=不改；正常值仍可写入。"""
    q = add_today_question(db, stem="正常改题测试题")
    c = _client(db)
    resp = c.put(f"/api/admin/questions/{q.id}", json={"difficulty": 4, "stem": "改写后的题干内容"})
    assert resp.status_code == 200
    assert resp.json()["difficulty"] == 4 and resp.json()["stem"] == "改写后的题干内容"
    # reviewed=True 仍可正常下架/上架
    assert c.put(f"/api/admin/questions/{q.id}", json={"reviewed": False}).status_code == 200
    db.expire_all()
    assert db.get(Question, q.id).reviewed_at is None
    assert c.put(f"/api/admin/questions/{q.id}", json={"reviewed": True}).status_code == 200


