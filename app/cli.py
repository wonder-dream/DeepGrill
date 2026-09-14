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


def cmd_propose(settings: Settings, domain: str) -> int:
    """跑知识层管道的第一步：提候选知识点 → 落成提案文件（供人审）。"""
    import json

    from app.bank import repository as bank_repository
    from app.llm import LLMClient
    from app.offline import knowledge_pipeline as kp
    from app.web.admin_page import PROPOSAL_PATH

    engine = create_db_engine(settings.resolved_database_path())
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)
        # 走 bank 的仓储，不自己 select(Question)（AGENTS.md §3.5；
        # 这条规则已经抓到过三次违规，包括这个文件的第一版）
        questions = bank_repository.public_questions(session)
        print(f"待提候选的公共题：{len(questions)} 道")
        if not questions:
            print("题库里还没有公共题 —— 先跑 python -m app.cli seed", file=sys.stderr)
            return 2

    if not settings.llm_api_key:
        print("提候选要调模型 —— 先配 DEEPGRILL_LLM_API_KEY", file=sys.stderr)
        return 2

    client = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.model_interviewer,
    )
    try:
        result = kp.propose_points(questions, llm=client)
    finally:
        client.close()

    PROPOSAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSAL_PATH.write_text(
        json.dumps(result.to_json() | {"domain": domain}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"候选 {len(result.candidates)} 条 → {PROPOSAL_PATH}")
    if result.llm_failed:
        print(f"（提候选失败：{result.note}）", file=sys.stderr)
        return 1
    print("下一步：起服务后打开 /admin/review 逐条审")
    return 0


def cmd_mount(settings: Settings) -> int:
    """跑管道的挂载步骤：把题挂到**已确认的**知识点上。"""
    from app.bank import repository as bank_repository
    from app.llm import LLMClient
    from app.offline import knowledge_pipeline as kp

    engine = create_db_engine(settings.resolved_database_path())
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)
        questions = bank_repository.unmounted_public(session)
        print(f"待挂载的公共题：{len(questions)} 道")
        if not questions:
            print("没有待挂载的题")
            return 0

        if not settings.llm_api_key:
            print("挂载要调模型 —— 先配 DEEPGRILL_LLM_API_KEY", file=sys.stderr)
            return 2

        client = LLMClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.model_interviewer,
        )
        try:
            result = kp.mount_questions(session, questions=questions, llm=client)
        finally:
            client.close()
        session.commit()

    print(f"已挂载 {result.mounted} 道；待定池 {result.left_for_review} 道")
    if result.left_for_review:
        print("待定池里的题**不会**自动新建知识点（决策 46）—— 需要人审后再挂")
    return 0


def cmd_status(settings: Settings) -> int:
    """看一眼库里的规模 —— 排查"页面为什么是空的"时第一条该跑的命令。"""
    from sqlalchemy import select

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
    propose = sub.add_parser("propose", help="提候选知识点 → 提案文件（供 /admin/review 审）")
    propose.add_argument("--domain", default="未命名领域", help="这批知识点属于哪个领域")
    sub.add_parser("mount", help="把题挂到已确认的知识点上（挂不上进待定池）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    if args.command == "seed":
        return cmd_seed(settings)
    if args.command == "status":
        return cmd_status(settings)
    if args.command == "propose":
        return cmd_propose(settings, args.domain)
    if args.command == "mount":
        return cmd_mount(settings)
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
