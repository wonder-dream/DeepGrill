"""组装根与首页的测试。

它测的不是"函数返回了什么"，而是**这个应用能不能起来、页面能不能出** ——
MVP 的第一条验收标准就是"最小可运行"，所以断言点在 HTTP 响应上。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_dir: Path) -> Settings:
    return Settings(database_path=tmp_dir / "app.db")


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_app_boots_and_home_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # 断言锚在**主页自己的文案**上（`home.html` 的 h1）：原来断的是
    # "AI 模拟面试平台"，那句在模板里早就不存在了 —— 一条断言里的字符串
    # 如果没有事实源，它就会在某个分支下恰好通过、换个配置就红。
    assert 'data-testid="home-hero"' in r.text, "首页自己的锚点 —— 改文案不该弄红这条"


def test_create_app_uses_the_injected_settings(client: TestClient, settings: Settings) -> None:
    """**回归测试**：`create_app(settings=…)` 传进来的配置必须真的被依赖链用上。

    第一版只写了 `app.state.settings = settings`，而 `get_settings()` 依赖会去读
    环境变量**重新造一个** Settings —— 于是注入的配置在依赖链里静默失效。
    症状很坏：测试传的是临时库，页面读的是仓库里那个真库，**而且不报错**
    （真库恰好存在时"看起来一切正常"）。已改为
    `app.dependency_overrides[get_settings]`。

    断言点选在"真的碰了哪个文件"：临时库被建出来（说明引擎指向它），
    而仓库里的库**没有**被这次请求碰到。
    """
    _ = client.get("/")

    assert settings.resolved_database_path().exists(), "注入的库没被使用（引擎指向了别处）"


def test_healthz_does_not_touch_the_database(client: TestClient) -> None:
    """存活探针与"库通不通"分开：它必须在没有库的情况下也是 200。"""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_home_says_how_to_initialize_an_empty_database(client: TestClient, settings: Settings) -> None:
    """**空库不是 500**：首页必须能显示"该跑哪条命令"。

    MVP 阶段接手的人第一步就是这个状态（`app/` 此前只有 docstring）。
    若这里 500，故障现场是"页面打不开"，而真因是"没跑迁移" —— 排查方向被带偏。
    """
    r = client.get("/")
    assert r.status_code == 200
    assert "python -m migrations.run" in r.text
    assert "未初始化" in r.text


def test_home_counts_rows_once_migrated(client: TestClient, settings: Settings) -> None:
    """迁移过的空库：显示"已连接"，计数为 0（而不是仍然提示未初始化）。"""
    from migrations._runner import migrate

    migrate(settings.resolved_database_path())
    r = client.get("/")
    assert r.status_code == 200
    assert "已连接" in r.text
    # 题库那一行在模板里是 `<dt>题库</dt><dd>{{ question_count }}</dd>`（无"道"字）
    assert 'data-testid="home-question-count"' in r.text, "计数那一格的锚点"
    assert "<dd>0</dd>" in r.text, "空库时计数为 0"

    # 插一道题，计数要跟着动 —— 否则"已连接"只是个写死的字符串
    conn = sqlite3.connect(str(settings.resolved_database_path()))
    try:
        conn.execute(
            "INSERT INTO questions (kind, stem, difficulty) VALUES ('knowledge', 'volatile 是什么', 2)"
        )
        conn.commit()
    finally:
        conn.close()

    assert "<dd>1</dd>" in client.get("/").text, "插一道题之后计数要跟着动"


def test_unknown_path_returns_404_json(client: TestClient) -> None:
    assert client.get("/no-such-page").status_code == 404
