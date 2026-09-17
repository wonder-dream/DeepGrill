"""静态资源的版号**由文件内容算出来** —— 一条以前靠人记得加一、并因此踩过两次的链路。

坏形态长这样：`app.css` 带着 `Cache-Control: public, max-age=3600`（`app/main.py`
的 `STATIC_MAX_AGE`），而文件名里没有内容指纹。模板里手写的 `?v=30` 忘了加一，于是
**改动静默不生效**：刷新、强刷都还是旧样式（用户报过两次：一次是"放弃本场"按钮文字
没居中，一次是整版样式没换）。现在版号是内容哈希，改文件就自动是新 URL。

三条断言各自钉一件事：

· 渲染出来的 URL 里那个哈希，**就是文件的 sha256 前 12 位** —— 不是手写的常量
· 同一份文件渲染两次结果相同 —— 版号是确定的，不会每次请求都变
· 连手写的 `/static/x?v=3` 也会被重算 —— 新模板抄了老写法也不会静默用旧文件
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import User
from app.main import create_app
from app.security import hash_password
from app.web.templating import STATIC_DIR, asset
from migrations._runner import migrate

PASSWORD = "secret123"


def _digest(name: str) -> str:
    return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]


@pytest.fixture
def client(tmp_dir: Path):
    db = tmp_dir / "asset.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(User(id=2, email="asset@local", username="asset",
                   password_hash=hash_password(PASSWORD), role="user"))
        s.commit()
    with TestClient(create_app(Settings(database_path=db))) as c:
        yield c


def test_asset_url_version_is_the_file_content_hash() -> None:
    """版号 = 文件内容的 sha256 前 12 位（不是谁手填的数字）。"""
    assert asset("app.css") == f"/static/app.css?v={_digest('app.css')}"
    assert asset("layout.js") == f"/static/layout.js?v={_digest('layout.js')}"


def test_the_rendered_page_points_at_the_current_content(client: TestClient) -> None:
    """页面上的资源 URL 必须指向**现在磁盘上这一份**。

    这条是给"改了 CSS 却没人加版号"那个坏形态的回归测试：只要文件变了，
    算出来的 URL 就跟着变，没有"记得加一"这一步。
    """
    body = client.get("/").text
    assert f'href="/static/app.css?v={_digest("app.css")}"' in body
    assert f'src="/static/layout.js?v={_digest("layout.js")}"' in body
    assert f'href="/static/favicon.ico?v={_digest("favicon.ico")}"' in body


def test_the_same_file_gives_the_same_url_twice(client: TestClient) -> None:
    """确定性：同一份文件渲染两次给同一个 URL（否则缓存等于没有）。"""
    first = client.get("/").text
    second = client.get("/").text
    assert asset("app.css") in first
    assert asset("app.css") in second


def test_a_hand_written_version_is_recomputed() -> None:
    """漏下来的手写 `?v=3` 会被重算 —— 新模板抄老写法也不会静默用旧文件。

    兜底那一步在 `render()` 里；这里直接验那条替换规则（它同时是"改文件不必加版号"
    的另一道保险）。
    """
    from app.web.templating import _HAND_WRITTEN_ASSET_URL

    stale = '<script src="/static/interview.js?v=4"></script>'
    assert _HAND_WRITTEN_ASSET_URL.sub(lambda m: asset(m.group(1)), stale) == (
        f'<script src="{asset("interview.js")}"></script>'
    )


def test_the_loaded_js_is_the_version_without_the_back_blocker(client: TestClient) -> None:
    """加载到的那个 js 必须是**撤掉拦后退挡板**之后的那份（决策 99）。

    "浏览器拿的是旧文件"这个坑我们踩过，而它没有任何报错 —— 所以连"文件里应该是
    什么内容"也钉一条：`interview.js` 里不许再有 `pushState` / `popstate`。
    """
    digest = _digest("interview.js")
    served = client.get(f"/static/interview.js?v={digest}")
    assert served.status_code == 200
    assert "pushState" not in served.text
    assert "popstate" not in served.text
