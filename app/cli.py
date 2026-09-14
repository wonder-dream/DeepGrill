"""命令行入口：`python -m app.cli <命令>`。

MVP 只有一个命令：`seed`（灌演示数据）。它存在的理由是"**最小可运行**"要能被人
当场验证 —— 没有种子数据时每个页面都是空的，"跑起来了"和"数据没进去"就分不清。

刻意不做的事：不自动跑迁移。迁移是显式的一步（AGENTS.md §3.7 / ADR-0011），
所以这里**检查**迁移跑没跑，没跑就报错并给出那条命令。
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db import create_db_engine, create_session_factory


def _ensure_migrated(session) -> None:
    """库没迁移过就明确失败 —— 不替用户跑迁移（那是另一条命令，见 ADR-0011）。"""
    try:
        session.execute(text("SELECT 1 FROM schema_migrations LIMIT 1"))
    except SQLAlchemyError as e:
        print(
            f"这个库还没跑过迁移：{e}\n"
            f"先执行： python -m migrations.run",
            file=sys.stderr,
        )
        raise SystemExit(2) from e


def cmd_seed(settings: Settings) -> int:
    from app.offline.seed import seed

    engine = create_db_engine(settings.resolved_database_path())
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)
        created = seed(session)
    print(
        "种子数据已写入："
        f"领域 {created['domains']}、知识点 {created['points']}、"
        f"考察点 {created['criteria']}、题目 {created['questions']}、"
        f"用户 {created['users']}"
    )
    return 0


def cmd_status(settings: Settings) -> int:
    """看一眼库里的规模 —— 排查"页面为什么是空的"时第一条该跑的命令。"""
    engine = create_db_engine(settings.resolved_database_path())
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)
        from app.db.models import Criterion, KnowledgePoint, Question, User

        for label, model in (
            ("题目", Question),
            ("知识点", KnowledgePoint),
            ("考察点", Criterion),
            ("用户", User),
        ):
            n = len(session.execute(select(model)).scalars().all())
            print(f"{label}: {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="DeepGrill 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="写入演示用的种子数据（幂等）")
    sub.add_parser("status", help="打印库里的规模")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    if args.command == "seed":
        return cmd_seed(settings)
    if args.command == "status":
        return cmd_status(settings)
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
