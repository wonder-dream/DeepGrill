"""反馈闭环的页面测试：用户能报、能看到状态；管理员能处理、能看到队列。

**匿名不许报**：表里允许 `user_id` 为空（v1 的匿名反馈），但产品上不接受 ——
没有回报渠道的反馈是单向的，用户看不到自己报的有没有被处理。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Question, QuestionFeedback, User
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "fb.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        owner = s.execute(select(User).where(User.email == "owner@local")).scalars().one()
        owner.password_hash = _HASH
        s.add(User(id=3, email="user@local", username="u", password_hash=_HASH, role="user"))
        s.flush()
        s.add(Question(id=1, kind="knowledge", stem="公共题", difficulty=3, origin="seed",
                       visibility="public"))
        s.commit()
    return path


def _login(client: TestClient, email: str) -> None:
    client.post("/login", data={"email": email, "password": PASSWORD})


@pytest.fixture
def app(db: Path):
    """**一个 app 服务两个客户端**（各自有自己的 cookie jar）。

    ⚠️ 第一版给"用户"和"owner"各建了一个 `create_app()` —— 于是同一个库上住了两套
    连接池，而其中一个池里的连接持着旧读事务，导致"owner 处理完之后用户再报一次"
    看不到新状态（表现为那次 POST 返回 200 而不是 302，看起来像唯一约束没生效）。
    用同一个 app 就没有这个问题，而它与线上更接近：一个进程、一个引擎。
    """
    return create_app(Settings(database_path=db))


@pytest.fixture
def user_client(app):
    with TestClient(app) as c:
        _login(c, "user@local")
        yield c


@pytest.fixture
def owner_client(app):
    with TestClient(app) as c:
        _login(c, "owner@local")
        yield c


def test_bank_detail_has_a_feedback_form(user_client: TestClient) -> None:
    body = user_client.get("/bank/1").text
    assert "报问题" in body
    assert "答案或评分标准有错" in body


def test_submit_then_see_it_in_my_feedback(user_client: TestClient, db: Path) -> None:
    r = user_client.post(
        "/bank/1/feedback",
        data={"kind": "unclear", "detail": "题干有歧义"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/bank/1")

    with create_session_factory(create_db_engine(db))() as s:
        row = s.execute(select(QuestionFeedback)).scalars().one()
        assert row.status == "open" and row.kind == "unclear"
        assert row.user_id == 3

    mine = user_client.get("/me/feedback").text
    assert "题干有歧义" in mine
    assert "待处理" in mine


def test_anonymous_cannot_submit_feedback(db: Path) -> None:
    """**要登录** —— 匿名的反馈没有回报渠道，是单向的。"""
    with TestClient(create_app(Settings(database_path=db))) as c:
        r = c.post("/bank/1/feedback", data={"kind": "unclear"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_duplicate_report_is_refused_with_a_readable_message(
    user_client: TestClient, db: Path
) -> None:
    user_client.post("/bank/1/feedback", data={"kind": "unclear", "detail": "第一次"})
    r = user_client.post("/bank/1/feedback", data={"kind": "wrong", "detail": "第二次"})
    assert r.status_code == 400
    assert "已经报过" in r.text


def test_owner_sees_the_queue_and_can_resolve(owner_client: TestClient, user_client: TestClient, db: Path) -> None:
    user_client.post("/bank/1/feedback", data={"kind": "unclear", "detail": "题干有歧义"})

    queue = owner_client.get("/admin/feedback").text
    assert "公共题" in queue, "管理员要看着题判断"
    assert "题干有歧义" in queue

    with create_session_factory(create_db_engine(db))() as s:
        fid = s.execute(select(QuestionFeedback)).scalars().one().id
    r = owner_client.post(f"/admin/feedback/{fid}/resolved", data={"note": "已改"},
                          follow_redirects=False)
    assert r.status_code == 302

    with create_session_factory(create_db_engine(db))() as s:
        row = s.get(QuestionFeedback, fid)
        assert row.status == "resolved"
        assert "已改" in (row.detail or "")


def test_owner_can_dismiss(owner_client: TestClient, user_client: TestClient, db: Path) -> None:
    user_client.post("/bank/1/feedback", data={"kind": "other", "detail": "随便说说"})
    with create_session_factory(create_db_engine(db))() as s:
        fid = s.execute(select(QuestionFeedback)).scalars().one().id

    owner_client.post(f"/admin/feedback/{fid}/dismissed", data={"note": "不是问题"})
    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(QuestionFeedback, fid).status == "dismissed"


def test_normal_user_cannot_open_the_admin_queue(user_client: TestClient) -> None:
    r = user_client.get("/admin/feedback")
    assert r.status_code == 403


def test_owner_can_report_again_after_resolution(
    owner_client: TestClient, user_client: TestClient, db: Path
) -> None:
    """**部分唯一索引的产物**：处理完之后可以再报（问题没修好是真实情况）。"""
    user_client.post("/bank/1/feedback", data={"kind": "unclear", "detail": "第一次"})
    with create_session_factory(create_db_engine(db))() as s:
        fid = s.execute(select(QuestionFeedback)).scalars().one().id
    r = owner_client.post(f"/admin/feedback/{fid}/resolved", data={"note": "改了"},
                          follow_redirects=False)
    assert r.status_code == 302, "owner 的处理动作本身要成功"
    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(QuestionFeedback, fid).status == "resolved", "处理结果必须落库"

    r = user_client.post("/bank/1/feedback", data={"kind": "wrong", "detail": "还是不对"},
                         follow_redirects=False)
    assert r.status_code == 302, f"再报应当成功，实际 {r.status_code}：{r.text[:200]}"
    with create_session_factory(create_db_engine(db))() as s:
        assert len(s.execute(select(QuestionFeedback)).scalars().all()) == 2
