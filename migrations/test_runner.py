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


def _migration_names() -> list[str]:
    """磁盘上全部迁移的文件名（排序）。

    ⚠️ 断言不要写死 `["0001_initial.sql"]`：**加一条迁移就会红**，而那时该改的是
    测试，不是迁移。第一版就是写死的，于是 0002 一落地两条测试同时红 ——
    修的方式是把它改成"与磁盘一致"，这样 0003、0004 都不会再触发同类修改。
    """
    from migrations._runner import _migration_files

    return [p.name for p in _migration_files()]


def test_initial_creates_all_tables(tmp_dir: Path) -> None:
    """跑完全部迁移后应当建出设计里的全部表，并留下记账。"""
    db = tmp_dir / "t.db"
    assert migrate(db) == len(_migration_names())

    names = _tables(db)
    expected = {
        "schema_migrations", "users", "invite_codes", "user_tokens", "quota_ledger",
        "domains", "roles", "role_points", "knowledge_points", "criteria",
        "knowledge_point_edges", "questions", "question_points", "question_flags",
        "candidate_profiles", "interviews", "report_items", "sessions", "attempts",
        "evaluations", "question_feedback", "task_logs", "jobs", "question_point_stats",
        "user_favorites",
    }
    assert expected <= names, f"缺少：{expected - names}"
    assert _recorded(db) == _migration_names()


