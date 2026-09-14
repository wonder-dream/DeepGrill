"""映射与迁移 schema 的一致性测试。

**为什么必须有**：`app/db/models.py` 是手写的（`0001_initial.sql` 也是手写的），
两者之间没有任何东西保证同步。而"列名写错"这类错的默认表现是**查询时才炸**，
甚至在 `select` 里不炸、取属性时才炸（那时已经离出错点很远）。

所以这里把迁移文件解析成「表 → 列」，和 `metadata` 对一遍：
**任何一边多了/少了，都是红测试**。它同时守住"加表忘了映射"这条。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import create_db_engine, create_session_factory
from app.db.models import Interview, User, metadata
from migrations._runner import migrate

SQL_FILE = Path(__file__).resolve().parents[2] / "migrations" / "0001_initial.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

#: 出现在表级约束里、但不是列名的词。踩过一次：`RE.findall(r"\w+", "PRIMARY KEY (...)")`
#: 会把 "PRIMARY" 本身当成一个列名，于是每个复合主键表都报"迁移多一列 primary"。
SQL_KEYWORDS = {"primary", "key", "unique", "foreign", "check", "constraint"}


def _migration_files() -> list[Path]:
    """全部迁移文件（按文件名排序）。

    ⚠️ **不能只看 `0001`**：第一条迁移之后的 `ALTER TABLE ... ADD COLUMN` 也是
    schema 的一部分（`0002` 加了 `question_feedback.status`）。第一版只读 `0001`，
    于是加了 0002 之后这条对账测试会报"models 多一列 status" —— 那时该改的是
    测试的读取范围，不是把列删掉。
    """
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if not p.name.startswith("_"))


def _sql_schema() -> dict[str, set[str]]:
    """从**全部迁移文件**解析出 表名 → 列名集合。

    解析规则与 `migrations/_runner.py` 的 splitter 同一前提：迁移文件是受控的
    （列定义形如 `name TYPE`，复合主键写在 `PRIMARY KEY (...)` 里，加列形如
    `ALTER TABLE t ADD COLUMN name TYPE`）。
    """
    schema: dict[str, set[str]] = {}
    for path in _migration_files():
        body = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\);", body, re.S
        ):
            name, inner = m.group(1), m.group(2)
            cols: set[str] = set()
            for line in inner.split("\n"):
                line = line.strip()
                if not line or line.startswith("--"):
                    continue
                cm = re.match(r'^[\["]?(\w+)[\]"]?\s+[A-Z]', line)
                # 表级约束行（`PRIMARY KEY (a, b)`）的第一词是关键字，不是列名 ——
                # 踩过一次：它让每个复合主键表都报"迁移多一列 primary"。
                if cm and cm.group(1).lower() not in SQL_KEYWORDS:
                    cols.add(cm.group(1).lower())
            for pk in re.findall(r"PRIMARY KEY\s*\(([^)]+)\)", inner):
                cols |= {
                    c.lower() for c in re.findall(r"\w+", pk) if c.lower() not in SQL_KEYWORDS
                }
            schema.setdefault(name, set()).update(cols)

        # 后继迁移里的加列（0002 起）
        for m in re.finditer(
            r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+[\[\"]?(\w+)[\]\"]?", body, re.I
        ):
            schema.setdefault(m.group(1), set()).add(m.group(2).lower())
    return schema


def test_every_table_in_the_migration_is_mapped() -> None:
    """迁移建的表必须都有映射 —— 否则"加表忘了映射"会拖到运行时才炸。

    例外：`schema_migrations` **刻意不映射**。它由迁移 runner 自己用
    `CREATE TABLE IF NOT EXISTS` 保证存在（ADR-0011），属于 runner 的私有
    记账表，不是应用数据 —— 业务代码碰它说明有人在手搓迁移状态。
    """
    mapped = set(metadata.tables)
    in_sql = set(_sql_schema()) - {"schema_migrations"}
    assert in_sql <= mapped, f"迁移里有、models.py 没映射：{sorted(in_sql - mapped)}"


def test_no_mapped_table_is_missing_from_the_migration() -> None:
    """反过来也要成立：映射了库里没有的表，查询时才报 no such table。"""
    mapped = set(metadata.tables)
    in_sql = set(_sql_schema())
    assert mapped <= in_sql, f"models.py 有、迁移里没有：{sorted(mapped - in_sql)}"


def test_columns_match_the_migration_exactly() -> None:
    """逐表比列集合。这是本文件存在的**主要理由**。"""
    schema = _sql_schema()
    mismatches: list[str] = []
    for name, table in metadata.tables.items():
        sql_cols = schema[name]
        py_cols = {c.name for c in table.columns}
        if sql_cols != py_cols:
            only_sql = sorted(sql_cols - py_cols)
            only_py = sorted(py_cols - sql_cols)
            mismatches.append(f"{name}: 迁移多 {only_sql} / models 多 {only_py}")
    assert not mismatches, "列不一致：\n  " + "\n  ".join(mismatches)


def test_orm_defaults_match_the_ddl_defaults() -> None:
    """**回归测试**：DDL 里的 DEFAULT 必须也写在映射上。

    漏了它的症状不是"少个默认值"，而是**两条写入路径行为不一致**：用 SQL 写入
    时库会填默认值（`started_at`），用 ORM 写入时 ORM 显式送 NULL → 撞 NOT NULL。
    实测撞过 `interviews.started_at`。
    """
    body = SQL_FILE.read_text(encoding="utf-8")
    ddl_defaults: dict[tuple[str, str], str] = {}
    for m in re.finditer(
        r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\);", body, re.S
    ):
        table, inner = m.group(1), m.group(2)
        for line in inner.split("\n"):
            cm = re.match(r'\s*[\["]?(\w+)[\]"]?\s+[A-Z]+\s*(.*)$', line)
            if not cm or cm.group(1).lower() in SQL_KEYWORDS:
                continue
            col, rest = cm.group(1), cm.group(2)
            dm = re.search(r"DEFAULT\s+(?:\((.*?)\)|'([^']*)')", rest)
            if dm:
                ddl_defaults[(table, col)] = (dm.group(1) or dm.group(2)).strip()

    missing: list[str] = []
    for (table_name, col), _value in ddl_defaults.items():
        if table_name == "schema_migrations":
            continue
        table = metadata.tables[table_name]
        if col not in table.columns:
            continue
        if table.columns[col].server_default is None:
            missing.append(f"{table_name}.{col}")
    assert not missing, f"迁移里有 DEFAULT、映射没写 server_default：{missing}"


@pytest.fixture
def db_session(tmp_dir: Path):
    db = tmp_dir / "models.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        yield s


def test_orm_writes_and_reads_a_row(db_session) -> None:
    """映射不只是"能建"：真的能写一行、读回来。"""
    user = User(email="a@b.c", username="a", password_hash="scrypt$x$y", role="user")
    db_session.add(user)
    db_session.commit()

    got = db_session.one("users", user.id)
    assert got is not None
    assert got.email == "a@b.c"  # type: ignore[attr-defined]


def test_json_columns_round_trip_chinese_without_escaping(db_session) -> None:
    """**JSON 列的中文语义**（`docs/v1行为规格.md` §8.7，硬性继承）。

    存成 `\\uXXXX` 之后，对 JSON 列做 SQL 文本匹配会**静默失效** —— v1 真发生过
    （`tags LIKE '%"RAG"%'` 对中文永远匹配不上）。所以断言两件事：
    ① 取回来还是原来的对象；② **库里存的是明文**（能直接 LIKE 到）。
    """
    user = User(email="a@b.c", username="a", password_hash="h", role="user")
    db_session.add(user)
    db_session.flush()

    plan = {"题目": ["volatile", "内存屏障"]}
    interview = Interview(user_id=user.id, mode="interview", plan=plan, quota_charged=6)
    db_session.add(interview)
    db_session.commit()

    # ① 走映射读回来是 dict（不是字符串）
    got = db_session.one("interviews", interview.id)
    assert got is not None
    assert got.plan == plan  # type: ignore[attr-defined]

    # ② 库里存的是明文 —— 用 LIKE 证明"SQL 文本匹配"这条路真的通
    raw = db_session.execute(
        text("SELECT plan FROM interviews WHERE id = :i"), {"i": interview.id}
    ).scalar_one()
    assert "内存屏障" in raw
    assert "\\u" not in raw
    hit = db_session.execute(
        text("SELECT COUNT(*) FROM interviews WHERE plan LIKE :pat"),
        {"pat": "%内存屏障%"},
    ).scalar_one()
    assert hit == 1
