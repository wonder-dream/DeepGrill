"""把配置、引擎与会话依赖装配到一起（组装用，不含业务）。

**为什么依赖是模块级函数而不是在 `create_app` 里现造**：FastAPI 的 `Depends`
需要在**路由函数定义时**就能拿到一个可调用对象，而路由模块在 import 时就绑定好了。
所以这里用懒单例（`lru_cache`）：同一库路径只开一个连接池。测试用
`app.dependency_overrides` 换掉它 —— 比"每个测试改环境变量再重新 import"干净，
也不会让 app 与测试互相污染。
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from fastapi import Depends
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import create_db_engine, create_session_factory, make_session_dependency


@lru_cache(maxsize=8)
def _engine_for(path_str: str) -> Engine:
    return create_db_engine(Path(path_str))


def get_settings() -> Settings:
    return Settings()


def get_engine(settings: Settings = Depends(get_settings)) -> Engine:
    return _engine_for(str(settings.resolved_database_path()))


def get_session(engine: Engine = Depends(get_engine)) -> Iterator[Session]:
    """每请求一个会话。事务边界在 `make_session_dependency` 里（成功提交、失败回滚）。"""
    factory = create_session_factory(engine)
    yield from make_session_dependency(factory)()
