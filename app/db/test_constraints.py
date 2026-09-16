"""枚举列的 CHECK 约束（决策 57 的落地）。

原先这些列**只靠业务层**：SQLite 会静默接受 `kind='K'`、`difficulty=9`、`status='Voice'`
这类错值 —— 而"静默接受"正是本项目最怕的那类失败（它不会报错，只会让下游读到一个
谁也不认识的取值）。决策 57 当时把它记成**有意识的欠债**，理由是"改值要写迁移"；
现在表是空的，还这笔债零成本。

断言点选在**数据库拒绝**（`IntegrityError`），而不是"函数返回了 False" ——
约束的意义就在于它不依赖任何一层业务代码记得检查。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Attempt,
    Domain,
    Evaluation,
    Interview,
    Job,
    KnowledgePoint,
    Question,
    User,
)
from app.db.models import (
    Session_ as InterviewSession,
)
from migrations._runner import migrate


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "checks.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        yield s


def test_users_role_is_checked(session: Session) -> None:
    session.add(User(email="a@b.c", username="a", password_hash="h", role="superuser"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_questions_kind_is_checked(session: Session) -> None:
    """题型只有 knowledge / design（决策 20）。"""
    session.add(Question(kind="project", stem="题", difficulty=3, origin="seed"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_questions_difficulty_range_is_checked(session: Session) -> None:
    """难度 1-5 —— v1 就有"无 DB 级范围约束"这条延期未修的技术债，这里还掉。"""
    session.add(Question(kind="knowledge", stem="题", difficulty=9, origin="seed"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_questions_visibility_is_checked(session: Session) -> None:
    session.add(Question(kind="knowledge", stem="题", difficulty=3, origin="seed",
                         visibility="PUBLIC"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_questions_origin_is_checked(session: Session) -> None:
    session.add(Question(kind="knowledge", stem="题", difficulty=3, origin="crawled"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_knowledge_point_status_is_checked(session: Session) -> None:
    """`status` 决定它算不算人审过的骨架（决策 26）。"""
    session.add(Domain(name="D"))
    session.flush()
    session.add(KnowledgePoint(domain_id=1, name="p", status="approved"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_attempts_input_mode_is_checked(session: Session, tmp_dir: Path) -> None:
    """决策 57 的原话点名的就是这个：`input_mode='Voice'` 不该被静默接受。"""
    _seed_session(session)
    session.add(Attempt(session_id=1, round_no=1, is_followup=0, input_mode="Voice",
                        answer_text="答"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_attempts_is_followup_is_boolean(session: Session) -> None:
    _seed_session(session)
    session.add(Attempt(session_id=1, round_no=1, is_followup=7, input_mode="text",
                        answer_text="答"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_interviews_mode_has_no_browse(session: Session) -> None:
    """`browse` 不是合法 mode —— 题库练习不建面试行（与 `docs/v2数据模型.md` 对齐）。"""
    _seed_user(session)
    session.add(Interview(user_id=1, mode="browse", status="active", quota_charged=0))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_evaluations_status_is_checked(session: Session) -> None:
    _seed_session(session)
    session.add(Evaluation(session_id=1, status="maybe"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_one_evaluation_per_session(session: Session) -> None:
    """一个题会话只有一条最终评分（迁移 0005 的唯一索引，决策 89）。

    收尾本来就允许被重复调用（页面层会：旧标签页重放一轮之后再收尾一次），
    而第二行会让 `GET /me/export` 的 `scalar_one_or_none()` 抛
    `MultipleResultsFound` —— 那个用户的导出**永久 500**（实测复现）。
    所以这条约束是"收尾幂等"的实现方式：不靠应用层先查再写（那有竞态）。
    """
    _seed_session(session)
    session.add(Evaluation(session_id=10, status="ok"))
    session.flush()
    session.add(Evaluation(session_id=10, status="ok"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_jobs_status_is_checked(session: Session) -> None:
    session.add(Job(kind="x", payload={}, status="queued", max_attempts=3))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_jobs_max_attempts_must_be_positive(session: Session) -> None:
    """`max_attempts=0` 会让任务一认领就"超限" —— 那是个必然卡死的配置。"""
    session.add(Job(kind="x", payload={}, status="pending", max_attempts=0))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_sessions_status_and_max_rounds(session: Session) -> None:
    _seed_user(session)
    session.add(Interview(user_id=1, mode="drill", status="active", quota_charged=1))
    session.flush()
    session.add(Question(kind="knowledge", stem="题", difficulty=3, origin="seed"))
    session.flush()
    session.add(InterviewSession(interview_id=1, question_id=1, seq=1, status="paused",
                                 max_rounds=3))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_valid_values_still_work(session: Session) -> None:
    """**误报守卫**：约束不该拦住合法数据 —— 这条与上面那些同样重要。"""
    _seed_session(session)
    session.add(Attempt(session_id=10, round_no=1, is_followup=1, input_mode="voice",
                        answer_text="答"))
    session.add(Evaluation(session_id=10, status="failed"))
    session.add(Job(kind="self_repair_stats", payload={}, status="pending", max_attempts=3))
    session.commit()
    assert session.get(Attempt, 1) is not None


# ---------------------------------------------------------------------------
# 启动期的库检查（决策 58）
# ---------------------------------------------------------------------------
def test_startup_check_is_off_by_default(tmp_dir: Path) -> None:
    """本机开发就是从占位 owner 开始的 —— 默认不拦，否则大家会绕过这个检查。"""
    from app.db.startup import placeholder_accounts

    db = tmp_dir / "default.db"
    migrate(db)
    assert placeholder_accounts(db) == ["owner@local"], "迁移确实种了一个占位账号"
    # 默认的 require_secure 是 False → 不抛
    from app.config import Settings
    from app.db.startup import assert_database_is_ready

    assert Settings(database_path=db).require_secure_db is False
    assert_database_is_ready(db, require_secure=False)


def test_startup_check_refuses_a_placeholder_password(tmp_dir: Path) -> None:
    """**决策 58**：打开开关之后，带占位口令的库**拒绝启动**。"""
    from app.config import Settings
    from app.db.startup import InsecureDatabase, assert_database_is_ready
    from app.main import create_app

    db = tmp_dir / "guarded.db"
    migrate(db)

    with pytest.raises(InsecureDatabase) as e:
        assert_database_is_ready(db, require_secure=True)
    assert "owner@local" in str(e.value)
    assert "拒绝启动" in str(e.value)

    # 真从组装根走一遍（而不只是调那个函数）
    with pytest.raises(InsecureDatabase):
        create_app(Settings(database_path=db, require_secure_db=True))


def test_startup_check_passes_after_the_password_is_replaced(tmp_dir: Path) -> None:
    """替换掉占位口令之后就能起 —— 检查不该把人永久挡在门外。"""
    from app.config import Settings
    from app.db.startup import assert_database_is_ready, placeholder_accounts
    from app.main import create_app
    from app.security import hash_password

    db = tmp_dir / "fixed.db"
    migrate(db)

    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE email = 'owner@local'",
        (hash_password("a-real-password"),),
    )
    conn.commit()
    conn.close()

    assert placeholder_accounts(db) == []
    assert_database_is_ready(db, require_secure=True)
    create_app(Settings(database_path=db, require_secure_db=True))  # 不抛


def test_startup_check_on_an_unmigrated_db_is_silent(tmp_dir: Path) -> None:
    """没迁移过的库不是本检查的事 —— 首页会告诉用户该跑哪条命令。"""
    from app.db.startup import assert_database_is_ready, placeholder_accounts

    db = tmp_dir / "empty.db"
    assert placeholder_accounts(db) == []
    assert_database_is_ready(db, require_secure=True)


# --- 半迁移防护（加了 0003 之后实测撞到的那种失败） --------------------------
def test_half_migrated_db_is_refused(tmp_dir: Path) -> None:
    """**迁移了一半** → 拒绝启动，并指向那条命令。

    为什么要有这条：加了 `explanation_cache`（0003）之后，没迁移的开发库上
    **只有题库详情页 500**（那一页读讲解缓存），其余页面正常 —— 那种"某一页莫名
    500"比"进程起不来"难查得多。而它本来可以是一句启动期的话。
    """
    import sqlite3

    from app.db.startup import SchemaOutOfDate, assert_schema_covers_code, missing_tables

    db = tmp_dir / "half.db"
    migrate(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DROP TABLE explanation_cache")

    assert missing_tables(db) == ["explanation_cache"]
    with pytest.raises(SchemaOutOfDate) as e:
        assert_schema_covers_code(db)
    assert "python -m migrations.run" in str(e.value), "诊断信息要指向那一条命令"


def test_create_app_refuses_a_half_migrated_db(tmp_dir: Path) -> None:
    import sqlite3

    from app.config import Settings
    from app.db.startup import SchemaOutOfDate
    from app.main import create_app

    db = tmp_dir / "half2.db"
    migrate(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DROP TABLE explanation_cache")

    with pytest.raises(SchemaOutOfDate):
        create_app(Settings(database_path=db))


def test_a_fresh_db_with_no_tables_is_still_allowed(tmp_dir: Path) -> None:
    """一张表都没有 = "还没初始化"，不是"半迁移"。

    （首页要能显示"先跑 python -m migrations.run"，所以这条不能拦。）
    """
    from app.db.startup import assert_schema_covers_code, missing_tables

    db = tmp_dir / "nothing.db"
    assert missing_tables(db) == []
    assert_schema_covers_code(db)  # 不抛

    db.write_bytes(b"")  # 存在但是空的库文件
    assert missing_tables(db) == []
    assert_schema_covers_code(db)


def test_half_migrated_columns_are_caught_in_strict_mode(tmp_dir: Path) -> None:
    """**回归测试**：加列的迁移没跑，也要能被启动检查挡下（线上那一档）。

    实测（我自己的这条改动）：`knowledge_points.owner_user_id` / `criteria.question_id`
    这四条迁移没跑时，服务**照常起得来** —— 然后注销与私有题集那条路报
    `no such column`。`missing_tables()` 看不见这种半迁移（表都在）。

    ⚠️ 只在**严格档**（`require_secure_db=true`，线上那个开关）拦：开发机的库常常
    故意落后于代码，把它变成"所有测试一起红"只会让人绕过检查。
    """
    import shutil

    from app.db.startup import SchemaOutOfDate, assert_schema_covers_code, missing_columns
    from migrations._runner import MIGRATIONS_DIR, _migration_files, migrate

    partial = tmp_dir / "partial"
    partial.mkdir()
    for path in _migration_files():
        if path.name[:4] <= "0006":   # 只放到 0006：0007 / 0008 的加列还没跑
            shutil.copy(path, partial / path.name)

    db = tmp_dir / "old.db"
    real = MIGRATIONS_DIR
    import migrations._runner as runner

    runner.MIGRATIONS_DIR = partial
    try:
        migrate(db)
    finally:
        runner.MIGRATIONS_DIR = real

    missing = missing_columns(db)
    assert "knowledge_points.owner_user_id" in missing, f"没查出缺列：{missing}"
    assert "criteria.question_id" in missing
    assert_schema_covers_code(db)  # 非严格档放行（开发机不拦）
    with pytest.raises(SchemaOutOfDate) as e:
        assert_schema_covers_code(db, strict=True)
    assert "python -m migrations.run" in str(e.value), "诊断信息要指向那一条命令"


def test_missing_tables_uses_the_code_mapping(tmp_dir: Path) -> None:
    """判据是**代码要用的表**（`models.metadata`），不是"库里有几张表"。

    这样它不必去读 `schema_migrations`（那要 import `migrations/`，与 ADR-0010 的
    目录纪律相反）—— 而且它问的正是真正要紧的那件事：代码能不能跑起来。
    """
    from app.db.models import metadata
    from app.db.startup import missing_tables

    db = tmp_dir / "full.db"
    migrate(db)
    assert missing_tables(db) == []
    assert "explanation_cache" in metadata.tables


# --- 夹具辅助 ---------------------------------------------------------------
#: 迁移会种一个占位 owner（id=1），所以测试数据从 2 起 —— 不占用它，
#: 也就不用去改 0001 里那条 INSERT。
ME = 2


def _seed_user(session: Session) -> None:
    session.add(User(id=ME, email="u@local", username="u", password_hash="h", role="user"))
    session.flush()


def _seed_session(session: Session) -> None:
    _seed_user(session)
    session.add(Interview(id=10, user_id=ME, mode="drill", status="active", quota_charged=1))
    session.flush()
    session.add(Question(id=10, kind="knowledge", stem="题", difficulty=3, origin="seed"))
    session.flush()
    session.add(InterviewSession(id=10, interview_id=10, question_id=10, seq=1,
                                 status="active", max_rounds=3))
    session.flush()
