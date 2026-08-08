import re
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from functools import wraps

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel

from .errors import DuplicateSource, StorageError
from .models import Question, QuestionStatus, QuestionType, Source, TaskLog

_engine: Engine | None = None
_factory: sessionmaker | None = None


def init_db(db_url: str) -> None:
    """连接 SQLite 并建表；任何 sqlite 异常包装为 StorageError。"""
    global _engine, _factory
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    try:
        _engine = create_engine(db_url, connect_args=connect_args)
        if db_url.startswith("sqlite"):
            event.listen(_engine, "connect", _enable_sqlite_foreign_keys)
        SQLModel.metadata.create_all(_engine)
        if db_url.startswith("sqlite"):
            _migrate_selected_at(_engine)
            _migrate_backfill_today_selected_at(_engine)
            _migrate_embedding(_engine)
            _migrate_attempt_level(_engine)
            _migrate_cleanup_empty_sessions(_engine)
    except SQLAlchemyError as e:
        raise StorageError(f"cannot init database {db_url}: {e}") from e
    _factory = sessionmaker(bind=_engine, class_=DBSession, expire_on_commit=False)


def _migrate_selected_at(engine: Engine) -> None:
    """轻量迁移：旧库 questions 表补 selected_at 列，回填已选今日题目。"""
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(questions)"))]
        if "selected_at" in cols:
            return
        conn.execute(text("ALTER TABLE questions ADD COLUMN selected_at DATETIME"))
        conn.execute(
            text("UPDATE questions SET selected_at = created_at WHERE status = 'today'")
        )


def _migrate_backfill_today_selected_at(engine: Engine) -> None:
    """幂等修复：status='today' 但 selected_at 为 NULL 的题补齐（历史/今日不同步兜底）。

    根因：selected_at 迁移后曾有旧代码运行 pick_questions（只设 status 不设 selected_at）。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE questions SET selected_at = created_at "
                "WHERE status = 'today' AND selected_at IS NULL"
            )
        )


def _migrate_embedding(engine: Engine) -> None:
    """轻量迁移：旧库 questions 表补 embedding 列（NULL 由首次去重自动补算）。"""
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(questions)"))]
        if "embedding" in cols:
            return
        conn.execute(text("ALTER TABLE questions ADD COLUMN embedding BLOB"))


def _migrate_attempt_level(engine: Engine) -> None:
    """轻量迁移：旧库 attempts 表补 level 列（深挖追问层级，旧数据为 NULL）。"""
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(attempts)"))]
        if "level" in cols:
            return
        conn.execute(text("ALTER TABLE attempts ADD COLUMN level INTEGER"))


def _migrate_cleanup_empty_sessions(engine: Engine) -> None:
    """幂等清理：删除既无回答（attempts）也无判分（judgments）的空壳会话。

    修复前"每次点击题目卡片都新建会话"产生的空壳（如首题 17 会话），
    启动时自动清理；只删无子行会话，不受 FK 影响。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM sessions "
                "WHERE id NOT IN (SELECT DISTINCT session_id FROM attempts) "
                "AND id NOT IN (SELECT DISTINCT session_id FROM judgments)"
            )
        )


def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite 默认 PRAGMA foreign_keys=OFF，逐连接开启以强制外键。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@contextmanager
def get_session() -> Generator[DBSession, None, None]:
    """依赖注入用会话（FastAPI 与流水线共用）。"""
    if _factory is None:
        raise StorageError("database not initialized: call init_db first")
    with _factory() as session:
        yield session


def commit(session: DBSession) -> None:
    """统一提交入口：UNIQUE 冲突特殊化为 DuplicateSource，其余包装为 StorageError。"""
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        raise _to_storage_error(e) from e
    except SQLAlchemyError as e:
        session.rollback()
        raise StorageError(f"database operation failed: {e}") from e


def _to_storage_error(e: IntegrityError) -> StorageError:
    if "source_hash" in str(e) and "UNIQUE" in str(e):
        return DuplicateSource(_source_hash_from(e))
    return StorageError(f"database constraint violation: {e}")


_INSERT_COLUMNS = re.compile(r"INSERT INTO \w+ \(([^)]*)\)")


def _source_hash_from(e: IntegrityError) -> str:
    """从 ORM flush 的 IntegrityError 提取冲突的 source_hash 值。"""
    params = getattr(e, "params", None)
    if isinstance(params, dict):
        return str(params.get("source_hash", "unknown"))
    match = _INSERT_COLUMNS.search(str(e.statement))
    if isinstance(params, (list, tuple)) and match:
        columns = [c.strip().strip('"') for c in match.group(1).split(",")]
        values = dict(zip(columns, params))
        if "source_hash" in values:
            return str(values["source_hash"])
    return "unknown"


def _wrap_storage(func):
    @wraps(func)
    def inner(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SQLAlchemyError as e:
            raise StorageError(f"database operation failed: {e}") from e

    return inner


@_wrap_storage
def list_today_questions(session: DBSession) -> list[Question]:
    stmt = (
        select(Question)
        .where(Question.status == QuestionStatus.today)
        .order_by(Question.created_at)
    )
    return list(session.scalars(stmt))


@_wrap_storage
def pick_questions(
    session: DBSession, limit: int, qtype: QuestionType | None = None
) -> list[Question]:
    """从 pending 池按创建顺序取题并标记 today；qtype 用于按题型配额选取（D8）。"""
    stmt = select(Question).where(Question.status == QuestionStatus.pending)
    if qtype is not None:
        stmt = stmt.where(Question.type == qtype)
    stmt = stmt.order_by(Question.created_at).limit(limit)
    picked = list(session.scalars(stmt))
    for q in picked:
        q.status = QuestionStatus.today
        q.selected_at = datetime.now()
    commit(session)
    return picked


@_wrap_storage
def find_source_by_hash(session: DBSession, source_hash: str) -> Source | None:
    return session.scalars(
        select(Source).where(Source.source_hash == source_hash)
    ).first()


@_wrap_storage
def latest_task_log(session: DBSession, task_name: str) -> TaskLog | None:
    stmt = (
        select(TaskLog)
        .where(TaskLog.task_name == task_name)
        .order_by(TaskLog.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


@_wrap_storage
def count_questions_created_since(session: DBSession, since: datetime) -> int:
    """供 D16 每日新入库不重复问题上限计数。"""
    stmt = select(func.count()).select_from(Question).where(Question.created_at >= since)
    return session.scalars(stmt).one()
