import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
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
def list_today_questions(session: DBSession, user_id: int) -> list[Question]:
    """今日题目（每用户池单轨）：该用户 picked_at 属今天的题。

    昨日及更早的 picks 自然不在今日列表（按日期过滤）；未完成的题明天自动回到候选。
    """
    stmt = (
        select(Question)
        .join(UserPick, UserPick.question_id == Question.id)
        .where(UserPick.user_id == user_id, UserPick.picked_at >= _today_start())
        .order_by(Question.created_at)
    )
    return list(session.scalars(stmt))


@_wrap_storage
def list_questions_by_date(
    session: DBSession, user_id: int, date_str: str
) -> list[Question]:
    """按选题日期（picked_at 落于当日零点~次日零点）查该用户选的题。

    供今日页日历单选回看某一天的题目（含未完成的题，历史记录不因回收消失）。
    """
    day = datetime.strptime(date_str, "%Y-%m-%d")
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    stmt = (
        select(Question)
        .join(UserPick, UserPick.question_id == Question.id)
        .where(UserPick.user_id == user_id, UserPick.picked_at >= start, UserPick.picked_at < end)
        .order_by(Question.created_at)
    )
    return list(session.scalars(stmt))


def _today_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _weak_tag_weights(
    session: DBSession, user_id: int, top: int = 5
) -> dict[str, int]:
    """聚合该用户全部判分的薄弱点（weak_tags）→ 出现次数 topN。"""
    from collections import Counter

    from .models import Judgment

    rows = session.scalars(
        select(Judgment)
        .join(Session, Judgment.session_id == Session.id)
        .where(Session.user_id == user_id)
    ).all()
    counter: Counter = Counter()
    for j in rows:
        for t in (j.weak_tags or []):
            counter[t] += 1
    return dict(counter.most_common(top))


def _rank_recommend(
    session: DBSession, candidates: list[Question], user_id: int
) -> list[Question]:
    """个性化推荐 = 岗位/语言权重 + 薄弱标签加权 + 多样性摊开 + 按序兜底：

    1. 题岗位分（focus × 分类 roles/lang，见 _role_score）
    2. 叠加薄弱标签权重（出现次数 top5）
    3. 每薄弱标签取命中它的第一题（摊开不同薄弱点），再按总分序补齐
    4. 非本岗/非本语言题不阻断：得分 0 的候选按 created_at 兜底排在最后
    """
    from .models import QuestionTag, Tag, TagCategory, User

    weights = _weak_tag_weights(session, user_id)
    user = session.get(User, user_id)
    focus = user.focus if user is not None else None
    focus_lang = user.focus_lang if user is not None else None

    rows = session.execute(
        select(QuestionTag.question_id, Tag.name, TagCategory.lang, TagCategory.roles)
        .join(Tag, QuestionTag.tag_id == Tag.id)
        .join(TagCategory, Tag.category_id == TagCategory.id)
        .where(QuestionTag.question_id.in_([q.id for q in candidates]))
    ).all()
    # 题 → (lang 命中, role 命中, 标签名列表)
    q_info: dict[int, tuple[int, int, list[str]]] = {}
    for qid, name, lang, roles in rows:
        cat_roles = tuple(roles or ())
        lang_hit = 1 if (focus_lang and lang == focus_lang) else 0
        role_hit = 1 if (focus and focus in cat_roles) else 0
        entry = q_info.setdefault(qid, [0, 0, []])
        entry[0] = max(entry[0], lang_hit)
        entry[1] = max(entry[1], role_hit)
        entry[2].append(name)

    def _score(q: Question) -> int:
        lang_hit, role_hit, _names = q_info.get(q.id, (0, 0, []))
        if focus == "backend":
            if focus_lang and lang_hit:
                return 4 + (2 if role_hit else 0)  # 我的语言 + 后端域最高
            if not focus_lang and role_hit:
                return 4  # 未指定语言：所有后端语言题均等
            if role_hit:
                return 2  # 其他语言的后端题
            return 0
        if focus and role_hit:
            return 3  # 非 backend 岗位：岗位域题
        return 0

    def _weak(q: Question) -> int:
        if not weights:
            return 0
        return max((weights.get(t, 0) for t in q_info.get(q.id, (0, 0, []))[2]), default=0)

    def _names(q: Question) -> list[str]:
        return q_info.get(q.id, (0, 0, []))[2]

    scored = sorted(
        candidates,
        key=lambda q: (-_score(q), -_weak(q), q.created_at),
    )
    picked: list[Question] = []
    taken: set[int] = set()
    for tag in weights:  # 薄弱标签多样性：每标签取 1 题（按上述总分序）
        for q in scored:
            if q.id not in taken and tag in _names(q):
                picked.append(q)
                taken.add(q.id)
                break
    for q in scored:
        if q.id not in taken:
            picked.append(q)
            taken.add(q.id)
    return picked


BAD_SCORE_THRESHOLD = 60  # 上次判分低于此阈值 = 表现不好，重新进入今日选题池（薄弱复习）


@_wrap_storage
def pick_questions(
    session: DBSession,
    user_id: int,
    limit: int,
    qtype: QuestionType | None = None,
) -> list[Question]:
    """今日选题：从「没写过」与「上次表现不好」双池按 7:3 权重随机选取。

    池 A（70% 权重）：用户从未完成（无我的 finished 会话）的题，组内随机
    池 B（30% 权重）：用户上次判分 < BAD_SCORE_THRESHOLD 的题（薄弱复习），组内随机
    每池不足时由另一池互补兜底；排除今天已选过的题；qtype 按题型配额过滤（D8）。
    """
    import random

    from .models import Judgment

    picked_today_qids = select(UserPick.question_id).where(
        UserPick.user_id == user_id,
        UserPick.picked_at >= _today_start(),
    )
    base = select(Question.id).where(Question.id.not_in(picked_today_qids))
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
    # 7:3 权重分配（每池内随机），不足互补
    n_bad = round(limit * 0.3)
    n_never = limit - n_bad
    picked: list[int] = []
    picked.extend(random.sample(a_ids, min(n_never, len(a_ids))))
    picked.extend(random.sample(b_ids, min(n_bad, len(b_ids))))
    if len(picked) < limit:
        rest = [qid for qid in a_ids + b_ids if qid not in picked]
        picked.extend(random.sample(rest, min(limit - len(picked), len(rest))))
    random.shuffle(picked)  # 双池混合后乱序展示
    questions = [session.get(Question, qid) for qid in picked]
    now = datetime.now()
    for q in questions:
        if q is not None:
            session.add(UserPick(user_id=user_id, question_id=q.id, picked_at=now))
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


@_wrap_storage
def count_questions_created_since(session: DBSession, since: datetime) -> int:
    """供 D16 每日新入库不重复问题上限计数。"""
    stmt = select(func.count()).select_from(Question).where(Question.created_at >= since)
    return session.scalars(stmt).one()
