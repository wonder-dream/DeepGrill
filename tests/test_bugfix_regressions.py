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


# --- P0-1/2：整数参数超 SQLite int64 上限 → 原本 OverflowError 500 ---


def test_bank_page_upper_bound_400(db):
    """GET /api/bank?page=INT64_MAX 原本 500（offset 溢出）→ 现在 400。"""
    c = _client(db)
    for page in ("9223372036854775807", "99999999999999999999", "100000001"):
        resp = c.get("/api/bank", params={"page": page})
        assert resp.status_code == 400, page
        assert resp.json()["detail"] == "invalid page or page_size"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/questions/{big}"),
    ("GET", "/api/questions/{big}/history"),
    ("GET", "/api/questions/{big}/similar"),
    ("GET", "/api/sessions/{big}"),
    ("GET", "/api/sessions/{big}/resume"),
    ("GET", "/api/ugc/submissions/{big}"),
    ("PUT", "/api/admin/questions/{big}"),
    ("DELETE", "/api/admin/questions/{big}"),
    ("DELETE", "/api/admin/tags/{big}"),
    ("DELETE", "/api/admin/categories/{big}"),
    ("POST", "/api/admin/feedback/{big}/resolve"),
    ("POST", "/api/favorites/{big}"),
])
def test_oversized_id_never_500(db, method, path):
    """超 int64 的资源 id 原本在驱动层 OverflowError → 500；现在一律 400/404。"""
    c = _client(db)
    resp = c.request(method, path.format(big="99999999999999999999"))
    assert resp.status_code in (400, 404), (method, path, resp.status_code, resp.text[:120])

def test_repeated_query_param_oversized_400(db):
    """?page=1&page=<超大> 重复参数原本 500 → 现在 400。"""
    c = _client(db)
    resp = c.get("/api/bank?page=1&page=99999999999999999999")
    assert resp.status_code == 400

# --- P0-3：date 溢出 + 解析口径 ---


def test_today_date_overflow_400(db):
    """date=9999-12-31 原本 +1 天 OverflowError → 500；现在 400。"""
    c = _client(db)
    for bad in ("9999-12-31", "9999-12-30", "2099-01-01"):
        assert c.get("/api/today", params={"date": bad}).status_code == 400, bad


def test_today_date_strict_iso(db, tmp_path):
    """非补零 `2026-9-3` 与带时间的 `2026-09-13T00:00:00` 都被拒（只收 YYYY-MM-DD）。"""
    c = _client(db)
    for bad in ("2026-9-3", "2026-09-13T00:00:00", "2026-09-13 ", "20260913"):
        assert c.get("/api/today", params={"date": bad}).status_code == 400, bad
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    assert c.get("/api/today", params={"date": today}).status_code == 200

