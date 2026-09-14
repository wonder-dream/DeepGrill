"""治理页与晋升按钮的页面测试（决策 5 / 9）。

页面这一侧要证明的是"动作真的有出口"：

· owner 能在治理页上看到待处理标记，并做出三种不同的处置
· **藏起来**的效果是"题从公共池里消失"（治理的实质），而不是只改一条记录
· 非 owner 进不去（它是全局动作，不是个人动作）
· 私有题的主人能看到晋升按钮，别人看不到
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.bank import quality
from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Criterion,
    Domain,
    KnowledgePoint,
    Question,
    QuestionFlag,
    User,
)
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "quality-page.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        owner = s.execute(select(User).where(User.email == "owner@local")).scalars().one()
        owner.password_hash = _HASH
        s.add(User(id=2, email="me@local", username="me", password_hash=_HASH, role="user"))
        s.add(User(id=3, email="other@local", username="o", password_hash=_HASH, role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile 的作用与边界",
                       difficulty=3, primary_point_id=1, origin="seed", visibility="public"))
        s.add(Question(id=2, kind="knowledge", stem="说说 volatile 的作用与边界。",
                       difficulty=3, primary_point_id=1, origin="generated", visibility="public"))
        # 我的私有题（用来验晋升按钮）
        s.add(Question(id=3, kind="design", stem="设计一个支持幂等与熔断的下单系统",
                       difficulty=4, primary_point_id=1, origin="generated",
                       owner_user_id=2, visibility="private"))
        # 别人的私有题（按钮不该出现）
        s.add(Question(id=4, kind="design", stem="别人的私有设计题，也足够长了吧",
                       difficulty=4, primary_point_id=1, origin="generated",
                       owner_user_id=3, visibility="private"))
        s.commit()
    return path


@pytest.fixture
def app(db: Path):
    return create_app(Settings(database_path=db))


def _client(app, email: str) -> TestClient:
    c = TestClient(app)
    c.post("/login", data={"email": email, "password": PASSWORD})
    return c


@pytest.fixture
def owner_client(app) -> TestClient:
    with _client(app, "owner@local") as c:
        yield c


@pytest.fixture
def user_client(app) -> TestClient:
    with _client(app, "me@local") as c:
        yield c


def _flags(db: Path) -> list[QuestionFlag]:
    with create_session_factory(create_db_engine(db))() as s:
        return list(s.execute(select(QuestionFlag).order_by(QuestionFlag.id)).scalars().all())


# ---------------------------------------------------------------------------
# 治理页
# ---------------------------------------------------------------------------
def test_page_lists_open_flags_with_the_stem(owner_client: TestClient, db: Path) -> None:
    """检测先跑一遍（它平时由离线任务做），然后页面要能读到。"""
    with create_session_factory(create_db_engine(db))() as s:
        quality.flag_duplicate_questions(s)
        s.commit()

    body = owner_client.get("/admin/quality").text
    assert "题库质量" in body
    assert "说说 volatile 的作用与边界。" in body, "治理页要直接给出题干，不能只给 id"
    assert "重复" in body or "conflict" in body
    assert "藏起来" in body and "忽略" in body and "结掉" in body


def test_empty_state_is_explicit(owner_client: TestClient) -> None:
    body = owner_client.get("/admin/quality").text
    assert "没有待处理的质量标记" in body


def test_only_owner_can_open_or_act(app, user_client: TestClient, db: Path) -> None:
    assert user_client.get("/admin/quality").status_code == 403
    with create_session_factory(create_db_engine(db))() as s:
        quality.flag_duplicate_questions(s)
        s.commit()
    flag_id = _flags(db)[0].id
    for action in ("resolve", "dismiss", "hide"):
        assert user_client.post(f"/admin/quality/{flag_id}/{action}").status_code == 403


def test_resolve_action_closes_the_flag_only(owner_client: TestClient, db: Path) -> None:
    with create_session_factory(create_db_engine(db))() as s:
        quality.flag_duplicate_questions(s)
        s.commit()
    flag_id = _flags(db)[0].id

    r = owner_client.post(f"/admin/quality/{flag_id}/resolve", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/admin/quality")
    assert _flags(db)[0].status == "resolved"
    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(Question, 2).visibility == "public", "结掉不动题目"


def test_dismiss_action_marks_it_as_a_false_positive(owner_client: TestClient, db: Path) -> None:
    with create_session_factory(create_db_engine(db))() as s:
        quality.flag_duplicate_questions(s)
        s.commit()
    flag_id = _flags(db)[0].id
    owner_client.post(f"/admin/quality/{flag_id}/dismiss", follow_redirects=False)
    assert _flags(db)[0].status == "dismissed"


def test_hide_action_removes_the_question_from_the_public_pool(
    owner_client: TestClient, db: Path
) -> None:
    """**治理的实质**：藏起来 = 它不再出现在公共浏览与抽题里。"""
    with create_session_factory(create_db_engine(db))() as s:
        quality.flag_duplicate_questions(s)
        s.commit()
    flag_id = _flags(db)[0].id

    r = owner_client.post(f"/admin/quality/{flag_id}/hide", follow_redirects=False)
    assert r.status_code == 302
    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(Question, 2).visibility == "hidden"
    assert "说说 volatile 的作用与边界。" not in owner_client.get("/bank").text


def test_unknown_flag_is_404(owner_client: TestClient) -> None:
    assert owner_client.post("/admin/quality/999/resolve").status_code == 404


def test_observability_page_links_to_the_governance_page(owner_client: TestClient) -> None:
    """只读的那半页要指向能动手的那半页 —— 否则看到问题也没有出口。"""
    assert "/admin/quality" in owner_client.get("/admin/observability").text


# ---------------------------------------------------------------------------
# 晋升按钮
# ---------------------------------------------------------------------------
def test_owner_of_a_private_question_sees_the_promote_button(
    user_client: TestClient, db: Path
) -> None:
    body = user_client.get("/bank/3").text
    assert "晋升到公共题库" in body
    assert 'action="/bank/3/promote"' in body


def test_someone_elses_private_question_has_no_button(user_client: TestClient, db: Path) -> None:
    """别人的私有题**根本打不开**（可见性过滤）—— 所以看不到按钮也看不到题。"""
    assert user_client.get("/bank/4").status_code == 404


def test_public_question_has_no_promote_form(user_client: TestClient, db: Path) -> None:
    body = user_client.get("/bank/1").text
    assert "晋升到公共题库" not in body or "已经在公共题库里了" in body


def test_promote_action_moves_it_and_redirects(user_client: TestClient, db: Path) -> None:
    r = user_client.post("/bank/3/promote", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/bank/3?promoted=ok"

    with create_session_factory(create_db_engine(db))() as s:
        q = s.get(Question, 3)
        assert q.owner_user_id is None and q.visibility == "public"
        assert q.primary_point_id is None and q.origin == "promoted"

    body = user_client.get("/bank/3?promoted=ok").text
    assert "公共待定池" in body


def test_failed_gate_shows_why_on_the_same_page(user_client: TestClient, db: Path) -> None:
    """门禁没过 → **200 + 说明哪一条没过**（不是错误页，用户要据此改）。"""
    with create_session_factory(create_db_engine(db))() as s:
        s.get(Question, 3).stem = "太短"
        s.commit()

    r = user_client.post("/bank/3/promote")
    assert r.status_code == 200
    assert "门禁没过" in r.text
    assert "字" in r.text, "要说清是哪条检查没过"
    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(Question, 3).owner_user_id == 2, "失败时题目留在私有题集里"


def test_anonymous_cannot_promote(app, db: Path) -> None:
    with TestClient(app) as c:
        r = c.post("/bank/3/promote", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"