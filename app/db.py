import random
import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from functools import wraps

from sqlalchemy import create_engine, delete, event, func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel

from .errors import DuplicateSource, StorageError
from .models import (
    Question,
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
    """连接 SQLite 并建表（绿地重建：新库无历史迁移）。

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
            event.listen(_engine, "connect", _enable_sqlite_busy_timeout)
        SQLModel.metadata.create_all(_engine)
        if db_url.startswith("sqlite"):
            _migrate_user_picks_unique(_engine)
            _migrate_sessions_active_unique(_engine)
            _migrate_users_email(_engine)
            _migrate_backfill_reviewed(_engine)
            _migrate_source_provenance(_engine)
            _migrate_question_feedback_open_unique(_engine)
    except SQLAlchemyError as e:
        raise StorageError(f"cannot init database {db_url}: {e}") from e
    _factory = sessionmaker(bind=_engine, class_=DBSession, expire_on_commit=False)
    from .tags import reload_tags

    reload_tags()  # 词表种子：DB 空时写入 13 分类/81 标签，非空则从 DB 重建（幂等）


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


def _enable_sqlite_busy_timeout(dbapi_connection, connection_record) -> None:
    """WAL 下并发写仍可能冲突：busy_timeout 让写等待而非立即报 locked（原为 500）。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.close()


def _migrate_user_picks_unique(engine: Engine) -> None:
    """幂等迁移：user_picks 加 (user_id, question_id) 唯一约束（绿地重建后模型新增，旧表缺约束）。

    SQLite 不支持 ALTER TABLE ADD CONSTRAINT，需重建表复制数据（按用户+题去重）。
    """
    with engine.begin() as conn:
        indexes = conn.execute(text("PRAGMA index_list('user_picks')")).fetchall()
        if any("uq_user_picks" in (r[1] or "") for r in indexes):
            return
        conn.execute(text("DROP TABLE IF EXISTS user_picks_new"))
        conn.execute(text(
            "CREATE TABLE user_picks_new ("
            " id INTEGER NOT NULL PRIMARY KEY,"
            " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
            " question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,"
            " picked_at DATETIME NOT NULL,"
            " CONSTRAINT uq_user_picks UNIQUE (user_id, question_id))"
        ))
        conn.execute(text(
            "INSERT INTO user_picks_new (id, user_id, question_id, picked_at) "
            "SELECT id, user_id, question_id, MAX(picked_at) FROM user_picks "
            "GROUP BY user_id, question_id"
        ))
        conn.execute(text("DROP TABLE user_picks"))
        conn.execute(text("ALTER TABLE user_picks_new RENAME TO user_picks"))
        conn.execute(text(
            "CREATE INDEX ix_user_picks_user_id ON user_picks (user_id)"
        ))


def _migrate_sessions_active_unique(engine: Engine) -> None:
    """幂等迁移：同用户同题最多一条 active 会话（partial unique index，防并发双开）。"""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_active "
            "ON sessions (user_id, question_id) WHERE status = 'active'"
        ))


def _migrate_users_email(engine: Engine) -> None:
    """幂等迁移：users 表加 email 登录标识（2026-08-12 邮箱验证码注册）。

    旧表（username 登录）无 email 列 → 重建表（email unique + username 降为显示名）
    并清空全部用户数据（用户确认重来：本地库判分/会话本为 0，题库/知识库/词表保留）。
    """
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info('users')")).fetchall()]
        if "email" in cols:
            return
        # 先清引用 users 的子表（FK 约束），再重建 users（表名全部为代码内硬编码常量，非用户输入）
        for _sql in (
            "DELETE FROM user_tokens",
            "DELETE FROM user_picks",
            "DELETE FROM user_favorites",
            "DELETE FROM attempts",
            "DELETE FROM judgments",
            "DELETE FROM sessions",
        ):
            conn.execute(text(_sql))
        conn.execute(text("DROP TABLE users"))
        conn.execute(text(
            "CREATE TABLE users ("
            " id INTEGER NOT NULL PRIMARY KEY,"
            " email VARCHAR NOT NULL,"
            " username VARCHAR NOT NULL,"
            " password_hash VARCHAR NOT NULL,"
            " role VARCHAR NOT NULL,"
            " focus VARCHAR,"
            " focus_lang VARCHAR,"
            " created_at DATETIME NOT NULL,"
            " CONSTRAINT uq_users_email UNIQUE (email))"
        ))
        conn.execute(text("CREATE INDEX ix_users_email ON users (email)"))


