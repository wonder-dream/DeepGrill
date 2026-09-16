"""后台（邀请码 / 账号 / 额度）的测试。

三条要紧的：
· **只有 owner 能进** —— 它是本地管理工具，不是公开面
· **作废的边界**：未使用的码可作废；**用过的码不能删**（它是发放记录）
· **重置口令必须同时踢下线** —— 否则旧会话还能用，"口令丢了"没被真正解决
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.account import repository as account_repository
from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import InviteCode, User, UserToken
from app.main import create_app
from app.security import hash_password, token_hash
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "admin2.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        owner = s.execute(select(User).where(User.email == "owner@local")).scalars().one()
        owner.password_hash = _HASH
        s.add(User(id=3, email="user@local", username="u", password_hash=_HASH, role="user"))
        s.flush()   # 先落用户：下面的 `used_by=3` 有外键指向它
        s.add(InviteCode(code="DG-UNUSED"))
        s.add(InviteCode(code="DG-USED", used_by=3, used_at="2026-01-01 00:00:00"))
        s.add(InviteCode(code="DG-EXPIRED", expires_at="2000-01-01 00:00:00"))
        s.commit()
    return path


@pytest.fixture
def app(db: Path):
    return create_app(Settings(database_path=db))


@pytest.fixture
def owner_client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "owner@local", "password": PASSWORD})
        yield c


@pytest.fixture
def user_client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "user@local", "password": PASSWORD})
        yield c


def test_admin_home_summarises_everything(owner_client: TestClient) -> None:
    body = owner_client.get("/admin").text
    assert "后台" in body
    assert "未用 1" in body, "三张码分别是未用/已用/已过期"
    assert "已用 1" in body and "已过期 1" in body
    assert "账号" in body


def test_normal_user_cannot_open_admin(user_client: TestClient) -> None:
    for path in ("/admin", "/admin/invites", "/admin/users"):
        r = user_client.get(path)
        assert r.status_code == 403, path


def test_anonymous_cannot_open_admin(app) -> None:
    with TestClient(app) as c:
        assert c.get("/admin", follow_redirects=False).status_code == 403


def test_invite_states_are_computed_from_the_row(owner_client: TestClient) -> None:
    """状态是 `used_by` 与 `expires_at` 的函数 —— 不新增列（会多一个不一致来源）。"""
    body = owner_client.get("/admin/invites").text
    assert "未使用" in body and "已使用" in body and "已过期" in body


def test_create_invite_with_and_without_expiry(owner_client: TestClient, db: Path) -> None:
    owner_client.post("/admin/invites", data={"days": "7"}, follow_redirects=False)
    owner_client.post("/admin/invites", data={"days": ""}, follow_redirects=False)

    with create_session_factory(create_db_engine(db))() as s:
        codes = list(s.execute(select(InviteCode)).scalars())
        generated = [c for c in codes if c.code not in ("DG-UNUSED", "DG-USED", "DG-EXPIRED")]
        assert len(generated) == 2
        with_expiry = [c for c in generated if c.expires_at]
        forever = [c for c in generated if not c.expires_at]
        assert len(with_expiry) == 1 and len(forever) == 1
        assert all(c.created_by == 1 for c in generated), "要记下是谁生成的"


def test_generated_code_is_not_guessable(owner_client: TestClient, db: Path) -> None:
    owner_client.post("/admin/invites", data={"days": ""}, follow_redirects=False)
    with create_session_factory(create_db_engine(db))() as s:
        codes = [c.code for c in s.execute(select(InviteCode)).scalars()]
    new = [c for c in codes if c not in ("DG-UNUSED", "DG-USED", "DG-EXPIRED")][0]
    assert new.startswith("DG-") and len(new) >= 15, f"码太短，容易被猜：{new}"


def test_revoke_only_works_on_unused_codes(owner_client: TestClient, db: Path) -> None:
    r = owner_client.post("/admin/invites/DG-UNUSED/revoke", follow_redirects=False)
    assert r.status_code == 302
    with create_session_factory(create_db_engine(db))() as s:
        assert account_repository.find_invite(s, "DG-UNUSED") is None

    # 用过的码不能删 —— 它是发放记录
    r = owner_client.post("/admin/invites/DG-USED/revoke")
    assert r.status_code == 400
    assert "发放记录" in r.text
    with create_session_factory(create_db_engine(db))() as s:
        assert account_repository.find_invite(s, "DG-USED") is not None


def test_revoke_unknown_code_is_not_found(owner_client: TestClient) -> None:
    assert owner_client.post("/admin/invites/NOPE/revoke").status_code == 404


def test_users_page_shows_quota_per_account(owner_client: TestClient) -> None:
    body = owner_client.get("/admin/users").text
    assert "owner@local" in body and "user@local" in body
    assert "还剩" in body


def test_password_reset_also_kicks_the_user_out(owner_client: TestClient, db: Path) -> None:
    """**重置口令必须撤销全部令牌** —— 否则旧会话还能用，
    "口令丢了"这件事就没被真正解决。"""
    with create_session_factory(create_db_engine(db))() as s:
        s.add(UserToken(token_hash=token_hash("live-token"), user_id=3,
                        expires_at="2099-01-01 00:00:00"))
        s.commit()

    r = owner_client.post("/admin/users/3/password", data={"new_password": "brand-new-pw"},
                          follow_redirects=False)
    assert r.status_code == 302

    with create_session_factory(create_db_engine(db))() as s:
        assert s.execute(select(UserToken).where(UserToken.user_id == 3)).first() is None


def test_password_reset_changes_the_login(app, owner_client: TestClient, db: Path) -> None:
    owner_client.post("/admin/users/3/password", data={"new_password": "brand-new-pw"})

    with TestClient(app) as c:
        r = c.post("/login", data={"email": "user@local", "password": "brand-new-pw"},
                   follow_redirects=False)
        assert r.status_code == 302, "新口令要能登录"
        r = c.post("/login", data={"email": "user@local", "password": PASSWORD},
                   follow_redirects=False)
        assert r.status_code == 200, "旧口令要失效"


def test_password_reset_rejects_a_short_password(owner_client: TestClient, db: Path) -> None:
    r = owner_client.post("/admin/users/3/password", data={"new_password": "short"})
    assert r.status_code == 400
    assert "至少 8 位" in r.text


def test_password_reset_unknown_user(owner_client: TestClient) -> None:
    r = owner_client.post("/admin/users/999/password", data={"new_password": "long-enough-pw"})
    assert r.status_code == 404


def test_create_invite_refuses_a_bogus_validity(owner_client: TestClient, db: Path) -> None:
    """有效期的非法值要**明确拒绝**，不能静默变成"永久有效"。

    原来写的是 `int(days) if days.isdigit() else None`：于是 `-1`、`abc`、`3.5`
    全都变成 None（= 永久）—— 一个输入错误换来一张永不过期的邀请码，而页面上
    什么都不说。留空**仍然**是永久（那是表单的默认，有测试钉着）。
    """
    for bogus in ("-1", "abc", "3.5", "0"):
        r = owner_client.post("/admin/invites", data={"days": bogus}, follow_redirects=False)
        assert r.status_code == 400, f"days={bogus!r} 被接受了"

    with create_session_factory(create_db_engine(db))() as s:
        codes = [c.code for c in s.execute(select(InviteCode)).scalars()]
    assert codes == ["DG-UNUSED", "DG-USED", "DG-EXPIRED"], "非法输入不该留下邀请码"


def test_an_out_of_range_id_is_a_400_not_a_500(owner_client: TestClient) -> None:
    """越界 id 传给驱动会抛 `OverflowError`（SQLite 的绑定参数是 64 位）。

    `/admin/users/2**63/password` 修之前是 500 —— 这是输入错误，不是服务端错误。
    """
    r = owner_client.post(
        f"/admin/users/{2**63}/password", data={"new_password": "long-enough-pw"}
    )
    assert r.status_code == 400
    assert "参数超出范围" in r.text

    r = owner_client.post(f"/admin/quality/{2**63}/resolve", follow_redirects=False)
    assert r.status_code == 400


def test_dashboard_shows_token_usage(owner_client: TestClient, db: Path) -> None:
    """决策 14 的第二道安全网要**看得见** —— 否则它等于没记。"""
    from app.account import service as account_service

    with create_session_factory(create_db_engine(db))() as s:
        account_service.record_tokens(s, 3, 1234)
        s.commit()

    body = owner_client.get("/admin").text
    assert "1234" in body
