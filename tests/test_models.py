from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import (
    commit,
    count_questions_created_since,
    get_session,
    init_db,
    latest_task_log,
    list_today_questions,
    pick_questions,
)
from app.errors import DuplicateSource, StorageError
from app.models import (
    Attempt,
    Judgment,
    Question,
    QuestionStatus,
    QuestionType,
    Session,
    SessionKind,
    SessionStatus,
    Source,
    SourceType,
    TaskLog,
)

VALID_SCORES = {"accuracy": 85, "completeness": 70, "clarity": 90, "depth": 60}


_source_seq = 0


def add_source(session, source_hash=None, **kw):
    global _source_seq
    _source_seq += 1
    if source_hash is None:
        source_hash = f"h{_source_seq}"
    source = Source(type=kw.pop("type", SourceType.manual), source_hash=source_hash, **kw)
    session.add(source)
    commit(session)
    session.refresh(source)
    return source


def add_question(session, type=QuestionType.knowledge, stem="问题", **kw):
    source = kw.pop("source", None) or add_source(session)
    question = Question(source_id=source.id, type=type, stem=stem, **kw)
    session.add(question)
    commit(session)
    session.refresh(question)
    return question


def add_session(session, **kw):
    question = kw.pop("question", None) or add_question(session)
    s = Session(question_id=question.id, kind=SessionKind.chain, **kw)
    session.add(s)
    commit(session)
    session.refresh(s)
    return s


def add_task_log(session, task_name="daily", **kw):
    log = TaskLog(task_name=task_name, **kw)
    session.add(log)
    commit(session)
    session.refresh(log)
    return log


# --- happy ---


def test_crud_roundtrip(db):
    source = add_source(db, title="面经A")
    assert source.id is not None

    loaded = db.get(Source, source.id)
    assert loaded.title == "面经A"
    assert loaded.type == SourceType.manual

    loaded.title = "面经B"
    commit(db)
    assert db.get(Source, source.id).title == "面经B"

    db.delete(loaded)
    commit(db)
    assert db.get(Source, source.id) is None


def test_cascade_delete_session(db):
    s = add_session(db)
    a1 = Attempt(session_id=s.id, round_no=0, answer_text="回答1")
    a2 = Attempt(session_id=s.id, round_no=1, is_followup=True, answer_text="回答2")
    j = Judgment(session_id=s.id, scores=VALID_SCORES, total_score=78)
    db.add_all([a1, a2, j])
    commit(db)

    db.delete(s)
    commit(db)

    assert db.get(Attempt, a1.id) is None
    assert db.get(Attempt, a2.id) is None
    assert db.get(Judgment, j.id) is None
    assert db.get(Session, s.id) is None


def test_json_fields_roundtrip(db):
    q = add_question(
        db,
        tags=["agent", "RAG"],
        good_criteria=["完整", "准确"],
        bad_criteria=["答非所问"],
        difficulty=4,
    )
    loaded_q = db.get(Question, q.id)
    assert loaded_q.tags == ["agent", "RAG"]
    assert loaded_q.good_criteria == ["完整", "准确"]
    assert loaded_q.bad_criteria == ["答非所问"]
    assert loaded_q.difficulty == 4

    s = add_session(db, question=loaded_q)
    j = Judgment(session_id=s.id, scores=VALID_SCORES, weak_tags=["深度不足"])
    db.add(j)
    commit(db)
    loaded_j = db.get(Judgment, j.id)
    assert loaded_j.scores == VALID_SCORES
    assert loaded_j.weak_tags == ["深度不足"]


def test_enum_values_roundtrip(db):
    source = add_source(db, type=SourceType.github, source_hash="h2")
    q = add_question(db, source=source, type=QuestionType.design)
    s = add_session(db, question=q, status=SessionStatus.finished, ended_at=datetime.now())

    assert db.get(Source, source.id).type == SourceType.github
    assert db.get(Question, q.id).type == QuestionType.design
    assert db.get(Session, s.id).status == SessionStatus.finished


# --- edge ---


def test_empty_table_helpers(db):
    assert list_today_questions(db) == []
    assert pick_questions(db, 5) == []
    assert latest_task_log(db, "daily") is None
    assert count_questions_created_since(db, datetime.now() - timedelta(days=1)) == 0


def test_migrate_backfills_today_selected_at(db):
    """status='today' 但 selected_at NULL 的题被补齐（今日/历史不同步修复）。"""
    from app.db import _migrate_backfill_today_selected_at

    q = add_question(db, stem="today 无 selected_at 的题")
    q.status = QuestionStatus.today
    q.selected_at = None
    commit(db)

    engine = db.get_bind()
    _migrate_backfill_today_selected_at(engine)
    _migrate_backfill_today_selected_at(engine)  # 幂等

    rows = db.exec(text("SELECT selected_at FROM questions WHERE id = :i"), params={"i": q.id}).all()
    assert rows[0][0] is not None  # 已补齐
    rows2 = db.exec(text("SELECT selected_at IS NULL FROM questions WHERE status = 'today'")).all()
    assert all(r[0] == 0 for r in rows2)  # 无遗漏