def _migrate_backfill_reviewed(engine: Engine) -> None:
    """一次性迁移：存量题目回填为已审核（reviewed_at=created_at）。

    审核门禁上线前，题库所有题目均视为已审核（保持可见）；此后新增题
    默认 reviewed_at=NULL（审核中），审核通过才可见。

    PRAGMA user_version 作迁移标记（>=1 已迁移，跳过）；回填仅当库内
    无任何已审核题时执行（P0 修复：避免每次启动批量审批现存待审核队列，
    也让已运行过门禁的库保留队列原状）。
    """
    with engine.begin() as conn:
        version = conn.execute(text("PRAGMA user_version")).scalar()
        if version >= 1:
            return
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info('questions')")).fetchall()]
        if "reviewed_at" not in cols:
            conn.execute(text("ALTER TABLE questions ADD COLUMN reviewed_at DATETIME"))
        has_approved = conn.execute(text(
            "SELECT 1 FROM questions WHERE reviewed_at IS NOT NULL LIMIT 1"
        )).fetchone()
        if has_approved is None:
            conn.execute(text(
                "UPDATE questions SET reviewed_at = created_at "
                "WHERE reviewed_at IS NULL AND created_at IS NOT NULL"
            ))
        conn.execute(text("PRAGMA user_version = 1"))


def _migrate_source_provenance(engine: Engine) -> None:
    """幂等迁移：sources 表加来源溯源列（license/author/repo_url）。

    历史行保持 NULL，不猜测许可；后续新导入的 GitHub 源写入元数据。
    """
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info('sources')")).fetchall()]
        for column in ("license", "author", "repo_url"):
            if column not in cols:
                conn.execute(text(f"ALTER TABLE sources ADD COLUMN {column} VARCHAR"))


def _migrate_question_feedback_open_unique(engine: Engine) -> None:
    """幂等迁移：同一用户对同一题目最多一条 open 反馈（partial unique index）。"""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_open "
            "ON question_feedback (question_id, user_id) WHERE status = 'open'"
        ))


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


def _today_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


BAD_SCORE_THRESHOLD = 70  # 上次判分低于此阈值 = 表现不好，重新进入今日选题池（薄弱复习）


def _question_focus_info(
    session: DBSession, qids: list[int], focus: str | None, focus_lang: str | None
) -> dict[int, tuple[int, int, bool, bool]]:
    """题 → (lang 命中, role 命中, 存在语言无关分类标签, 命中通用分类标签)。

    由标签分类的 lang/roles 推导；无标签题不在返回中（调用方按相关兜底）。
    """
    from .models import QuestionTag, Tag, TagCategory

    if not qids:
        return {}
    rows = session.execute(
        select(QuestionTag.question_id, TagCategory.lang, TagCategory.roles)
        .join(Tag, QuestionTag.tag_id == Tag.id)
        .join(TagCategory, Tag.category_id == TagCategory.id)
        .where(QuestionTag.question_id.in_(qids))
    ).all()
    info: dict[int, list] = {}
    for qid, lang, roles in rows:
        cat_roles = tuple(roles or ())
        entry = info.setdefault(qid, [0, 0, 0, 0])
        if focus_lang and lang == focus_lang:
            entry[0] = 1
        if focus and focus in cat_roles:
            entry[1] = 1
        if not cat_roles:
            entry[3] = 1  # 通用分类标签
        elif lang is None:
            entry[2] = 1  # 语言无关分类标签（如 数据库/分布式/OS）
    return {qid: tuple(v) for qid, v in info.items()}


def _is_focus_relevant(
    info: tuple[int, int, bool, bool] | None, focus: str | None, focus_lang: str | None
) -> bool:
    """岗位×语言相关性：backend 严格语言原则（其他语言分类不相关）；通用/无标签题算相关。"""
    if focus is None:
        return True
    if info is None:  # 无标签题：无法判定，兜底算相关（新题可进今日）
        return True
    lang_hit, role_hit, has_neutral_cat, is_common = info
    if is_common:
        return True
    if focus == "backend":
        if focus_lang:
            if lang_hit:
                return True  # 我的语言分类
            if has_neutral_cat and role_hit:
                return True  # 语言无关的后端分类（数据库/分布式/OS/基础设施）
            return False  # 其他语言分类（Go/Python/C++ 等）严格排除
        return bool(role_hit)
    return bool(role_hit)


def _weighted_sample(
    relevant_ids: list[int], other_ids: list[int], n: int, p_rel: float = 0.75
) -> list[int]:
    """3:1 权重随机抽取（不放回）：3/4 概率从相关池抽，1/4 从非相关池抽；池空互转。"""
    rel = list(relevant_ids)
    oth = list(other_ids)
    random.shuffle(rel)
    random.shuffle(oth)
    picked: list[int] = []
    while len(picked) < n and (rel or oth):
        if rel and (not oth or random.random() < p_rel):
            picked.append(rel.pop())
        elif oth:
            picked.append(oth.pop())
        elif rel:
            picked.append(rel.pop())
        else:
            break
    return picked