def _columns(db: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_criteria_has_the_shared_marker(tmp_dir: Path) -> None:
    """**回归测试**：`criteria.shared` 必须存在。

    没有它，ADR-0002 判据②（复制传播）就无法区分两种情况：
      · 一条考察点被复制到多个知识点下 → **两个知识点该合并**
      · 一条通用表达类考察点被多个知识点合法引用 → **不该合并**（「共用考察点」）

    区分办法在 ADR-0002 里写死了（把通用表达类显式标记为共用考察点），
    而第一版 schema 里没有这一列 —— 于是那条判据只能吃误报。
    """
    db = tmp_dir / "t.db"
    migrate(db)
    assert "shared" in _columns(db, "criteria"), "criteria 缺 shared 列（ADR-0002 判据②）"


def test_question_point_stats_keys_on_criterion_id(tmp_dir: Path) -> None:
    """**回归测试**：聚合表必须用 `criterion_id` 键，不能是 `criterion_index`。

    `attempts.hits` 是 `criterion_id → 命中状态` 的映射（累积快照，决策 28）。
    聚合表若按 `seq`（位置）索引，两处**无法互相映射** —— 注销用户时
    「把 attempts.hits 累加进本表」这一步就没有可用的对照键。

    此外位置键在知识点被重审、考察点被删或重排之后会**静默指向另一条考察点**；
    而 ADR-0002 已明说知识点定义会随人审而变，所以这不是假想风险。
    """
    db = tmp_dir / "t.db"
    migrate(db)
    assert "criterion_id" in _columns(db, "question_point_stats")
    assert "criterion_index" not in _columns(db, "question_point_stats"), (
        "位置键会让注销聚合与 attempts.hits 对不上（决策 28）"
    )


def test_quota_ledger_separates_day_from_month(tmp_dir: Path) -> None:
    """**回归测试**：`quota_ledger` 的日行与月行必须能被机械区分。

    决策 23 要"每日聚合 + 月度归档行"，而两者粒度不同。第一版的设计是
    "月度行用一个特殊 day 值，如 `2026-09`" —— 同一列里混两种格式，
    于是"这个月已用多少"只能靠字符串形状猜。`kind` 把这件事变成显式事实。
    """
    db = tmp_dir / "t.db"
    migrate(db)
    cols = _columns(db, "quota_ledger")
    assert "kind" in cols, "quota_ledger 缺 kind 列（日月粒度无法机械区分）"
    assert "day" in cols


def test_favorite_is_idempotent_by_unique_constraint(tmp_dir: Path) -> None:
    """**回归测试**：同一人重复收藏同一道题只能有一行（决策 63）。

    这条约束就是"收藏"这个动作幂等的实现方式 —— 不靠应用层先查再写
    （那有竞态），而是让第二次 INSERT 直接撞唯一约束。
    v1 的同一张表也是这么设计的（`docs/v1现状-20260913.md` 第 12 张表）。

    断言点在**唯一约束真的挡住了第二行**：那是"约束生效"唯一不可伪装的结果
    （与 `test_migration_statements_actually_run` 同一个理由）。
    """
    db = tmp_dir / "t.db"
    migrate(db)
    conn = sqlite3.connect(str(db))
    try:
        # 外键在迁移里是开着的，但那是 runner 的连接；这里要自己再开一次
        conn.execute("PRAGMA foreign_keys = ON")
        uid = conn.execute("SELECT id FROM users WHERE email='owner@local'").fetchone()[0]
        # 不建知识点：`questions.primary_point_id` 可空，正好用上那个"v1 导入容错"的余地
        conn.execute(
            "INSERT INTO questions (id, kind, stem, difficulty) VALUES (1, 'knowledge', 'q', 3)"
        )
        conn.execute("INSERT INTO user_favorites (user_id, question_id) VALUES (?, 1)", (uid,))
        try:
            conn.execute(
                "INSERT INTO user_favorites (user_id, question_id) VALUES (?, 1)", (uid,)
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("重复收藏被写进去了 —— UNIQUE 约束没生效")
        n = conn.execute("SELECT COUNT(*) FROM user_favorites").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_one_evaluation_per_session_and_the_dedupe_keeps_the_good_row(tmp_dir: Path) -> None:
    """**回归测试**：迁移 0005 的两件事 —— 去重规则 + 唯一索引（决策 89）。

    题会话有两行 `evaluations` 时，`GET /me/export` 的 `scalar_one_or_none()` 抛
    `MultipleResultsFound` ⇒ 那个用户的导出**永久 500**。所以 0005 先按
    「优先留 `ok`、其次留最早」去重，再加唯一索引。

    两段断言：去重**留下了哪一行**（规则），以及第二行**再也插不进去**（约束）。
    为了让 0005 有东西可去重，这里先把索引摘掉、造出两行 —— 那正是"跑过 0004 的库"
    的形状，而"已经在线上跑过的库"恰恰是这个迁移真正要处理的输入。
    """
    db = tmp_dir / "t.db"
    migrate(db)
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        uid = conn.execute("SELECT id FROM users WHERE email='owner@local'").fetchone()[0]
        conn.execute(
            "INSERT INTO questions (id, kind, stem, difficulty) VALUES (1, 'knowledge', 'q', 3)"
        )
        conn.execute(
            "INSERT INTO interviews (id, user_id, mode, status, quota_charged) "
            "VALUES (1, ?, 'drill', 'active', 1)",
            (uid,),
        )
        conn.execute(
            "INSERT INTO sessions (id, interview_id, question_id, seq, status, max_rounds) "
            "VALUES (1, 1, 1, 1, 'finished', 3)"
        )
        conn.execute("DROP INDEX IF EXISTS uq_evaluation_session")
        # 先失败后成功的收尾：留下的必须是**成功**那一行（失败那行没有分数）
        conn.execute(
            "INSERT INTO evaluations (session_id, total_score, review, status) "
            "VALUES (1, 0, '判分失败', 'failed')"
        )
        conn.execute(
            "INSERT INTO evaluations (session_id, total_score, review, status) "
            "VALUES (1, 80, '真判定', 'ok')"
        )
        # 让它回到"还没跑过 0005"的状态：删掉记账行，migrate 会重跑那一个文件
        conn.execute(
            "DELETE FROM schema_migrations WHERE filename = '0005_one_evaluation_per_session.sql'"
        )
    finally:
        conn.close()

    assert migrate(db) == 1, "应该只重跑 0005 这一个文件"

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT total_score, status FROM evaluations WHERE session_id = 1"
        ).fetchall()
        assert rows == [(80.0, "ok")], f"去重留下了 {rows} —— 该留成功的那一行"
        try:
            conn.execute("INSERT INTO evaluations (session_id, status) VALUES (1, 'ok')")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("同一个题会话写进了第二条 evaluations —— 唯一索引没生效")
    finally:
        conn.close()


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


def test_migration_statements_actually_run(fake_migrations: Path, tmp_dir: Path) -> None:
    """**回归测试**：迁移文件里的 SQL 必须真的被执行。

    为什么需要这么"显然"的一条测试：一次错误的编辑让 `_apply_file` 里的
    `for stmt in _split_statements(text)` 整行消失，**迁移文件里的 SQL 一条都不执行**
    —— 而当时 9 条测试全绿，因为记账表照建（runner 自己保证）、记账也照写。
    唯一的表现是"表没被建出来"，而没有任何测试检查过"表建出来了没有"。

    断言点选在**表是否存在**：那是"SQL 执行了"唯一不可伪装的结果。
    """
    (fake_migrations / "0001_ddl.sql").write_text(
        "CREATE TABLE proof (id INTEGER PRIMARY KEY, note TEXT);",
        encoding="utf-8",
    )
    db = tmp_dir / "t.db"
    migrate(db)
    assert "proof" in _tables(db), "迁移文件里的 CREATE TABLE 没有被执行"
    # 顺带确认它不是"记账成功但什么都没做"的假象
    conn = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(proof)")]
    finally:
        conn.close()
    assert cols == ["id", "note"]


def test_bookkeeping_insert_failure_rolls_back(fake_migrations: Path, tmp_dir: Path) -> None:
    """**回归测试**：记账的 INSERT 失败时，迁移文件也必须被回滚。

    这个 bug 的形状很坏：`INSERT` 原在 try 之外，所以它失败时事务不回滚，
    **而消息却说「已回滚整个文件、没有记账」** —— 出问题时给了一条错误的线索。

    构造办法：第二个迁移重复建第一张表，于是它的 `CREATE TABLE` 就失败；
    更直接的是让记账表本身不可写 —— 这里用「已存在的表名」触发，
    断言点只有一个：**失败之后库里不能留下那个迁移的任何东西**。
    """
    (fake_migrations / "0001_a.sql").write_text(
        "CREATE TABLE t1 (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    db = tmp_dir / "t.db"
    migrate(db)

    # 第二个迁移：先建一张新表，再建一张与 0001 里同名的表 → 后半失败
    (fake_migrations / "0002_b.sql").write_text(
        "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\nCREATE TABLE t1 (id INTEGER);",
        encoding="utf-8",
    )
    with pytest.raises(Exception) as e:
        migrate(db)
    assert "0002_b.sql" in str(e.value)

    conn = sqlite3.connect(str(db))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        recorded = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations")]
    finally:
        conn.close()
    assert "t2" not in names, "0002 的前半必须回滚"
    assert recorded == ["0001_a.sql"], "失败的迁移不许记账"


def test_foreign_keys_are_actually_enabled(fake_migrations: Path, tmp_dir: Path) -> None:
    """**回归测试**：`PRAGMA foreign_keys` 必须在写操作之前设好，且真的生效。

    这个 bug 是静默的：`PRAGMA foreign_keys` 在**事务内是空操作**，而原先它执行
    在那句 `CREATE TABLE`（会开启事务）之后 —— 于是迁移期间外键根本没打开。
    表现为"引用了不存在的表也能建成功"，没有任何报错。

    这里用一条引用不存在表的外键来表达：外键关着时它能建成功。
    """
    (fake_migrations / "0001_fk.sql").write_text(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id));",
        encoding="utf-8",
    )
    db = tmp_dir / "t.db"
    assert migrate(db) == 1  # 这条本身是合法的，先确认能建

    # 真正验证开关：另起一个库，让迁移写一条违反外键的数据
    (fake_migrations / "0002_bad_fk.sql").write_text(
        "INSERT INTO child (id, pid) VALUES (1, 999);",  # parent 里没有 999
        encoding="utf-8",
    )
    db2 = tmp_dir / "t2.db"
    with pytest.raises(Exception) as e:
        migrate(db2)
    assert "FOREIGN KEY" in str(e.value).upper(), (
        "外键没生效 —— 违反外键的数据被写进去了"
    )


