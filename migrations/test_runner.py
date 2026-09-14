"""迁移 runner 的行为测试。

布局依据 ADR-0005：领域内测试跟领域走，跨领域测试在 `tests/`。
迁移不是领域、也不是基础设施 —— 它自己一层，所以测试住在它旁边。

用 `tmp_dir`（根 conftest 提供）而不是 `tmp_path`：本环境下 pytest 自带的
临时目录插件必然失败，原因见 `tests/conftest.py`。

**第一个测试是关于回滚的**，因为 ADR-0011 的实现第一版就在这里错了：
它用 `conn.executescript()`，而后者执行前会隐式提交挂起的事务，于是
`BEGIN` 被顶掉、"失败即整文件回滚"根本不生效。当时 ADR-0011 已经把这条
写成"唯一跑一次才知道的点"—— 它确实需要跑一次才知道。
按编码原则 4：修 bug 补一条复现测试，这条就是。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from migrations._runner import migrate

SQL_DIR = Path(__file__).resolve().parent


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _recorded(db: Path) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute("SELECT filename FROM schema_migrations")]
    finally:
        conn.close()


@pytest.fixture
def fake_migrations(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把"迁移目录"换成一个空目录，好放我们自己造的迁移文件。

    `_migration_files()` 读的是模块常量 `MIGRATIONS_DIR`，所以 monkeypatch 它
    即可 —— 不必把真迁移复制来复制去。
    """
    d = tmp_dir / "migrations"
    d.mkdir()
    monkeypatch.setattr("migrations._runner.MIGRATIONS_DIR", d)
    return d


def test_initial_creates_all_tables(tmp_dir: Path) -> None:
    """`0001_initial.sql` 跑完应当建出设计里的全部表，并留下记账。"""
    db = tmp_dir / "t.db"
    assert migrate(db) == 1

    names = _tables(db)
    expected = {
        "schema_migrations", "users", "invite_codes", "user_tokens", "quota_ledger",
        "domains", "roles", "role_points", "knowledge_points", "criteria",
        "knowledge_point_edges", "questions", "question_points", "question_flags",
        "candidate_profiles", "interviews", "report_items", "sessions", "attempts",
        "evaluations", "question_feedback", "task_logs", "jobs", "question_point_stats",
    }
    assert expected <= names, f"缺少：{expected - names}"
    assert _recorded(db) == ["0001_initial.sql"]


def test_second_run_is_idempotent(tmp_dir: Path) -> None:
    """跑第二遍不做事、不报错 —— 否则 pipeline 的第一步就不可重跑。"""
    db = tmp_dir / "t.db"
    migrate(db)
    assert migrate(db) == 0
    assert "users" in _tables(db)


def test_failed_migration_leaves_no_trace(fake_migrations: Path, tmp_dir: Path) -> None:
    """**回归测试**：文件里第 N 条语句失败 → 前面已执行的语句全部回滚、且不记账。

    这条如果失败，说明事务边界又不在 runner 手里了（例如有人改回
    `executescript`）——而症状是"留下半个 schema 且不记账"，一个静默的坏库。

    用的坏 SQL 是**语法错了的外键子句**。第一版用的是
    `CREATE TABLE oops (this_is_not_valid_sql);` —— 那条**在 SQLite 里完全合法**
    （一个无类型列），所以测试当时是靠另一个错误"通过"的。教训与 §一.4 同源：
    断言要先证明它会失败。
    """
    (fake_migrations / "0001_first.sql").write_text(
        "CREATE TABLE good (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (fake_migrations / "0002_bad.sql").write_text(
        "CREATE TABLE also_good (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE oops (id INTEGER PRIMARY KEY, FOREIGN KEY (id) REFERENCES nope(id)",  # 括号没闭合
        encoding="utf-8",
    )

    db = tmp_dir / "t.db"
    with pytest.raises(Exception) as e:
        migrate(db)
    assert "0002_bad.sql" in str(e.value)

    names = _tables(db)
    assert "good" in names, "0001 应当已经正常生效"
    assert "also_good" not in names, "0002 的第一条语句必须被回滚掉"
    assert "oops" not in names
    assert _recorded(db) == ["0001_first.sql"], "失败的文件不许记账 —— 下次要能重跑"


def test_modified_migration_is_refused(fake_migrations: Path, tmp_dir: Path) -> None:
    """跑过的迁移被改了 → 拒绝继续，而不是重跑。

    "跑过的文件被改"不会有任何报错，但库的当前状态与文件已经对不上 ——
    典型的静默漂移（ADR-0011）。
    """
    f = fake_migrations / "0001_x.sql"
    f.write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8")
    db = tmp_dir / "t.db"
    migrate(db)

    f.write_text(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);\nCREATE TABLE b (id INTEGER);",
        encoding="utf-8",
    )
    with pytest.raises(Exception) as e:
        migrate(db)
    assert "被改了" in str(e.value)
