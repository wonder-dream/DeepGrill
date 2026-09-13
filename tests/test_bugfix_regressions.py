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


# --- P1-10：上传文件名归一化 ---


def test_upload_filename_traversal_normalized(db):
    """`../../evil.md` 原本按后缀放行且原样入库；现在只取 basename。"""
    c = _client(db)
    resp = c.post("/api/upload", json={"filename": "../../evil.md",
                                       "content": "Q: 路径穿越归一化测试题"})
    assert resp.status_code == 200
    from app.models import Source as _S

    source = db.scalars(select(_S).order_by(_S.id.desc())).first()
    assert source.title == "evil.md"
    assert "/" not in source.title and "\\" not in source.title


def test_upload_filename_overlong_400(db):
    c = _client(db)
    resp = c.post("/api/upload", json={"filename": "a" * 500 + ".md", "content": "Q: 超长文件名测试"})
    assert resp.status_code == 400

# --- P2-12：LIKE 元字符按字面量 ---


def test_bank_search_wildcard_is_literal(db):
    """q=% / q=_ 原本是 LIKE 通配（整个题库命中）→ 现在只做字面量匹配。"""
    add_today_question(db, stem="Buffer 与缓冲区设计题")
    add_today_question(db, stem="含百分号%的题目")
    add_today_question(db, stem="含下划线_的题目")
    c = _client(db)
    assert c.get("/api/bank").json()["total"] == 3
    assert c.get("/api/bank", params={"q": "%"}).json()["total"] == 1  # 修复前 = 3
    assert c.get("/api/bank", params={"q": "_"}).json()["total"] == 1  # 修复前 = 3
    assert c.get("/api/bank", params={"q": "Buffer"}).json()["total"] == 1
    assert c.get("/api/bank", params={"q": "不含的关键词"}).json()["total"] == 0