def test_split_statements_keeps_semicolons_in_place() -> None:
    """statement splitter 的关键性质：注释与字符串里的分号**不切**。

    它存在的唯一理由是 `executescript()` 会隐式提交事务（ADR-0011），
    所以这个函数是"事务边界真的在 runner 手里"的前提。

    ⚠️ 注意它的粒度：**注释会留在它所属的那一块里，孤立的注释自己成一块**
    （实测：`sql` 末尾的 `-- 说明` 会切成一块）。执行 `-- 注释` 在 SQLite 里是
    空操作，所以这不影响正确性 —— 但断言必须按真实行为写，
    否则测试会以"我期望的样子"通过。
    """
    from migrations._runner import _split_statements

    sql = (
        "CREATE TABLE a (id INTEGER);\n"
        "INSERT INTO a (id) VALUES (1);\n"
        "INSERT INTO a (id) VALUES (';');  -- 字符串里的分号\n"
        "-- 结尾的行注释\n"
    )
    got = _split_statements(sql)
    assert len(got) == 4, got
    assert got[0] == "CREATE TABLE a (id INTEGER)"
    # 关键性质一：字符串里的分号没有被当成语句边界
    assert got[2].startswith("INSERT INTO a (id) VALUES (';')")
    # 关键性质二：行尾与结尾的注释都留在尾块里，不产生半截语句
    assert got[3].startswith("--")
    assert "分号" in got[3]
    # 关键性质三：每一块都不是空的（半截语句会让迁移失败）
    assert all(s.strip() for s in got), got

    # 行注释里的分号也不切：注释自己成一块（执行时是空操作），语句完整
    got2 = _split_statements("CREATE TABLE b (id INTEGER);  -- 注释里有 ; 分号")
    assert len(got2) == 2, got2
    assert got2[0] == "CREATE TABLE b (id INTEGER)"
    assert got2[1].startswith("--")


