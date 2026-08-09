import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps

from sqlalchemy import create_engine, event, func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel

from .errors import DuplicateSource, StorageError
from .models import (
    Question,
    QuestionStatus,
    QuestionType,
    Session,
    SessionStatus,
    Source,
    TaskLog,
    UserPick,
)

_engine: Engine | None = None
_factory: sessionmaker | None = None
_db_url: str | None = None
_import_lock = threading.Lock()


def init_db(db_url: str) -> None:
    """连接 SQLite 并建表；任何 sqlite 异常包装为 StorageError。

    重建时先 dispose 旧引擎（防止旧连接句柄锁住数据库文件，Windows 上替换文件会失败）。
    """
    global _engine, _factory, _db_url
    if _engine is not None:
        _engine.dispose()
    _db_url = db_url
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    try:
        _engine = create_engine(db_url, connect_args=connect_args)
        if db_url.startswith("sqlite"):
            event.listen(_engine, "connect", _enable_sqlite_foreign_keys)
            event.listen(_engine, "connect", _enable_sqlite_wal)
        SQLModel.metadata.create_all(_engine)
        if db_url.startswith("sqlite"):
            _migrate_selected_at(_engine)
            _migrate_backfill_today_selected_at(_engine)
            _migrate_embedding(_engine)
            _migrate_attempt_level(_engine)
            _migrate_cleanup_empty_sessions(_engine)
            _migrate_session_user(_engine)
    except SQLAlchemyError as e:
        raise StorageError(f"cannot init database {db_url}: {e}") from e
    _factory = sessionmaker(bind=_engine, class_=DBSession, expire_on_commit=False)


def close() -> None:
    """释放引擎连接（导入恢复时用）；init_db 可重建。"""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


def db_url() -> str:
    """当前数据库 URL（init_db 记录；未初始化返回默认 sqlite 路径）。"""
    return _db_url or "sqlite:///data/interview.db"


def engine() -> Engine:
    """当前引擎（未初始化时惰性 init）。"""
    if _engine is None:
        init_db(db_url())
    return _engine


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


def _migrate_session_user(engine: Engine) -> None:
    """多用户迁移：sessions 表补 user_id 列（旧单用户数据由注册 owner 时回填）。"""
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(sessions)"))]
        if "user_id" not in cols:
            conn.execute(text("ALTER TABLE sessions ADD COLUMN user_id INTEGER"))


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


def _enable_sqlite_wal(dbapi_connection, connection_record) -> None:
    """多用户并发读写：WAL 模式（读不阻塞写，20 用户规模更稳）。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
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
def backfill_owner_data(session: DBSession, user_id: int) -> None:
    """首个用户（owner）注册时回填旧单用户数据：sessions 归属 + 今日题转 picks。"""
    from sqlalchemy import update

    session.execute(
        update(Session).where(Session.user_id.is_(None)).values(user_id=user_id)
    )
    today_qs = list(
        session.scalars(
            select(Question).where(Question.status == QuestionStatus.today)
        )
    )
    for q in today_qs:
        session.add(
            UserPick(
                user_id=user_id,
                question_id=q.id,
                picked_at=q.selected_at or datetime.now(),
            )
        )
    commit(session)


@_wrap_storage
def list_today_questions(session: DBSession) -> list[Question]:
    """今日题目：status=today 且被选为今日（selected_at 属今天）的题。

    昨日及更早未完成的题不在此列（由 recycle_stale_today 回收回 pending 池）。
    """
    stmt = (
        select(Question)
        .where(Question.status == QuestionStatus.today)
        .where(Question.selected_at >= _today_start())
        .order_by(Question.created_at)
    )
    return list(session.scalars(stmt))


@_wrap_storage
def list_questions_by_date(session: DBSession, date_str: str) -> list[Question]:
    """按被选为今日题目的日期（selected_at 落于当日零点~次日零点）查题，不限 status。

    供今日页日历单选回看某一天的题目（含被回收回 pending 的未完成题）。
    """
    day = datetime.strptime(date_str, "%Y-%m-%d")
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    stmt = (
        select(Question)
        .where(Question.selected_at >= start, Question.selected_at < end)
        .order_by(Question.created_at)
    )
    return list(session.scalars(stmt))


@_wrap_storage
def recycle_stale_today(session: DBSession) -> int:
    """回收昨日及更早被选为今日、但未完成的题回 pending 池（今日列表按日期过滤后不再展示，
    回池后可在后续选题中再次被选中）；已完成（有 finished 会话）的题保持 today 不回池。"""
    stmt = (
        select(Question)
        .where(Question.status == QuestionStatus.today)
        .where(or_(Question.selected_at.is_(None), Question.selected_at < _today_start()))
    )
    stale = list(session.scalars(stmt))
    recycled = 0
    for q in stale:
        finished = (
            session.scalars(
                select(Session)
                .where(
                    Session.question_id == q.id,
                    Session.status == SessionStatus.finished,
                )
                .limit(1)
            ).first()
            is not None
        )
        if finished:
            continue
        q.status = QuestionStatus.pending
        recycled += 1
    if recycled:
        commit(session)
    return recycled


def _today_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


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
