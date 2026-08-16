from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text
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
    QuestionTag,
    QuestionType,
    Session,
    SessionKind,
    SessionStatus,
    Source,
    SourceType,
    Tag,
    TaskLog,
    User,
    UserPick,
)

VALID_SCORES = {"accuracy": 85, "completeness": 70, "clarity": 90, "depth": 60}


_source_seq = 0
_user_seq = 0


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
    question = Question(
        source_id=source.id,
        type=type,
        stem=stem,
        reviewed_at=kw.pop("reviewed_at", datetime.now()),  # 测试默认已审核（可见）；审核门禁用例传 None
        **kw,
    )
    session.add(question)
    commit(session)
    session.refresh(question)
    return question


def add_user(session, username=None):
    global _user_seq
    if username is None:
        _user_seq += 1
        username = f"u{_user_seq}"
    user = User(email=f"{username}@test.com", username=username, password_hash="x")
    session.add(user)
    commit(session)
    session.refresh(user)
    return user


def add_session(session, **kw):
    question = kw.pop("question", None) or add_question(session)
    user = kw.pop("user", None) or add_user(session)
    s = Session(question_id=question.id, user_id=user.id, kind=SessionKind.chain, **kw)
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

    # DB 真实级联验证（select 走真实查询，不受 identity map 缓存影响）
    assert db.scalars(select(Attempt).where(Attempt.id.in_([a1.id, a2.id]))).all() == []
    assert db.scalars(select(Judgment).where(Judgment.id == j.id)).all() == []
    assert db.scalars(select(Session).where(Session.id == s.id)).all() == []


def test_json_fields_roundtrip(db):
    q = add_question(
        db,
        good_criteria=["完整", "准确"],
        bad_criteria=["答非所问"],
        difficulty=4,
    )
    loaded_q = db.get(Question, q.id)
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


def test_question_tags_roundtrip(db):
    """题目-标签多对多：写入关联后查询命中；重复插同关联由复合主键拦截。"""
    q = add_question(db)
    tag_a = db.scalars(select(Tag).where(Tag.name == "Java")).first()  # 种子词表
    assert tag_a is not None
    db.add(QuestionTag(question_id=q.id, tag_id=tag_a.id))
    commit(db)
    rows = db.scalars(
        select(QuestionTag).where(QuestionTag.question_id == q.id)
    ).all()
    assert len(rows) == 1
    with pytest.raises(StorageError):  # commit 统一包装 IntegrityError → StorageError
        db.add(QuestionTag(question_id=q.id, tag_id=tag_a.id))
        commit(db)


def test_enum_values_roundtrip(db):
    source = add_source(db, type=SourceType.github, source_hash="h2")
    q = add_question(db, source=source, type=QuestionType.design)
    s = add_session(db, question=q, status=SessionStatus.finished, ended_at=datetime.now())

    assert db.get(Source, source.id).type == SourceType.github
    assert db.get(Question, q.id).type == QuestionType.design
    assert db.get(Session, s.id).status == SessionStatus.finished

# --- edge ---


def test_empty_table_helpers(db):
    user = add_user(db)
    assert list_today_questions(db, user.id) == []
    assert pick_questions(db, user.id, 5) == []
    assert latest_task_log(db, "daily") is None
    assert count_questions_created_since(db, datetime.now() - timedelta(days=1)) == 0


def test_bulk_insert(db):
    source = add_source(db)
    for i in range(10):
        db.add(Question(source_id=source.id, type=QuestionType.knowledge, stem=f"q{i}"))
    commit(db)
    assert count_questions_created_since(db, datetime.now() - timedelta(days=1)) == 10


def test_long_text_roundtrip(db):
    long_text = "长" * 1_000_000
    source = add_source(db, cleaned_text=long_text)
    assert db.get(Source, source.id).cleaned_text == long_text


def test_multiple_active_sessions_coexist(db):
    s1 = add_session(db)
    s2 = add_session(db)
    assert db.get(Session, s1.id).status == SessionStatus.active
    assert db.get(Session, s2.id).status == SessionStatus.active


def test_backfill_reviewed_migration(db):
    """一次性回填迁移（P0 修复）：无已审核证据时存量回填一次并置标记；
    有证据/已置标记时跳过——待审核队列不被自动审批。"""
    from app.db import _migrate_backfill_reviewed, engine

    def reset_version():
        with engine().begin() as conn:
            conn.execute(text("PRAGMA user_version = 0"))

    # 场景 1：无任何已审核题 → 存量全部回填一次，并置标记
    add_question(db, stem="存量0", reviewed_at=None)
    add_question(db, stem="存量1", reviewed_at=None)
    reset_version()
    _migrate_backfill_reviewed(engine())
    db.expire_all()
    for i in range(2):
        q = db.scalars(select(Question).where(Question.stem == f"存量{i}")).one()
        assert q.reviewed_at is not None

    # 场景 2：已存在已审核题（门禁已运行过）→ 待审核队列原样保留
    reset_version()
    q_new = add_question(db, stem="新题", reviewed_at=None)
    _migrate_backfill_reviewed(engine())
    db.expire_all()
    assert db.get(Question, q_new.id).reviewed_at is None

    # 场景 3：标记已置（user_version>=1）→ 完全跳过
    q_new2 = add_question(db, stem="新题2", reviewed_at=None)
    _migrate_backfill_reviewed(engine())
    db.expire_all()
    assert db.get(Question, q_new2.id).reviewed_at is None