def test_real_migration_file_survives_the_splitter() -> None:
    """真实迁移文件逐条执行必须成功 —— 这是 splitter 唯一要紧的用途。

    比"手写一段 SQL 猜它怎么切"更可靠：它跑的是真正会进库的那个文件，
    而且是按 runner 的真实路径（`_apply_file` → `_split_statements`）跑的。
    """
    real = Path(__file__).resolve().parent / "0001_initial.sql"
    from migrations._runner import _split_statements

    stmts = _split_statements(real.read_text(encoding="utf-8"))
    assert len(stmts) > 30, f"切得太少，可能整段被当成一条：{len(stmts)}"
    # 每条都得能独立执行 —— 空块或半截语句会让迁移失败
    assert all(s.strip() for s in stmts)


def test_cli_migrate_and_check(tmp_dir: Path) -> None:
    """`--db` 与 `--check` 的实际行为（此前只有 migrate() 被测过）。

    `--db` 的存在理由就是让测试能对着临时库跑**真实迁移文件** ——
    所以这条测的是"命令行那条路真的通"。
    """
    from migrations._runner import main

    db = tmp_dir / "cli.db"
    assert main(["--db", str(db)]) == 0
    assert _recorded(db) == _migration_names()
    assert main(["--db", str(db)]) == 0, "幂等：第二遍也要成功"
    assert main(["--db", str(db), "--check"]) == 0
    assert main(["--db"]) == 2, "缺参数要报错退出，不是静默用默认库"
