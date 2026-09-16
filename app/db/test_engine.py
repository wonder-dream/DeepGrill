"""引擎参数（决策 87）：busy timeout / BEGIN IMMEDIATE / 连接池。

这三个数是**并发写实测**出来的（见 `app/db/__init__.py` 的常量注释），而"实测出来的
数字"最容易在重构里被悄悄改回默认值 —— 所以这里钉住的是**取值本身**，
不是"能建出一个引擎"。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.db import (
    DEFAULT_BUSY_TIMEOUT_SECONDS,
    DEFAULT_MAX_OVERFLOW,
    DEFAULT_POOL_SIZE,
    connect_args_for,
    create_db_engine,
)


def test_connect_args_carry_the_measured_values() -> None:
    args = connect_args_for(Path("whatever.db"))
    assert args["check_same_thread"] is False
    # python sqlite3 的默认 timeout 是 5 秒 —— 那正是并发写 500 的近因
    assert args["timeout"] == DEFAULT_BUSY_TIMEOUT_SECONDS == 30.0
    assert args["isolation_level"] == "IMMEDIATE"


def test_connect_args_can_be_turned_down() -> None:
    """两个开关都留了口子（排查/对照实验要用）。"""
    args = connect_args_for(Path("whatever.db"), busy_timeout=1.5, begin_immediate=False)
    assert args == {"check_same_thread": False, "timeout": 1.5}


def test_engine_uses_the_configured_pool(tmp_dir: Path) -> None:
    engine = create_db_engine(tmp_dir / "pool.db", pool_size=7, max_overflow=11)
    try:
        assert engine.pool.size() == 7
        # QueuePool 没有公开的"最大溢出"读法，只能读私有属性（值本身就是被钉住的东西）
        assert engine.pool._max_overflow == 11
        assert (DEFAULT_POOL_SIZE, DEFAULT_MAX_OVERFLOW) == (20, 30)
    finally:
        engine.dispose()


def test_engine_connections_are_immediate_and_wal(tmp_dir: Path) -> None:
    """`IMMEDIATE` 与 `WAL` 都在**每个新连接**上生效（前者是连接参数，后者是 PRAGMA）。"""
    engine = create_db_engine(tmp_dir / "conn.db")
    try:
        with engine.connect() as conn:
            raw = conn.connection.driver_connection
            assert raw.isolation_level == "IMMEDIATE"
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_deps_engine_cache_key_includes_pool_params(tmp_dir: Path) -> None:
    """池参数必须进 `lru_cache` 的键。

    只按库路径缓存的话，测试里两个不同配置的 app 会共用同一个引擎 ——
    那是"配置静默失效"，本项目已经吃过一次（`create_app(settings=…)` 传进去的配置
    在依赖链里失效，见 `app/main.py` 的注释）。
    """
    from app.deps import _engine_for

    path = str(tmp_dir / "cache.db")
    a = _engine_for(path, 5, 5, 5.0)
    b = _engine_for(path, 9, 5, 5.0)
    try:
        assert a is not b, "池参数不同却拿到同一个引擎 —— 配置会静默失效"
        assert a.pool.size() == 5
        assert b.pool.size() == 9
    finally:
        a.dispose()
        b.dispose()
