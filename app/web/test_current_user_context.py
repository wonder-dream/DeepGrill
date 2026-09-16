"""「当前是谁」怎么到达模板 —— 一条被静默破坏过一次的链路。

两个真实缺陷构成了这个文件：

① **首页从来没传 `user`。** `render()` 只写了默认值 `{"user": None}`，靠每个页面
   自己把用户塞进 context。首页漏了，于是**已登录用户看到的主按钮是"从题库挑一道
   题开始"**，导航栏也写着"登录" —— 页面上没有一个字是错的，只是它永远那样。
   现在默认值来自 `request.state.user`，不再依赖"每个调用点都记得"。

② **错误页曾经会自己 500。** 请求失败时依赖的 `finally` 会先 `rollback()`，而
   rollback 让该会话里所有对象的已加载属性失效；异常处理器随后渲染错误页，读
   `user.username` 就撞 `DetachedInstanceError`。所以 `request.state.user` 放的是
   一份**值的快照**（`deps.CurrentUser`），不是那个 ORM 对象。
   下面那条断言（403 页面里也有用户名）就是钉住它的：换成 ORM 对象立刻变 500。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import User
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


@pytest.fixture
def app(tmp_dir: Path):
    db = tmp_dir / "identity.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(User(id=2, email="who@local", username="who", password_hash=_HASH, role="user"))
        s.commit()
    return create_app(Settings(database_path=db))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> None:
    assert client.post(
        "/login", data={"email": "who@local", "password": PASSWORD}, follow_redirects=False
    ).status_code == 302


def test_anonymous_pages_offer_login(client: TestClient) -> None:
    """匿名页要有**一条**去登录的路。

    决策 96 之后它的文案是「登录/注册」：注册不单设入口（登录页自己带通往注册的
    链接），所以这里断言的是"路在"，不是某个具体措辞。
    """
    body = client.get("/").text
    assert 'href="/login"' in body
    assert "登录/注册" in body
    assert ">退出<" not in body


def test_home_knows_who_is_logged_in_even_though_it_never_passed_user(
    client: TestClient,
) -> None:
    """首页是那个漏传 `user` 的页面 —— 它现在必须知道当前是谁。

    断言选在**导航栏的用户名**上（首页自己不渲染用户名），所以它证明的是
    "默认值来自 request"，而不是"首页记得传了"。
    """
    _login(client)
    body = client.get("/").text
    assert "class=\"rail-user\">who<" in body
    assert ">退出<" in body
    assert ">登录<" not in body


def test_error_page_keeps_the_navigation_intact(client: TestClient) -> None:
    """异常处理器渲染的错误页也要有正确的导航栏，**而且不能自己 500**。

    为什么选 `POST /me/delete`（确认短语不对 → `InvalidInput`）：它**没有被路由自己
    捕获**，所以会一路走到 `main.py` 的 `AppError` 处理器。这条路上的会话已经被
    `AsyncExitStack` 中间件回滚并关掉了（FastAPI 0.106 之后，`yield` 依赖的收尾在
    中间件里、而处理器在路由里）—— 那一刻读 `user.username`，如果 `request.state.user`
    放的是 ORM 对象，就是 `DetachedInstanceError`，错误页自己变成 500。

    ⚠️ 别用 `/interview/start` 来测这一条：它在路由内部自己 catch 了 `AppError`
    并 `render`，会话还活着 —— 那样测等于什么都没测（我第一版就是这么写的，
    变异测试当场指出它抓不到"放 ORM 对象"这个改动）。
    """
    _login(client)
    r = client.post("/me/delete", data={"confirm": "no", "password": "x"})
    assert r.status_code == 400
    assert "确认短语不正确" in r.text
    assert "class=\"rail-user\">who<" in r.text, "错误页的导航栏也该知道当前是谁"


def test_a_page_that_renders_inside_the_route_still_knows_who(
    client: TestClient,
) -> None:
    """路由内 catch 之后自己渲染的那种页（`/interview/start` 的降级页）也要对。

    它与上面那条是**两条不同的通路**（会话活着的 vs 已经关掉的），所以两条都要钉。
    """
    _login(client)
    r = client.post("/interview/start", data={"mode": "drill"})
    assert r.status_code == 400
    assert "这场面试没能开始" in r.text
    assert "class=\"rail-user\">who<" in r.text


def test_login_failure_renders_the_form_back_without_a_username(client: TestClient) -> None:
    """登录失败**渲染回同一页**（不重定向、也不给错误码），且不把用户名漏进导航栏。

    表单会把已填的邮箱带回去，所以"页面上没有 who"是错的断言 —— 要断言的是
    **导航栏那一段**没有用户名（`rail-user` 那一行）。
    """
    r = client.post("/login", data={"email": "who@local", "password": "wrong"})
    assert r.status_code == 200, "失败重渲染同一页是刻意的（刷新不丢提示）"
    assert "邮箱或口令不正确" in r.text
    assert "class=\"rail-user\">who<" not in r.text
    assert ">登录<" in r.text
