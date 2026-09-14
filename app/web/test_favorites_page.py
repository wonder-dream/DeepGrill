"""收藏夹的页面测试（决策 63）。

页面这条通路测的是三件领域测试看不见的事：

· **动作是 POST + 302 回原页**（ADR-0004：状态住 URL 与服务端）—— 刷新不重复提交
· **未登录点收藏**回登录页，而不是撞一堵 403
· **收藏状态在页面上看得见**：题目详情页的按钮会翻转，列表页有 ★，`/me` 有入口
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Question, User
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


def _quota_line(client) -> str:
    """页面上的额度行 —— 数字从常量算（决策 71 标定过一次，别再写死）。"""
    from app.account import service as account

    return f"还剩 <strong>{account.DAILY_UNITS} / {account.DAILY_UNITS}</strong> 点"

@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "favpage.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="fav@local", username="fav", password_hash=_HASH, role="user"))
        s.flush()
        s.add(Question(id=1, kind="knowledge", stem="公共题一", difficulty=3,
                       origin="seed", visibility="public"))
        s.add(Question(id=2, kind="knowledge", stem="公共题二", difficulty=3,
                       origin="seed", visibility="public"))
        s.commit()
    return path


@pytest.fixture
def app(db: Path):
    return create_app(Settings(database_path=db))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "fav@local", "password": PASSWORD})
        yield c


def _favorite_ids(db: Path, user_id: int = 2) -> list[int]:
    from sqlalchemy import select

    from app.db.models import UserFavorite

    with create_session_factory(create_db_engine(db))() as s:
        return [
            r[0]
            for r in s.execute(
                select(UserFavorite.question_id).where(UserFavorite.user_id == user_id)
            ).all()
        ]


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
def test_favorite_button_round_trip(client: TestClient, db: Path) -> None:
    assert "收藏这道题" in client.get("/bank/1").text
    r = client.post("/bank/1/favorite", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/bank/1?favorite=ok"
    assert _favorite_ids(db) == [1]

    # 302 落在带 `?favorite=ok` 的地址上 —— 那一页才显示"已收藏"这句提示
    body = client.get(r.headers["location"]).text
    assert "已收藏" in body
    # 状态本身住服务端：不带参数重新打开也仍然是"取消收藏"
    plain = client.get("/bank/1").text
    assert "取消收藏" in plain
    assert "已收藏" not in plain, "提示只在动作之后那一次出现"


def test_clicking_twice_does_not_duplicate(client: TestClient, db: Path) -> None:
    """按钮连点两次：库里一行，第二次也不报错（决策 63 的"幂等"）。"""
    client.post("/bank/1/favorite")
    r = client.post("/bank/1/favorite", follow_redirects=False)
    assert r.status_code == 302
    assert _favorite_ids(db) == [1]


def test_unfavorite_round_trip(client: TestClient, db: Path) -> None:
    client.post("/bank/1/favorite")
    r = client.post("/bank/1/favorite/remove", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/bank/1?favorite=off"
    assert _favorite_ids(db) == []
    assert "收藏这道题" in client.get("/bank/1").text


def test_unfavorite_without_being_favorited_is_not_an_error(client: TestClient) -> None:
    assert client.post("/bank/1/favorite/remove", follow_redirects=False).status_code == 302


def test_anonymous_favorite_goes_to_login_instead_of_403(app) -> None:
    """未登录点收藏 = 正常误触 → 送登录页。**不是** 403 一堵墙。"""
    with TestClient(app) as c:
        r = c.post("/bank/1/favorite", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"


def test_favorite_unknown_question_is_404(client: TestClient) -> None:
    assert client.post("/bank/999/favorite").status_code == 404


def test_favorite_does_not_charge_quota(client: TestClient) -> None:
    """决策 63：收藏**不消耗额度点**。"""
    client.post("/bank/1/favorite")
    assert _quota_line(client) in client.get("/").text


# ---------------------------------------------------------------------------
# 列表页
# ---------------------------------------------------------------------------
def test_list_page_is_empty_before_any_favorite(client: TestClient) -> None:
    body = client.get("/me/favorites").text
    assert "我的收藏" in body
    assert "还没有收藏" in body


def test_list_page_shows_favorites(client: TestClient) -> None:
    client.post("/bank/1/favorite")
    client.post("/bank/2/favorite")
    body = client.get("/me/favorites").text
    assert "共 2 道" in body
    assert "公共题一" in body and "公共题二" in body


def test_list_page_can_unfavorite_inline(client: TestClient, db: Path) -> None:
    client.post("/bank/1/favorite")
    r = client.post("/bank/1/favorite/remove", follow_redirects=False)
    assert r.status_code == 302
    assert "还没有收藏" in client.get("/me/favorites").text


def test_list_page_requires_login(app) -> None:
    with TestClient(app) as c:
        r = c.get("/me/favorites", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# 状态在别处也看得见
# ---------------------------------------------------------------------------
def test_bank_list_marks_favorites_with_a_star(client: TestClient) -> None:
    """列表页的 ★ 是**批量**查出来的（一页一次查询，不逐题查库）。"""
    assert "★" not in client.get("/bank").text
    client.post("/bank/1/favorite")
    body = client.get("/bank").text
    assert "★" in body
    assert body.count("★") == 1, "只该有一道题被标星"


def test_me_page_has_an_entry_to_favorites(client: TestClient) -> None:
    client.post("/bank/1/favorite")
    body = client.get("/me").text
    assert "/me/favorites" in body
    assert "收藏 1 道" in body


def test_home_has_an_entry_to_favorites(client: TestClient) -> None:
    """决策 63：收藏夹**是首页的一个入口**。"""
    assert "/me/favorites" in client.get("/").text
