"""命令行入口：`python -m app.cli <命令>`。

MVP 只有一个命令：`seed`（灌演示数据）。它存在的理由是"**最小可运行**"要能被人
当场验证 —— 没有种子数据时每个页面都是空的，"跑起来了"和"数据没进去"就分不清。

刻意不做的事：不自动跑迁移。迁移是显式的一步（AGENTS.md §3.7 / ADR-0011），
所以这里**检查**迁移跑没跑，没跑就报错并给出那条命令。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db import create_db_engine, create_session_factory

#: SQLite 绑定参数能表示的整数上限（64 位有符号）。越界的 id 会在驱动里抛
#: `OverflowError` —— 命令行要**当场**说清楚，而不是把一条 Python 栈甩给用户。
SQLITE_MAX_INT = 2**63 - 1


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


def cmd_propose(
    settings: Settings, domain: str, *, batched: bool = False, only_pending: bool = False
) -> int:
    """跑知识层管道的第一步：提候选知识点 → 落成提案文件（供人审）。

    `--batched` 是**全量装配**那条路（决策 44）：分批提候选 → 嵌入粗筛聚类 →
    逐簇让 LLM 判断归并。它需要嵌入（`DEEPGRILL_EMBEDDING_PROVIDER`），
    因为"分批必然跨批重复"，而去重只能靠向量粗筛。
    """
    import json

    from app.bank import repository as bank_repository
    from app.config import Settings as _Settings
    from app.deps import get_embeddings, get_llm
    from app.offline import knowledge_pipeline as kp
    from app.web.admin_page import PROPOSAL_PATH

    engine = create_db_engine(settings.resolved_database_path())
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)
        # 走 bank 的仓储，不自己 select(Question)（AGENTS.md §3.5；
        # 这条规则已经抓到过三次违规，包括这个文件的第一版）
        questions = bank_repository.public_questions(session)
        if only_pending:
            # 补漏用：只对还没挂上主知识点的题提候选。全量重跑一次要 76 批、几十分钟，
            # 而补漏通常只剩几十道 —— 没有这个开关就只能全量重来。
            questions = [q for q in questions if q.primary_point_id is None]
            print(f"只对待定池提候选：{len(questions)} 道")
        else:
            print(f"待提候选的公共题：{len(questions)} 道")
        if not questions:
            print("题库里还没有公共题 —— 先跑 python -m app.cli seed", file=sys.stderr)
            return 2

    if not settings.llm_api_key:
        print("提候选要调模型 —— 先配 DEEPGRILL_LLM_API_KEY", file=sys.stderr)
        return 2

    # 依赖工厂需要 Settings 对象；这里直接调它们（与 web 端同一批构造点）
    deps_settings = _Settings()
    client = get_llm(deps_settings)
    try:
        if not batched:
            result = kp.propose_points(questions, llm=client)
        else:
            embeddings = get_embeddings(deps_settings)
            with create_session_factory(engine)() as session:
                proposal = kp.propose_batched(session, questions=questions, llm=client)
                print(f"分批 {proposal.batches} 批，候选 {len(proposal.candidates)} 条")
                # 落一份**归并前**的候选：调聚类阈值 / 拆分策略时不必再花 76 次
                # LLM 调用重提（这一轮真吃过这个亏 —— 提案里只有归并后的结果）
                _candidates_path = PROPOSAL_PATH.parent / "knowledge_candidates.json"
                _candidates_path.write_text(
                    json.dumps(
                        kp.ProposalResult(candidates=proposal.candidates).to_json()
                        | {"domain": domain},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"归并前的候选另存一份 → {_candidates_path}")
                for note in proposal.notes:
                    print(f"  [注意] {note}", file=sys.stderr)
                clusters, embed_report = kp.cluster_candidates(
                    session, candidates=proposal.candidates, embeddings=embeddings
                )
                print(
                    f"嵌入：新算 {embed_report['embedded']} 条、复用缓存 "
                    f"{embed_report['reused']} 条 → 聚成 {len(clusters)} 簇"
                )
                merged, judgement = kp.juddge_clusters(session, clusters=clusters, llm=client)
                print(
                    f"逐簇判断：合并 {judgement.merged} 簇、保持 {judgement.kept} 簇、"
                    f"失败 {judgement.failed} 簇"
                )
                for note in judgement.notes:
                    print(f"  [注意] {note}", file=sys.stderr)
                result = kp.ProposalResult(candidates=merged)
                session.commit()
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
    if not result.candidates:
        # ⚠️ **零候选也是失败**（实测 bug 27）：模型回了合法 JSON 但没有 `points` 键
        # 时，原来打印"候选 0 条"、退出码 0、一句 note 都没有 —— 脚本与人都看不出
        # "这一步什么都没产出"。退出码是唯一能让 `&&` / CI 发现它的东西。
        print(f"（没有产出任何候选：{result.note or '模型没给出可用候选'}）", file=sys.stderr)
        print("也许：题量太少 / 题都还没挂知识点（先跑 assemble）", file=sys.stderr)
        return 1
    print("下一步：起服务后打开 /admin/review 逐条审")
    return 0


def cmd_merge_points(
    settings: Settings, source: int, target: int, *, dry_run: bool = False
) -> int:
    """把源知识点并进目标知识点（决策 83）—— 事后发现重复时的补救。

    **先跑 `--dry-run`**：打印两边各有多少题 / 关联 / 考察点，确认了再去掉这个开关。
    搬考察点会改变历史归属（掌握度矩阵跟着变），这一步必须让人看见。
    """
    from app.offline.knowledge_pipeline import merge_points

    # ⚠️ 先挡住越界的 id（实测 probe33：`merge-points 9223372036854775808 1` 抛
    # `OverflowError: Python int too large to convert to SQLite INTEGER` —— 一条 Python
    # 栈追到人脸上，谁也不知道是自己敲错了数字）。SQLite 的绑定参数是 64 位。
    for label, value in (("源", source), ("目标", target)):
        if not (0 < value <= SQLITE_MAX_INT):
            print(f"{label} id 超出范围：{value}（合法范围 1 ~ {SQLITE_MAX_INT}）", file=sys.stderr)
            return 2
    if source == target:
        print("源与目标是同一个知识点 —— 没什么可并的", file=sys.stderr)
        return 2

    engine = create_db_engine(settings.resolved_database_path())
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)
        report = merge_points(session, source_id=source, target_id=target, dry_run=dry_run)
        print(report.summary())
        for note in report.notes:
            print(f"  [注意] {note}", file=sys.stderr)
        if dry_run:
            print("这是演练：什么都没改。确认后去掉 --dry-run 再跑一次。")
            return 0
        session.commit()
    print("合并完成（不可撤销 —— 要回退请用 data/backups 的快照）")
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


def cmd_backup(settings: Settings, dest: str, keep: int) -> int:
    """做一份备份并**当场验证它能不能恢复**（ADR-0008）。

    退出码就是答案：0 = 这份备份现在能恢复；非 0 = 不能（cron 因此会报警）。
    异地那一跳交给部署侧的 rclone/aws-cli —— 这一层不引云 SDK。
    """
    from app.backup import backup, record
    from app.config import REPO_ROOT

    db_path = settings.resolved_database_path()
    dest_dir = Path(dest) if dest else REPO_ROOT / "data" / "backups"
    result = backup(db_path, dest_dir, keep=keep)

    engine = create_db_engine(db_path)
    with create_session_factory(engine)() as session:
        # 写报告本身要能失败得下去：库都打不开的时候，备份的意义是"别的机器上还有一份"
        try:
            record(session, result)
            session.commit()
        except Exception as e:  # noqa: BLE001
            print(f"（这次备份的报告没写进库：{e}）", file=sys.stderr)

    if result.archive is not None:
        print(f"备份：{result.archive}（{result.manifest.size_bytes if result.manifest else 0} 字节）")
    for name, passed, why in result.report.checks:
        print(f"  [{'ok  ' if passed else 'FAIL'}] {name}：{why}")
    if result.pruned:
        print(f"按保留策略删掉 {len(result.pruned)} 个文件")
    if not result.ok:
        print(result.error or result.report.summary(), file=sys.stderr)
        return 1
    print("这份备份已验证可恢复")
    return 0


def cmd_verify_backup(path: str) -> int:
    """只做恢复验证（演练用）：`python -m app.cli verify-backup <文件>`。"""
    from app.backup import verify

    target = Path(path)
    report = verify(target)
    for name, passed, why in report.checks:
        print(f"  [{'ok  ' if passed else 'FAIL'}] {name}：{why}")
    print(report.summary())
    return 0 if report.ok else 1


def cmd_set_password(settings: Settings, email: str, password_from_env: str) -> int:
    """改一个账号的口令（决策 58 的**上线前置条件**）。

    ## 为什么必须有这条命令

    迁移会种一个 `owner@local`，它的口令哈希是明文可读的 `PLACEHOLDER__…`，所以线上
    必须打开 `DEEPGRILL_REQUIRE_SECURE_DB=true`（否则进程拒绝启动）。而**在这条命令
    之前，仓库里没有任何改口令的入口** —— `deploy/README.md` 只能写"直接改库即可"，
    等于让人手算一个 scrypt 哈希再 UPDATE 进 SQLite：算错一位就是"登录不上 + 进程拒绝
    启动"两件事同时发生，且没有报错指向真因。

    它复用 `account.admin_reset_password`，于是**规则与后台重置完全一致**：口令至少
    8 位、**同时撤销该账号的全部令牌**（改了口令还把旧会话留着，等于没改）。

    ## 口令从哪来

    · 环境变量 `DEEPGRILL_NEW_PASSWORD`（脚本化用）
    · 否则交互输入两次（`getpass`，不回显）—— **刻意不从命令行参数读**：那会进
      shell 历史与 `ps` 的输出。
    """
    import getpass

    from app.account import service as account

    password = password_from_env or ""
    if not password:
        if not sys.stdin.isatty():
            print(
                "没有终端可以交互输入 —— 请用环境变量：\n"
                "  DEEPGRILL_NEW_PASSWORD='…' python -m app.cli set-password --email owner@local",
                file=sys.stderr,
            )
            return 2
        password = getpass.getpass(f"给 {email} 设置新口令：")
        again = getpass.getpass("再输一遍：")
        if password != again:
            print("两次输入不一致", file=sys.stderr)
            return 2

    engine = create_db_engine(settings.resolved_database_path())
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)
        from app.account import repository as account_repository

        user = account_repository.find_user_by_email(session, email)
        if user is None:
            print(f"没有这个账号：{email}", file=sys.stderr)
            return 2
        try:
            revoked = account.admin_reset_password(
                session, user_id=user.id, new_password=password
            )
        except Exception as e:  # InvalidInput（口令太短）等
            print(f"没改成：{e}", file=sys.stderr)
            return 2
        session.commit()
    print(f"已改 {email} 的口令，并撤销了它的 {revoked} 个令牌（旧会话立即失效）")
    print("接下来：确认 DEEPGRILL_REQUIRE_SECURE_DB=true，再启动服务")
    return 0


def cmd_generate(settings: Settings, point: int | None, count: int, enqueue: bool) -> int:
    """给知识点补公共题（决策 4 的"生成题"那条来源）。

    `--enqueue` 只投一条离线任务（由 worker 跑，适合 cron）；默认**当场跑**（排查用）。
    两条路都过同一份实现（`offline.generation`）。
    """
    from app.deps import get_llm
    from app.offline import generation

    db_path = settings.resolved_database_path()
    engine = create_db_engine(db_path)
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)

        if enqueue:
            from app.offline import jobs as jobs_module
            from app.offline import tasks as _tasks  # noqa: F401  （注册副作用）

            payload: dict[str, object] = {"count": count}
            if point is not None:
                payload["point_id"] = point
            jobs_module.enqueue(session, kind="generate_public_questions", payload=payload)
            session.commit()
            print("已投递任务 generate_public_questions —— 由 worker 执行")
            return 0

        if not settings.llm_api_key:
            print("补题要调模型 —— 先配 DEEPGRILL_LLM_API_KEY", file=sys.stderr)
            return 2

        # ⚠️ 必须**显式传 settings**：`get_llm(settings=Depends(get_settings))` 是给
        # FastAPI 依赖注入用的 —— 直接 `get_llm()` 拿到的是那个 `Depends` 对象本身
        # （实测报 `'Depends' object has no attribute 'llm_api_key'`）。
        # 这一处与离线任务里那一处是同一个坑，两处都有测试守着。
        llm = get_llm(settings)
        if point is not None:
            from app.db.models import KnowledgePoint

            target = session.get(KnowledgePoint, point)
            if target is None:
                print(f"知识点 {point} 不存在", file=sys.stderr)
                return 2
            reports = [
                generation.generate_for_point(session, point=target, count=count, llm=llm)
            ]
        else:
            reports = generation.generate_for_missing(session, llm=llm, per_point=count)
        session.commit()

    for report in reports:
        print(f"  {report.summary()}")
    total = sum(r.created for r in reports)
    print(f"共新增 {total} 道公共题")
    if not reports:
        print("没有缺题的知识点（或它们都还没有考察点定义）")
    return 0


def cmd_worker(settings: Settings, once: bool) -> int:
    """跑离线 worker。`--once` 跑空队列就退出（排查用；不常驻）。

    任务函数由 `app/offline/tasks.py` 注册 —— 与 web 端共用同一批领域函数
    （ADR-0006：worker 与 web 只是启动命令不同）。
    """
    from app.offline import jobs as jobs_module
    from app.offline import tasks as _tasks  # noqa: F401  (side effect: register)
    from app.offline.worker import worker_id

    engine = create_db_engine(settings.resolved_database_path())
    factory = create_session_factory(engine)
    if not once:
        print("常驻模式：Ctrl-C 退出")
        return jobs_module.run_forever(session_factory=factory, worker_id=worker_id())

    handled = 0
    with factory() as session:
        jobs_module.requeue_stale(session)
        session.commit()
    while True:
        with factory() as session:
            if not jobs_module.run_one(session, worker_id=worker_id()):
                break
            session.commit()
        handled += 1
    print(f"跑完 {handled} 条任务（--once 模式）")
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
        from app.offline import seed as seed_module

        print("冷启动顺序（决策 72）：")
        for line in seed_module.cold_start_report(session):
            print(line)
    return 0


def cmd_calibrate(settings: Settings, live: bool) -> int:
    """打印阈值标定报告（§未决 6）。

    **只读**：它不改任何常量（理由写在 `app/offline/calibration.py` 的模块文档里）。
    `--live` 会在**库的副本**上真调两次模型测单位成本 —— 真实库一个字节都不动。
    """
    from app.offline import calibration

    db = settings.resolved_database_path()
    engine = create_db_engine(db)
    with create_session_factory(engine)() as session:
        _ensure_migrated(session)
        extra = [calibration.live_costs(settings)] if live else []
        report = calibration.collect(session, db=str(db), extra=extra)
    print(report.render())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="DeepGrill 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="写入演示用的种子数据（幂等）")
    sub.add_parser("status", help="打印库里的规模")
    propose = sub.add_parser("propose", help="提候选知识点 → 提案文件（供 /admin/review 审）")
    propose.add_argument("--domain", default="未命名领域", help="这批知识点属于哪个领域")
    propose.add_argument(
        "--batched",
        action="store_true",
        help="全量装配那条路：分批提 + 嵌入聚类 + 逐簇判断（需要嵌入供应商）",
    )
    propose.add_argument(
        "--only-pending",
        action="store_true",
        help="只对还没挂载的公共题提候选（补漏用，不必全量重跑）",
    )
    sub.add_parser("mount", help="把题挂到已确认的知识点上（挂不上进待定池）")
    merge = sub.add_parser("merge-points", help="把源知识点并进目标知识点（决策 83）")
    merge.add_argument("source", type=int, help="源知识点 id（它会被删掉）")
    merge.add_argument("target", type=int, help="目标知识点 id（保留）")
    merge.add_argument("--dry-run", action="store_true", help="只看影响，不改任何东西")
    worker = sub.add_parser("worker", help="跑离线 worker（jobs 表；Ctrl-C 退出）")
    worker.add_argument("--once", action="store_true", help="跑空队列就退出（排查用）")
    backup = sub.add_parser("backup", help="做一份备份并当场验证可恢复（ADR-0008）")
    backup.add_argument("--dest", default="", help="备份目录（默认 data/backups）")
    backup.add_argument("--keep", type=int, default=14, help="保留最近几份（默认 14）")
    verify = sub.add_parser("verify-backup", help="只做恢复验证（演练用）")
    verify.add_argument("path", help="备份文件（.db.gz）")
    generate = sub.add_parser(
        "generate", help="给缺题的已确认知识点补公共题（决策 4 的「生成题」）"
    )
    generate.add_argument("--point", type=int, default=None, help="只补这个知识点（默认扫全部）")
    generate.add_argument("--count", type=int, default=3, help="每个知识点最多生成几道")
    generate.add_argument(
        "--enqueue", action="store_true", help="只投一条离线任务（由 worker 跑，适合 cron）"
    )
    calibrate = sub.add_parser(
        "calibrate", help="阈值标定报告（§未决 6；只读，不改常量）"
    )
    calibrate.add_argument(
        "--live", action="store_true", help="额外真调两次模型测单位成本（在库的副本上跑）"
    )
    set_password = sub.add_parser(
        "set-password", help="改一个账号的口令（上线前置：替换 owner 的占位口令，决策 58）"
    )
    set_password.add_argument("--email", required=True, help="账号邮箱，如 owner@local")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    if args.command == "seed":
        return cmd_seed(settings)
    if args.command == "status":
        return cmd_status(settings)
    if args.command == "propose":
        return cmd_propose(
            settings, args.domain, batched=args.batched, only_pending=args.only_pending
        )
    if args.command == "mount":
        return cmd_mount(settings)
    if args.command == "merge-points":
        return cmd_merge_points(settings, args.source, args.target, dry_run=args.dry_run)
    if args.command == "worker":
        return cmd_worker(settings, args.once)
    if args.command == "backup":
        return cmd_backup(settings, args.dest, args.keep)
    if args.command == "verify-backup":
        return cmd_verify_backup(args.path)
    if args.command == "generate":
        return cmd_generate(settings, args.point, args.count, args.enqueue)
    if args.command == "calibrate":
        return cmd_calibrate(settings, args.live)
    if args.command == "set-password":
        # 口令从环境变量读（脚本化），没有就交互输入 —— **不走 argv**（会进 ps 与历史）
        return cmd_set_password(
            settings, args.email, os.environ.get("DEEPGRILL_NEW_PASSWORD", "")
        )
    # `parser.error` 自己会 `SystemExit(2)` —— 后面那句 `return 2` 永远走不到
    # （mypy 的 `warn_unreachable` 会（正确地）指出来）
    parser.error(f"未知命令：{args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
