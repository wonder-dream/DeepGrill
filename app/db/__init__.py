"""数据库：引擎、会话、FastAPI 依赖（ADR-0005 的基础设施层）。

三条硬约束，每条都对应 v1 的一次事故：

① **不许在 async 上下文里同步查库**（AGENTS.md §3.3）—— v1 在 async 中间件里
   同步查库，SQLite 写锁争用时全部并发请求停止派发。v2 的做法是把同步会话
   放在 FastAPI 的**同步依赖**里（`def`，不是 `async def`），于是 FastAPI 会
   把它丢进线程池，事件循环不受影响。

② **迁移与启动分离**（AGENTS.md §3.7 / ADR-0011）—— 本模块**绝不建表**。
   库不存在就是不存在，由 `python -m migrations.run` 显式建。

③ **JSON 列必须 `ensure_ascii=False`**（`docs/v1行为规格.md` §8.7，硬性继承）
   —— v1 的实测结论：中文存成 `\\uXXXX` 转义之后，**对 JSON 列做 SQL 文本匹配会
   静默失效**。所以这里提供 `JsonText`，`app/db/models.py` 的 JSON 列一律用它，
   不允许业务代码自己 `json.dumps`。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TEXT, TypeDecorator


def connect_args_for(path: Path) -> dict[str, Any]:
    """sqlite3 的连接参数。

    `check_same_thread=False`：FastAPI 的同步依赖在线程池里跑，一个请求可能
    在不同的线程上开/关同一个连接 —— 而 SQLAlchemy 的连接池本身已经保证
    同一时刻只有一个线程用它。
    """
    del path  # 参数保留给"将来要按路径决定参数"的情形，此刻只有一个库
    return {"check_same_thread": False}


def create_db_engine(path: Path, *, echo: bool = False) -> Engine:
    """建引擎，并把两条 PRAGMA 挂到**每个新连接**上。

    - `foreign_keys=ON`：SQLite 默认**关着**外键。迁移 runner 自己开了它，
      但那是 runner 的连接 —— 应用的连接必须自己再开一次，否则
      `REFERENCES` 只是一句注释（v1 也踩过这个）。
    - `journal_mode=WAL`：单机 2C2G 上读写并发的前提（ADR-0008）。
      它是**持久属性**（写进库文件），每次连接重复设置是幂等的。

    `isolation_level="AUTOCOMMIT"` 是 SQLAlchemy 侧的取舍，理由有两条：
    ① **读不留长事务**：默认的 "BEGIN on first statement" 会让一次 SELECT 开启
       事务并一直持有 —— 读到的是快照，且 WAL 模式下会挡住 checkpoint。
       实测形态：一次读之后从别的连接插一行，再读仍是旧值。
    ② 写路径本来就是"一次请求一个会话、成功即 commit"，显式 commit 仍然有效，
       所以没有拿掉事务语义。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        echo=echo,
        connect_args=connect_args_for(path),
        isolation_level="AUTOCOMMIT",
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn: sqlite3.Connection, _record: object) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    return engine


class JsonText(TypeDecorator):
    """JSON 存进 TEXT 列，中文**不转义**（`docs/v1行为规格.md` §8.7）。

    只实现 `process_bind_param` / `process_result_value`，因为库里那一列是普通
    `TEXT`（迁移文件不许有 SQLAlchemy 依赖，见 ADR-0011）。
    """

    impl = TEXT
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: object) -> str | None:
        del dialect
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: str | None, dialect: object) -> Any:
        del dialect
        if value is None:
            return None
        return json.loads(value)


class Session(SQLAlchemySession):
    """会话的薄子类：加一个**认表名**的 `one`。

    为什么需要它：v2 的仓储层按领域分（`bank/repository.py` 只碰题目表…），
    而"取一行"这个动作要重复很多次。`session.get(Model, id)` 要求调用方知道
    模型类 —— 而模型集中在 `app/db/models.py`，领域层因此要多 import 一个类。
    `session.one("questions", 12)` 让领域层只记表名（与它自己的 SQL 一致），
    同时仍然是**类型安全**的（表名错在运行时立刻 KeyError，不会静默查错表）。
    """

    def one(self, table_name: str, pk: object) -> object | None:
        from app.db import models

        for table, cls in models.CLASS_BY_TABLE.items():
            if table.name == table_name:
                return self.get(cls, pk)
        raise KeyError(f"没有映射的表：{table_name}")


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """会话工厂。`expire_on_commit=False`：提交后还要读对象（返回响应时）。"""
    return sessionmaker(
        bind=engine, class_=Session, expire_on_commit=False, future=True
    )

def make_session_dependency(factory: sessionmaker[Session]):
    """把会话工厂包成 FastAPI 依赖。

    **必须是同步 `def`**（见模块头 ①）：FastAPI 见到同步依赖会丢进线程池，
    于是同步 SQLAlchemy 不会阻塞事件循环。

    事务边界在这个依赖里：请求成功提交、抛异常回滚。业务代码因此不需要
    自己 commit —— v1 的 31 处 `db.commit()` 散落在 handler 里，是"事务由
    handler 手控"的直接后果。
    """

    def _dependency() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _dependency