def test_pick_questions_per_user_pool(db):
    """每用户池：A 选走题不影响 B；已完成且高分的题不再被选；当天不重复选。"""
    user_a = add_user(db, username="pick_a")
    user_b = add_user(db, username="pick_b")
    for i in range(3):
        add_question(db, stem=f"q{i}")

    # limit=2 → 7:3 随机（3 题全在没写过池）：任意 2 题
    picked_a = pick_questions(db, user_a.id, 2)
    a_stems = {q.stem for q in picked_a}
    assert len(a_stems) == 2 and a_stems <= {"q0", "q1", "q2"}
    assert {q.stem for q in list_today_questions(db, user_a.id)} == a_stems

    # B 今天从全库可选题（A 的选择不影响 B）
    picked_b = pick_questions(db, user_b.id, 2)
    assert len(picked_b) == 2

    # A 今天再选：排除今天已选的，只剩第 3 题
    picked_a2 = pick_questions(db, user_a.id, 2)
    assert [q.stem for q in picked_a2] == list({"q0", "q1", "q2"} - a_stems)

    # 完成 picked_a 中一题（finished 会话，无判分）→ 明天 A 不再被选它
    done_stem = sorted(a_stems)[0]
    done_q = db.scalars(select(Question).where(Question.stem == done_stem)).first()
    add_session(db, user=user_a, question=done_q, status=SessionStatus.finished)
    commit(db)
    # 模拟新的一天（把 A 今天 picks 改为昨天）
    for p in db.scalars(select(UserPick).where(UserPick.user_id == user_a.id)).all():
        p.picked_at = datetime.now() - timedelta(days=1)
    commit(db)
    picked_a3 = pick_questions(db, user_a.id, 10)
    assert done_stem not in [q.stem for q in picked_a3]
    assert ({"q0", "q1", "q2"} - {done_stem}) <= {q.stem for q in picked_a3}


def test_pick_questions_bad_score_reenters_pool(db):
    """表现不好（上次判分 < 阈值）的题重新进入今日候选；高分完成的题不进入。"""
    from app.db import BAD_SCORE_THRESHOLD

    user = add_user(db, username="pick_bad")
    q_never = add_question(db, stem="没写过题")
    q_bad = add_question(db, stem="上次低分题")
    q_good = add_question(db, stem="上次高分题")

    def finish(q, score):
        s = add_session(db, user=user, question=q, status=SessionStatus.finished)
        db.add(Judgment(session_id=s.id, scores={"status": "ok"}, total_score=score))
        commit(db)

    finish(q_bad, BAD_SCORE_THRESHOLD - 5)
    finish(q_good, BAD_SCORE_THRESHOLD + 20)

    # limit=2 → 7:3 各取 1：必选「没写过」+「上次低分」；高分完成题不得出现
    picked = pick_questions(db, user.id, 2)
    stems = [q.stem for q in picked]
    assert "没写过题" in stems and "上次低分题" in stems
    assert "上次高分题" not in stems


def test_pick_never_pool_focus_strict(db):
    """池 A（没写过）严格岗位×语言：backend+java 用户不选其他语言分类（Go）的题；
    池 B 有相关题可补时也不引入非相关题（不足兜底才允许出现）。"""
    from app.models import User

    user = User(email="focus_user@test.com", username="focus_user", password_hash="x", focus="backend", focus_lang="java")
    db.add(user)
    commit(db)
    db.refresh(user)

    q_java = add_question(db, stem="Java 分类题")
    q_java_bad = add_question(db, stem="Java 低分题")
    add_question(db, stem="无标签题")
    add_question(db, stem="Go 分类题")

    from app.db import set_question_tags

    set_question_tags(db, q_java.id, ["JVM"])
    set_question_tags(db, q_java_bad.id, ["JVM"])
    go_q = db.scalars(select(Question).where(Question.stem == "Go 分类题")).first()
    set_question_tags(db, go_q.id, ["Goroutine"])
    commit(db)
    # q_java_bad 判分 40 → 进池 B（相关低分）
    s = add_session(db, user=user, question=q_java_bad, status=SessionStatus.finished)
    db.add(Judgment(session_id=s.id, scores={"status": "ok"}, total_score=40))
    commit(db)

    # limit=2：池 A 抽 1（相关）+ 池 B 抽 1（相关低分）→ Go 题不出现
    picked = pick_questions(db, user.id, 2)
    stems = [q.stem for q in picked]
    assert len(stems) == 2
    assert "Go 分类题" not in stems
    assert "Java 分类题" in stems or "无标签题" in stems
    assert "Java 低分题" in stems  # 池 B 低分题进入


def test_pick_questions_by_type(db):
    user = add_user(db)
    add_question(db, type=QuestionType.knowledge, stem="知识题")
    add_question(db, type=QuestionType.design, stem="设计题")
    picked = pick_questions(db, user.id, 5, qtype=QuestionType.design)
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
                "INSERT INTO questions (source_id, type, stem, difficulty, created_at) "
                "VALUES (:sid, 'nonsense', 'x', 1, :now)"
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
