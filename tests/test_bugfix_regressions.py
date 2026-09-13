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


# --- P1-7：限流额度只计真正执行的请求 ---


def test_rejected_requests_do_not_consume_quota(db):
    """被业务层拒绝（4xx）的写请求不占额度：否则 10 次非法上传就能锁死合法上传。

    走完整登录链路（Cookie 认证）拿到 owner，再用恒 400 的非法后缀请求验证额度未被吃光。
    """
    from app import ratelimit
    from app.auth import hash_password
    from app.models import User as _U

    with get_session() as s:
        s.add(_U(email="quota@test.com", username="quota",
                 password_hash=hash_password("quotapass1"), role="owner"))
        commit(s)

    app = create_app(make_config(), llm_factory=lambda role: FakeLLM([]),
                     daily_runner=None, embedder_factory=lambda: FakeEmbedder())
    client = TestClient(app)
    assert client.post("/api/auth/login", json={
        "email": "quota@test.com", "password": "quotapass1"}).status_code == 200
    bad = {"filename": "a.exe", "content": "x"}
    ratelimit.reset()
    for _ in range(40):  # 远超 LLM_COST_LIMIT(10)
        resp = client.post("/api/upload", json=bad, headers={"X-Requested-With": "fetch"})
        assert resp.status_code == 400, resp.text[:120]
    assert client.post("/api/upload", json=bad,
                       headers={"X-Requested-With": "fetch"}).status_code == 400


def test_rate_limit_still_blocks_after_real_requests(db):
    """真正执行的写请求照常计数（成功响应累计到上限后 429）。"""
    from app import ratelimit

    c = _client(db)
    q = add_today_question(db, stem="限流计数测试题")
    ratelimit.reset()
    codes = [c.post(f"/api/favorites/{q.id}").status_code for _ in range(62)]
    assert codes.count(200) == 60
    assert codes[-1] == 429


