"""账号域的测试：注册要邀请码、登录发令牌、令牌进 HttpOnly cookie。

这些断言对应 `docs/v1行为规格.md` §9（v1 这部分做对了，整套继承）与决策 6
（邀请码注册、不做邮箱验证码）。

**测试用真 HTTP 流程**（TestClient 的 POST 表单 + cookie jar），不直接调 service ——
因为"cookie 设对了没有"只有走一遍响应才测得到，而那一层正是最容易写错的地方
（Secure/SameSite/HttpOnly 漏一个，本机测试全绿、线上被 XSS 偷走）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.account import repository
from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.deps import SESSION_COOKIE
from app.main import create_app
from app.security import hash_password, token_hash
from migrations._runner import migrate


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "auth.db"
    migrate(path)
    return path


@pytest.fixture
def settings(db: Path) -> Settings:
    return Settings(database_path=db)


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


def make_invite(db: Path, code: str = "INVITE-1", expires_at: str | None = None) -> None:
    with create_session_factory(create_db_engine(db))() as s:
        repository.create_invite(s, code, expires_at=expires_at)
        s.commit()


def add_user(db: Path, email: str = "u@local", password: str = "secret123") -> int:
    """建一个用户。

    ⚠️ 口令哈希**每次跑算一遍会拖慢整个测试套件**：`scrypt(n=2^14)` 是刻意昂贵的
    （约 50ms 一次），而这个夹具被十几条测试用到。所以这里缓存一份 ——
    **不改参数**（那等于让测试跑在与线上不同的密码学配置下，是拿安全性换时间）。
    缓存的语义也正好与线上一致：同一份口令哈希被反复校验。
    """
    global _CACHED_HASH
    if _CACHED_HASH is None:
        _CACHED_HASH = hash_password(password)
    with create_session_factory(create_db_engine(db))() as s:
        user = repository.create_user(
            s, email=email, username="u", password_hash=_CACHED_HASH
        )
        s.commit()
        return user.id


#: `hash_password("secret123")` 的缓存（见 `add_user`）。
_CACHED_HASH: str | None = None


def _token_rows(db: Path) -> int:
    """用**另一条连接**数令牌行 —— 也就是"浏览器紧接着发的那个请求会看到什么"。"""
    import sqlite3

    con = sqlite3.connect(str(db))
    try:
        return int(con.execute("SELECT COUNT(*) FROM user_tokens").fetchone()[0])
    finally:
        con.close()


def test_login_commits_before_the_redirect_is_sent(db: Path, settings: Settings) -> None:
    """登录的令牌必须在 **302 发出之前**落库（注册、退登、收藏、作答同理）。

    ⚠️ 这条测试为什么要绕开 TestClient 的常规用法：它等整个 ASGI 调用跑完才返回，
    而**依赖栈的提交发生在响应发出之后**（`fastapi/routing.py`：`function_stack`
    包的正是 `await response(...)`）。于是"响应先于提交"这个窗口在常规用法里看不见。
    这里在外面包一层 ASGI spy，在 `http.response.start` 那一刻用另一条连接读库：
    那时**看得到令牌行 ⇔ 处理器自己提交过**。

    实测（真 uvicorn、第 1 轮）：少了这一句，登录/注册之后紧接着的请求 12/12 与 8/8
    被判成匿名，窗口 9–40ms。
    """
    add_user(db)
    app = create_app(settings)
    seen: list[int] = []

    async def spy_app(scope, receive, send):
        async def spy(message):
            if message["type"] == "http.response.start":
                seen.append(_token_rows(db))
            await send(message)

        await app(scope, receive, spy)

    with TestClient(spy_app) as c:
        r = c.post(
            "/login", data={"email": "u@local", "password": "secret123"}, follow_redirects=False
        )
        assert r.status_code == 302

    assert seen == [1], (
        f"302 发出时令牌行数={seen}（应当是 [1]）：提交必须发生在响应之前，"
        "否则浏览器紧接着的请求读不到这个令牌"
    )


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
def test_register_requires_a_valid_invite_code(client: TestClient) -> None:
    r = client.post(
        "/register",
        data={"email": "a@local", "username": "a", "password": "secret123", "invite_code": "NOPE"},
    )
    assert r.status_code == 200  # 渲染回注册页，不是重定向
    assert "邀请码无效" in r.text


def test_register_then_logs_in_and_consumes_the_invite(client: TestClient, db: Path) -> None:
    make_invite(db, "INVITE-OK")
    r = client.post(
        "/register",
        data={"email": "a@local", "username": "a", "password": "secret123", "invite_code": "INVITE-OK"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/me"
    assert SESSION_COOKIE in r.cookies, "注册后应当直接登录（同一个事务里签发令牌）"

    # 邀请码必须被标记已用，且**同一个事务**里完成 —— 所以不可能"注册成功但码没消耗"
    with create_session_factory(create_db_engine(db))() as s:
        invite = repository.find_invite(s, "INVITE-OK")
        assert invite is not None and invite.used_by is not None


def test_invite_cannot_be_used_twice(client: TestClient, db: Path) -> None:
    make_invite(db, "ONCE")
    first = client.post(
        "/register",
        data={"email": "a@local", "username": "a", "password": "secret123", "invite_code": "ONCE"},
        follow_redirects=False,
    )
    assert first.status_code == 302
    client.post("/logout")
    second = client.post(
        "/register",
        data={"email": "b@local", "username": "b", "password": "secret123", "invite_code": "ONCE"},
    )
    assert "邀请码无效" in second.text


def test_expired_invite_is_refused(client: TestClient, db: Path) -> None:
    make_invite(db, "OLD", expires_at="2000-01-01 00:00:00")
    r = client.post(
        "/register",
        data={"email": "a@local", "username": "a", "password": "secret123", "invite_code": "OLD"},
    )
    assert "邀请码无效" in r.text


def test_short_password_is_refused(client: TestClient, db: Path) -> None:
    make_invite(db, "OK")
    r = client.post(
        "/register",
        data={"email": "a@local", "username": "a", "password": "short", "invite_code": "OK"},
    )
    assert "至少 8 位" in r.text


def test_duplicate_email_is_refused(client: TestClient, db: Path) -> None:
    make_invite(db, "K1")
    make_invite(db, "K2")
    data = {"email": "a@local", "username": "a", "password": "secret123"}
    client.post("/register", data={**data, "invite_code": "K1"}, follow_redirects=False)
    r = client.post("/register", data={**data, "invite_code": "K2"})
    assert "已经注册过" in r.text


# ---------------------------------------------------------------------------
# 登录 / 退出
# ---------------------------------------------------------------------------
def test_login_sets_hardened_cookie(client: TestClient, db: Path) -> None:
    """**cookie 三个属性都要在**（§9）：漏一个，本机测试全绿而线上可被 XSS 偷。"""
    add_user(db)
    r = client.post(
        "/login", data={"email": "u@local", "password": "secret123"}, follow_redirects=False
    )
    assert r.status_code == 302

    set_cookie = r.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")
    assert "Max-Age=" in set_cookie, "cookie 不能比令牌活得久（否则表现为莫名 403）"


def test_login_failure_does_not_reveal_whether_the_email_exists(
    client: TestClient, db: Path
) -> None:
    """账号枚举防线：邮箱不存在与口令不对必须是**同一句话**。"""
    add_user(db, "known@local")
    wrong_password = client.post(
        "/login", data={"email": "known@local", "password": "nope-nope"}
    )
    unknown_email = client.post(
        "/login", data={"email": "ghost@local", "password": "nope-nope"}
    )
    assert wrong_password.status_code == unknown_email.status_code == 200
    assert "邮箱或口令不正确" in wrong_password.text
    assert "邮箱或口令不正确" in unknown_email.text


def test_logout_revokes_the_token(client: TestClient, db: Path) -> None:
    add_user(db)
    client.post("/login", data={"email": "u@local", "password": "secret123"})
    token = client.cookies[SESSION_COOKIE]

    assert client.get("/me").status_code == 200
    client.post("/logout", follow_redirects=False)

    # 令牌在库里被删掉了 —— 所以重放同一个 cookie 也不认
    with create_session_factory(create_db_engine(db))() as s:
        assert repository.user_for_token(s, token_hash(token)) is None


def test_expired_token_is_refused_and_cleaned(client: TestClient, db: Path) -> None:
    """过期令牌必须不认，并**顺手删掉那一行**（惰性清理，v1 的做法）。"""
    user_id = add_user(db)
    with create_session_factory(create_db_engine(db))() as s:
        repository.issue_token(s, user_id, token_hash("stale"))
        from app.db.models import UserToken

        row = s.query(UserToken).filter(UserToken.token_hash == token_hash("stale")).one()
        row.expires_at = "2000-01-01 00:00:00"
        s.commit()

    client.cookies.set(SESSION_COOKIE, "stale")
    assert client.get("/me", follow_redirects=False).status_code == 302

    with create_session_factory(create_db_engine(db))() as s:
        from app.db.models import UserToken

        assert s.query(UserToken).filter(UserToken.token_hash == token_hash("stale")).count() == 0


# ---------------------------------------------------------------------------
# 页面守卫与额度
# ---------------------------------------------------------------------------
def test_me_requires_login(client: TestClient) -> None:
    r = client.get("/me", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_me_shows_remaining_quota(client: TestClient, db: Path) -> None:
    from app.account import service

    user_id = add_user(db)
    with create_session_factory(create_db_engine(db))() as s:
        service.spend_units(s, user_id, "interview")
        s.commit()

    client.post("/login", data={"email": "u@local", "password": "secret123"})
    r = client.get("/me")
    assert r.status_code == 200
    # 数字从常量算，不写死：每日上限标定过一次（决策 71，20 → 8），
    # 写死的话每次标定都要回来改这条断言 —— 而漏改的断言仍然是绿的
    left = service.DAILY_UNITS - service.COST["interview"]
    assert f"{left} / {service.DAILY_UNITS}" in r.text, (
        f"扣掉一场模拟面试（{service.COST['interview']} 点）后应当剩 {left}"
    )


def test_login_page_redirects_when_already_logged_in(client: TestClient, db: Path) -> None:
    add_user(db)
    client.post("/login", data={"email": "u@local", "password": "secret123"})
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/me"