@_wrap_storage
def pick_questions(
    session: DBSession,
    user_id: int,
    limit: int,
    qtype: QuestionType | None = None,
) -> list[Question]:
    """今日选题：70% 没写过池（严格岗位语言相关）+ 30% 上次表现不好池（3:1 岗位加权随机）。

    池 A（70% 权重）：用户从未完成（无我的 finished 会话）的题，**严格按岗位×语言筛选**，
      只取相关题（我的语言分类 ∪ 语言无关后端分类 ∪ 通用 ∪ 无标签兜底），组内随机
    池 B（30% 权重）：用户上次判分 < BAD_SCORE_THRESHOLD 的题（薄弱复习），
      岗位相关题与非相关题按 3:1 权重随机
    每池不足由另一池互补兜底（优先相关）；排除今天已选过的题；qtype 按题型配额过滤（D8）。
    """
    from .models import Judgment, User

    picked_today_qids = select(UserPick.question_id).where(
        UserPick.user_id == user_id,
        UserPick.picked_at >= _today_start(),
    )
    base = select(Question.id).where(
        Question.id.not_in(picked_today_qids),
        Question.reviewed_at.is_not(None),  # 审核中题目不进今日选题池
    )
    if qtype is not None:
        base = base.where(Question.type == qtype)
    done_qids = select(Session.question_id).where(
        Session.user_id == user_id,
        Session.status == SessionStatus.finished,
    )
    # 池 A：没写过（无 finished 会话）
    a_ids = list(session.scalars(base.where(Question.id.not_in(done_qids))))
    # 池 B：每题取最新 finished 判分，低于阈值进池
    rows = session.execute(
        select(Session.question_id, Judgment.total_score)
        .join(Judgment, Judgment.session_id == Session.id)
        .where(
            Session.user_id == user_id,
            Session.status == SessionStatus.finished,
        )
        .order_by(Session.id.desc())
    ).all()
    seen: set[int] = set()
    bad_qids: set[int] = set()
    for qid, score in rows:
        if qid in seen:
            continue
        seen.add(qid)
        if score is not None and score < BAD_SCORE_THRESHOLD:
            bad_qids.add(qid)
    b_ids = list(session.scalars(base.where(Question.id.in_(bad_qids))))

    user = session.get(User, user_id)
    focus = user.focus if user is not None else None
    focus_lang = user.focus_lang if user is not None else None
    info = _question_focus_info(session, a_ids + b_ids, focus, focus_lang)
    relevant = lambda qid: _is_focus_relevant(info.get(qid), focus, focus_lang)

    # 池 A：严格岗位相关（不足由池 B 3:1 权重互补）
    n_bad = round(limit * 0.3)
    n_never = limit - n_bad
    a_rel = [qid for qid in a_ids if relevant(qid)]
    picked: list[int] = list(random.sample(a_rel, min(n_never, len(a_rel))))
    # 池 B：3:1 岗位加权（排除已选）
    b_pool = [qid for qid in b_ids if qid not in picked]
    b_rel = [qid for qid in b_pool if relevant(qid)]
    b_oth = [qid for qid in b_pool if not relevant(qid)]
    picked.extend(_weighted_sample(b_rel, b_oth, min(n_bad, len(b_pool))))
    # 不足互补：全部剩余候选按 3:1 相关加权补齐
    if len(picked) < limit:
        rest = [qid for qid in a_ids + b_ids if qid not in picked]
        rest_rel = [qid for qid in rest if relevant(qid)]
        rest_oth = [qid for qid in rest if not relevant(qid)]
        picked.extend(_weighted_sample(rest_rel, rest_oth, limit - len(picked)))
    random.shuffle(picked)  # 双池混合后乱序展示

    questions = [session.get(Question, qid) for qid in picked]
    now = datetime.now()
    rows = [
        {"user_id": user_id, "question_id": q.id, "picked_at": now}
        for q in questions
        if q is not None
    ]
    if rows:
        # on_conflict_do_nothing：并发双开标签页同题重复插入被唯一约束吸收（结果一致，不 500）
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

        session.execute(_sqlite_insert(UserPick).values(rows).on_conflict_do_nothing())
        commit(session)
    return [q for q in questions if q is not None]


@_wrap_storage
def set_question_tags(session: DBSession, question_id: int, names: list[str]) -> None:
    """覆写题目标签：删旧关联 + 按词表名写新关联（未命中词表的静默丢弃）。"""
    from .models import QuestionTag, Tag

    session.execute(delete(QuestionTag).where(QuestionTag.question_id == question_id))
    if not names:
        return
    tag_ids = [t.id for t in session.scalars(select(Tag).where(Tag.name.in_(names))).all()]
    if tag_ids:
        session.add_all(
            QuestionTag(question_id=question_id, tag_id=tid) for tid in tag_ids
        )


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
