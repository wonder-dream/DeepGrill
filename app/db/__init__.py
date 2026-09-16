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

#: DBAPI 的 busy timeout（秒）。**python sqlite3 的默认值是 5 秒**，而每个请求都会把
#: 事务跨在 LLM 调用上（几秒到几十秒）—— 并发写时后面的请求等锁超过 5 秒就被掐断成
#: `database is locked` → 500。实测（20 路并发慢轮次）：默认 5 秒 **18/20 失败**；
#: 30 秒 + `BEGIN IMMEDIATE` 之后失败数降到个位数，剩下的仍是"事务跨在模型调用上"。
DEFAULT_BUSY_TIMEOUT_SECONDS = 30.0

#: 连接池大小。同样因为"连接被跨着 LLM 调用持有"：默认的 5+10 在并发轮次下会耗尽，
#: 等不到连接的请求 30 秒后以 `QueuePool limit ... timed out` 变成 500（实测 18/20）。
DEFAULT_POOL_SIZE = 20
DEFAULT_MAX_OVERFLOW = 30


def connect_args_for(
    path: Path,
    *,
    busy_timeout: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    begin_immediate: bool = True,
) -> dict[str, Any]:
    """sqlite3 的连接参数。

    `check_same_thread=False`：FastAPI 的同步依赖在线程池里跑，一个请求可能
    在不同的线程上开/关同一个连接 —— 而 SQLAlchemy 的连接池本身已经保证
    同一时刻只有一个线程用它。

    `timeout`（DBAPI 的 busy timeout）与 `isolation_level="IMMEDIATE"` 是**并发写**
    的两条实测结论，见上面两个常量的注释与 `docs/v2范围基线.md` 决策 87。

    ⚠️ `IMMEDIATE` 与 `create_db_engine` 里那条"不能设 AUTOCOMMIT"的警告**不冲突**：
    它不是"每条语句自己提交"，而是"写事务一开始就取写锁"，回滚语义完好。
    """
    del path  # 参数保留给"将来要按路径决定参数"的情形，此刻只有一个库
    args: dict[str, Any] = {"check_same_thread": False, "timeout": busy_timeout}
    if begin_immediate:
        args["isolation_level"] = "IMMEDIATE"
    return args


def create_db_engine(
    path: Path,
    *,
    echo: bool = False,
    pool_size: int = DEFAULT_POOL_SIZE,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
    busy_timeout: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    begin_immediate: bool = True,
) -> Engine:
    """建引擎，并把两条 PRAGMA 挂到**每个新连接**上。

    - `foreign_keys=ON`：SQLite 默认**关着**外键。迁移 runner 自己开了它，
      但那是 runner 的连接 —— 应用的连接必须自己再开一次，否则
      `REFERENCES` 只是一句注释（v1 也踩过这个）。
    - `journal_mode=WAL`：单机 2C2G 上读写并发的前提（ADR-0008）。
      它是**持久属性**（写进库文件），每次连接重复设置是幂等的。

    ⚠️ **这里不能设 `isolation_level="AUTOCOMMIT"`**（我曾设过，被测试推翻）。
    AUTOCOMMIT 下每条语句自己提交，于是 `Session.rollback()` **什么也不回滚** ——
    而 ADR-0006 选"库表 + worker"而不用 Redis 的**唯一不可替代收益**就是
    「投递与业务写入同一事务」。实测对照：

    ```
    AUTOCOMMIT: flush 后另一个连接就看得见 → rollback 后仍然是 1 行
    默认(DEFERRED): flush 后另一个连接看不见 → rollback 后是 0 行
    ```

    也就是说：AUTOCOMMIT 让那条理由**变成假话**，而且它不会报错 ——
    任务记录会留下、业务写入却回滚了，正是 v1 那批"卡在中间态"的 bug 形状。

    `pool_size` / `max_overflow` / `busy_timeout` 由调用方（配置）决定，默认值取
    `DEFAULT_*` —— 这三个数都是**并发写**实测出来的，不是拍的（见常量注释）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        echo=echo,
        connect_args=connect_args_for(
            path, busy_timeout=busy_timeout, begin_immediate=begin_immediate
        ),
        pool_size=pool_size,
        max_overflow=max_overflow,
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