def test_migrate_cleans_empty_sessions(db):
    """启动迁移：无回答无判分的空壳会话被清理；有内容的保留；幂等。"""
    from app.db import _migrate_cleanup_empty_sessions

    q = add_question(db)
    empty = Session(question_id=q.id, kind=SessionKind.chain, status=SessionStatus.active)
    db.add(empty)
    commit(db)
    db.refresh(empty)
    with_content = Session(question_id=q.id, kind=SessionKind.chain, status=SessionStatus.finished)
    db.add(with_content)
    commit(db)
    db.refresh(with_content)
    db.add(Attempt(session_id=with_content.id, round_no=0, answer_text="答过一轮"))
    commit(db)

    engine = db.get_bind()
    _migrate_cleanup_empty_sessions(engine)
    _migrate_cleanup_empty_sessions(engine)  # 幂等

    remaining = db.exec(text("SELECT id FROM sessions")).all()
    assert [r[0] for r in remaining] == [with_content.id]


def test_bulk_insert(db):
    source = add_source(db)
    for i in range(10):
        db.add(Question(source_id=source.id, type=QuestionType.knowledge, stem=f"q{i}"))
    commit(db)
    assert count_questions_created_since(db, datetime.now() - timedelta(days=1)) == 10


def test_long_text_roundtrip(db):
    long_text = "长" * 1_000_000
    source = add_source(db, raw_text=long_text)
    assert db.get(Source, source.id).raw_text == long_text


def test_multiple_active_sessions_coexist(db):
    s1 = add_session(db)
    s2 = add_session(db)
    assert db.get(Session, s1.id).status == SessionStatus.active
    assert db.get(Session, s2.id).status == SessionStatus.active


def test_pick_questions_marks_today_and_limits(db):
    for i in range(3):
        add_question(db, stem=f"q{i}")
    picked = pick_questions(db, 2)
    assert [q.stem for q in picked] == ["q0", "q1"]
    assert [q.stem for q in list_today_questions(db)] == ["q0", "q1"]
    assert all(q.selected_at is not None for q in picked)
    remaining = db.exec(
        text("SELECT stem FROM questions WHERE status = 'pending'")
    ).all()
    assert [r[0] for r in remaining] == ["q2"]


def test_pick_questions_by_type(db):
    add_question(db, type=QuestionType.knowledge, stem="知识题")
    add_question(db, type=QuestionType.design, stem="设计题")
    picked = pick_questions(db, 5, qtype=QuestionType.design)
    assert [q.stem for q in picked] == ["设计题"]


def test_latest_task_log_returns_newest(db):
    add_task_log(db, status="success", fetched_count=1)
    add_task_log(db, status="failed", error="boom")
    log = latest_task_log(db, "daily")
    assert log.status == "failed"
    assert log.error == "boom"
    assert latest_task_log(db, "other") is None


def test_count_questions_created_since(db):
    add_question(db)
    add_question(db)
    now = datetime.now()
    assert count_questions_created_since(db, now - timedelta(days=1)) == 2
    assert count_questions_created_since(db, now + timedelta(days=1)) == 0


# --- fail ---


def test_duplicate_source_hash_raises_duplicate_source(db):
    add_source(db, source_hash="dup-1")
    dup = Source(type=SourceType.nowcoder, source_hash="dup-1")
    db.add(dup)
    with pytest.raises(DuplicateSource) as exc:
        commit(db)
    assert exc.value.source_hash == "dup-1"


def test_invalid_enum_value_raises_storage_error(db):
    """DB 级 CHECK 约束：非法枚举值入库即抛 StorageError。"""
    source = add_source(db)
    db.add(Question(source_id=source.id, type="nonsense", stem="x"))
    with pytest.raises(StorageError):
        commit(db)


def test_invalid_enum_via_raw_sql_rejected_by_db(db):
    """绕过 ORM 的裸 SQL 也会被 DB 级 CHECK 拒绝（约束真实存在）。"""
    source = add_source(db)
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO questions (source_id, type, stem, difficulty, status, created_at) "
                "VALUES (:sid, 'nonsense', 'x', 1, 'pending', :now)"
            ),
            {"sid": source.id, "now": datetime.now().isoformat()},
        )


def test_get_session_before_init_raises(monkeypatch):
    import app.db as db_module

    monkeypatch.setattr(db_module, "_factory", None)
    with pytest.raises(StorageError, match="not initialized"):
        with get_session():
            pass


def test_foreign_key_enforced(db):
    """PRAGMA foreign_keys=ON：伪造 session_id 的孤儿行提交即抛 StorageError。"""
    orphan = Attempt(session_id=99999, round_no=0, answer_text="孤儿")
    db.add(orphan)
    with pytest.raises(StorageError):
        commit(db)


def test_readonly_db_file_raises_storage_error(tmp_path):
    db_file = tmp_path / "ro.sqlite"
    db_file.write_bytes(b"")
    db_file.chmod(0o444)
    try:
        with pytest.raises(StorageError):
            init_db(f"sqlite:///{db_file}")
    finally:
        db_file.chmod(0o644)
