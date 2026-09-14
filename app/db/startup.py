"""启动期的库检查（决策 58 + 半迁移防护）。

**决策 58 的原话**：`owner@local` 的 `password_hash` 仍是 `PLACEHOLDER__…` 时，
进程拒绝提供服务。理由写在台账里：

> 迁移里那条 INSERT 是明文可读的占位值，而**注释不是约束**。不检查的话，
> "上线前要替换"只是一句提醒 —— 而提醒会忘。这是唯一一个"忘了就出安全事故"的
> 待办，所以它不进待办清单，直接进启动检查。

## 为什么默认不拦（`require_secure_db` 默认 False）

本机开发**就是**从一个占位 owner 开始的。如果开发时也要求先改口令，结果只会是
大家把这个检查绕过去 —— 那比没有检查更糟（"一条满屏误报的规则比没有规则更糟"
的同一道理）。所以它由一个显式开关控制，**线上必须打开**。

## 为什么它是"启动期"而不是"某条请求里"

因为这类问题的代价不对称：漏检一次就是 owner 权限被人拿走。而启动期检查的失败
是**响亮的**（进程起不来 + 一行说清原因），修起来也只是改一个字段。

## 第二个检查：半迁移（不只是占位口令）

加了 `explanation_cache`（`0003`）之后实测撞到：没迁移的开发库上**只有题库详情页
500**（那一页会读讲解缓存），其余页面都好好的。那种"某一页莫名 500"比"进程起不来"
难查得多，而它本来是可以在启动时一句话说清的。所以这里加一条：

· **空库 / 没迁移过** → 放行（首页会显示"先跑 python -m migrations.run"）
· **迁移了一半**（有表，但缺代码要用的表）→ **拒绝启动**，并指向那条命令
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: 迁移里那个占位口令的前缀（`migrations/0001_initial.sql` 的 owner INSERT）。
PLACEHOLDER_PREFIX = "PLACEHOLDER__"


class InsecureDatabase(RuntimeError):
    """库里还有占位口令 —— **拒绝启动**。"""


def placeholder_accounts(db_path: Path) -> list[str]:
    """列出仍在使用占位口令的账号邮箱。

    库不存在 / 没迁移过时返回空列表：那是"还没初始化"，由别的地方给出提示
    （首页会显示"未初始化，先跑 python -m migrations.run"）—— 不是本检查的职责。
    """
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            rows = conn.execute(
                "SELECT email FROM users WHERE password_hash LIKE ?",
                (f"{PLACEHOLDER_PREFIX}%",),
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # users 表都还没有 —— 没迁移过，不是本检查的事
    finally:
        conn.close()
    return [r[0] for r in rows]


def assert_database_is_ready(db_path: Path, *, require_secure: bool) -> None:
    """启动前调用。两类检查：

    ① `require_secure` 为真且发现占位口令 → 抛 `InsecureDatabase`（决策 58）
    ② 库**已经迁移过一部分**、却缺代码要用的表 → 抛 `SchemaOutOfDate`
    """
    assert_schema_covers_code(db_path)
    if not require_secure:
        return
    offenders = placeholder_accounts(db_path)
    if offenders:
        raise InsecureDatabase(
            f"拒绝启动：这些账号的口令哈希还是占位值 {PLACEHOLDER_PREFIX}… —— {offenders}。\n"
            f"任何人都能用那个明文口令拿到对应权限。请先设置真实口令再启动。\n"
            f"（本检查由 DEEPGRILL_REQUIRE_SECURE_DB=true 开启；决策 58）"
        )


class SchemaOutOfDate(RuntimeError):
    """库缺了代码要用的表 —— **拒绝启动**，并指向那条命令。"""


def missing_tables(db_path: Path) -> list[str]:
    """库已经部分迁移、但缺了代码映射的表 → 返回缺的表名。

    三件事说清楚：

    **为什么这个检查是必要的**：它是"代码先上线、迁移还没跑"那个窗口。实测撞过 ——
    加了 `explanation_cache` 之后，没迁移的开发库上**只有题库详情页 500**
    （那一页会读讲解缓存），其余页面正常。那种"某一页莫名 500"比"进程起不来"
    难查得多，而诊断信息本来可以指向那条命令（ADR-0010）。

    **为什么空库不算**：库不存在 / 一张表都没有时返回空 —— 那是"还没初始化"，
    由首页显示"先跑 python -m migrations.run"（首页因此不会被一个 500 挡住，
    这是 v1 的教训之一）。本检查只管**半迁移**这个危险状态。

    **为什么不读 `schema_migrations`**：那要 import `migrations/`（另一个层的文件），
    而 ADR-0010 的目录纪律要求方向单一。这里问的是"代码要用的表在不在"，答案在
    `models.py` 的 metadata 里 —— 用自己的映射当判据，不必去问 runner。
    """
    if not db_path.is_file():
        return []
    from app.db.models import metadata

    wanted = set(metadata.tables.keys())
    conn = sqlite3.connect(str(db_path))
    try:
        present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()

    if not (present & wanted):
        return []  # 一张都没有：还没初始化，不是"半迁移"
    return sorted(wanted - present)


def assert_schema_covers_code(db_path: Path) -> None:
    missing = missing_tables(db_path)
    if missing:
        raise SchemaOutOfDate(
            f"拒绝启动：库里缺了代码要用的表 {missing} —— "
            f"这是「代码先上线、迁移没跑」的状态，会让**个别页面**莫名 500。\n"
            f"请先跑：python -m migrations.run\n"
            f"（库：{db_path}）"
        )
