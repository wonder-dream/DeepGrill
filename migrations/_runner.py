"""迁移执行器（ADR-0011）。

**纯 SQL，没有分支。** 本模块只做三件事：读已记账的、跑没跑过的、记账。

为什么不是 Alembic、为什么迁移里不许写 Python：见
`docs/adr/0011-migrations-are-plain-sql.md` —— v1 那次删库之所以能发生，
是因为迁移是 Python、它有 `if`。这里没有地方可以写 `if`。

事务语义：**每个文件一个事务**（`BEGIN` … `COMMIT`）。
失败 → `ROLLBACK` 整个文件 + **不记账** → 退出码非 0。
DDL 在 SQLite 里是事务性的，所以"跑到一半失败"不会留下半个 schema。
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent


class MigrationError(RuntimeError):
    """迁移失败。**必须让调用方看到非零退出码** —— 不许静默继续。"""


def _migration_files() -> list[Path]:
    """按文件名排序的全部迁移。

    `_` 前缀的文件（`_runner.py` / `_common.py`）是包内部件，不是迁移 ——
    这个约定让"加一个内部辅助模块"不会被误当成一次 schema 变更。
    """
    return sorted(
        p for p in MIGRATIONS_DIR.glob("*.sql") if not p.name.startswith("_")
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 记账表的形状。迁移文件里也写了一遍（作为 schema 的自文档），
# 两处都用 IF NOT EXISTS，因此谁先谁后都成立。
_BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def _ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    """确保记账表存在。幂等，无分支。"""
    conn.execute(_BOOKKEEPING_DDL)


def applied(conn: sqlite3.Connection) -> dict[str, str]:
    """已记账的迁移 → checksum。

    首次运行时记账表还不存在（它由 `0001_initial.sql` 自己创建），
    此时"一条都没跑过"是**预期状态**，不是错误。这里显式捕获那一个错误码，
    而不是吞掉所有异常 —— 后者会让"表被改名"和"首次运行"长得一样。
    """
    try:
        rows = conn.execute("SELECT filename, checksum FROM schema_migrations").fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {}
        raise
    return {r[0]: r[1] for r in rows}


def check_drift(conn: sqlite3.Connection) -> list[str]:
    """跑过的迁移被改过 → 返回那些文件名。

    **这不是防重复执行，是防静默漂移**：文件被改了之后，库的当前状态与文件内容
    已经对不上，而这件事不会有任何报错。发现后应报错退出，**不是重跑** ——
    重跑一个已经生效的迁移可能造成二次伤害。
    """
    recorded = applied(conn)
    drifted: list[str] = []
    for path in _migration_files():
        if path.name in recorded:
            if _sha256(path.read_text(encoding="utf-8")) != recorded[path.name]:
                drifted.append(path.name)
    return drifted


def _split_statements(sql: str) -> list[str]:
    """把迁移文件切成一条条语句。

    **为什么不用 `conn.executescript()`：它会先隐式提交挂起的事务**
    （Python 3.12+ 依然如此，实测于 3.13.13）。于是 `BEGIN` 被顶掉、脚本在自动
    提交模式下跑完，而 ADR-0011 承诺的"失败即整文件回滚"**根本不生效** ——
    SQL 跑到一半报错会留下半个 schema 且不记账。

    这条路是实测撞出来的：第一版用了 `executescript`，迁移成功但 `COMMIT`
    报 "no transaction is active"。当时 ADR-0011 已把这条列为
    "唯一跑一次才知道的点"，它确实需要跑一次才知道。

    切分规则很小，因为**迁移文件是受控的**：
      · 支持 `--` 行注释与 `/* */` 块注释（注释里的 `;` 不切）
      · 支持 `'…'` 与 `"…"` 字符串（字符串里的 `;` 不切）
      · 不需要处理触发器 `BEGIN…END` —— 本项目没有触发器，
        真需要时再扩展，而不是先写一个猜的解析器（§一.2）
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_line_comment = in_block_comment = False
    quote: str | None = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 1
                in_block_comment = False
        elif quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _apply_file(conn: sqlite3.Connection, path: Path) -> None:
    """在**一个显式事务**里执行整个文件。

    任何一条语句失败 → 整个文件回滚。事务边界由本模块持有，
    因此迁移文件里不许出现 `BEGIN` / `COMMIT`（ADR-0011）。
    """
    text = path.read_text(encoding="utf-8")
    conn.execute("BEGIN")
    try:
        # 外键必须在**执行迁移语句的那一刻**真的开着。
        #
        # ⚠️ `PRAGMA foreign_keys` 在事务内是空操作 —— 所以它由 `migrate()` 在
        # BEGIN 之前设好，这里只**断言**它没有失效。断言放在这个位置是因为
        # 这是唯一要紧的时刻。
        #
        # 这一处曾经被一次错误的编辑吃掉（`for stmt in ...` 循环整行消失），
        # 结果是**迁移文件里的 SQL 一条都不执行** —— 而记账表照建（runner 自己
        # 保证）、记账也照写，所以失败的表现只有"表没被建出来"。
        # 突变测试（把循环删掉）会让 6 条测试变红，其中
        # `test_migration_statements_actually_run` 是专门为这件事写的。
        if not conn.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise RuntimeError(
                "PRAGMA foreign_keys 未生效，拒绝在失去外键保护的情况下执行迁移"
            )
        for stmt in _split_statements(text):
            conn.execute(stmt)
        # 记账必须在**同一个事务**里。它原先在这个 try 之外 —— 于是它一旦失败，
        # 事务不回滚，而抛出的消息还会说「已回滚整个文件、没有记账」。
        # **消息在撒谎**，而这是最坏的一种错：出问题时给了一条错误的线索。
        conn.execute(
            "INSERT INTO schema_migrations (filename, checksum, applied_at) "
            "VALUES (?, ?, datetime('now'))",
            (path.name, _sha256(text)),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(db_path: Path | str) -> int:
    """跑所有待执行的迁移，返回跑掉的个数。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None → autocommit。事务边界由本模块用显式 BEGIN 持有，
    # 不让 sqlite3 的隐式事务管理掺进来。
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # `PRAGMA foreign_keys` 是**连接级**设置，必须在任何写操作之前设。
        #
        # ⚠️ 这里曾经写着一条**没核实的说法**：「`CREATE TABLE IF NOT EXISTS` 会开启
        # 事务，从而让这条 PRAGMA 失效」。两次突变测试都不支持它 —— 把那行挪到建表
        # 之后，测试照样全绿。所以那个说法是错的，已删；真正要紧的断言在
        # `_apply_file` 里（执行迁移语句的那一刻），它才测得到"外键真的开着"。
        conn.execute("PRAGMA foreign_keys = ON")
        if not conn.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise MigrationError("PRAGMA foreign_keys 没能生效 —— 迁移会失去外键保护")

        # 记账表由 runner 自己保证存在，而不是依赖"某个迁移文件会创建它"。
        #
        # 这不是风格问题，是实测撞出来的：原来靠 `0001_initial.sql` 建它，
        # 于是 runner 与迁移文件之间有一条**隐式契约** —— 任何"第一批迁移里
        # 没写这句"的情形（比如测试里的合成迁移目录）都会让记账崩掉。
        # `IF NOT EXISTS` 是幂等的，没有分支，因此不违反 ADR-0011 的"纯 SQL"。
        _ensure_bookkeeping(conn)

        drifted = check_drift(conn)
        if drifted:
            raise MigrationError(
                "这些迁移已经跑过，但文件内容被改了：\n  "
                + "\n  ".join(drifted)
                + "\n库的当前状态与文件已经对不上。请新增一个迁移来表达这次变更，"
                "\n不要修改已经跑过的文件（ADR-0011）。"
            )

        recorded = applied(conn)
        pending = [p for p in _migration_files() if p.name not in recorded]
        if not pending:
            print(f"没有待执行的迁移（已执行 {len(recorded)} 个）")
            return 0

        for path in pending:
            try:
                _apply_file(conn, path)
            except Exception as e:
                raise MigrationError(
                    f"{path.name} 执行失败，已回滚整个文件、**没有记账**：\n  {e}"
                ) from e
            print(f"  已执行 {path.name}")

        print(f"完成：执行了 {len(pending)} 个迁移")
        return len(pending)
    finally:
        conn.close()


def status(db_path: Path | str) -> None:
    """只报告状态，不改库（`--check`）。"""
    # 与 migrate 用同一套连接参数。原先这里漏了 isolation_level=None —— 读路径
    # 不写东西所以没出过事，但两处参数不一致本身就是下一次事故的种子。
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        recorded = applied(conn)
        drifted = check_drift(conn)
        for path in _migration_files():
            mark = "已执行" if path.name in recorded else "待执行"
            print(f"  [{mark}] {path.name}")
        if drifted:
            raise MigrationError("被改过的已执行迁移：" + "、".join(drifted))
        if not recorded:
            print("（库还没有记账表 —— 这个库还没跑过迁移）")
    finally:
        conn.close()


def default_db_path() -> Path:
    """默认库位置：仓库根的 `data/interview.db`。

    相对**仓库根**解析，不是相对 `cwd` —— 否则从别的工作目录跑时会静默指向
    另一个库（ADR-0010 记的同类入口）。
    """
    return MIGRATIONS_DIR.parent / "data" / "interview.db"


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m migrations.run [--check] [--db <路径>]`

    `--db` 不只是方便：测试要能对着临时库跑真实的迁移文件，
    否则"回滚真的生效吗"这类断言只能靠人手工试。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    if check_only:
        args.remove("--check")

    db_path = default_db_path()
    if "--db" in args:
        i = args.index("--db")
        if i + 1 >= len(args):
            print("--db 后面要给一个路径", file=sys.stderr)
            return 2
        db_path = Path(args[i + 1])

    try:
        if check_only:
            status(db_path)
        else:
            migrate(db_path)
    except MigrationError as e:
        print(f"迁移失败：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
