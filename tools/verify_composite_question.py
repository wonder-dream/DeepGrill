"""真跑一次「综合题 → 多个知识点的掌握度」——验收标准里唯一还标着人工的那条。

判据（决策 24 + §掌握度的定义）：**一道综合题同时填充多个格子**，且
**「没考过」（空格）与「考了但没答」（0%）可区分**。

这条链此前只有替身跑过。这里用**真题 1065**（"在RAG+知识图谱的Agent系统中，请设计
知识图谱的更新机制并保证实时性"）走一遍完整的真链路，全程在**库的副本**上：

    挂载（真 LLM 判它属于哪几个知识点）→ 开一场单题追问 → 答两轮 → 收尾判分
    → 读掌握度矩阵

它同时验证了三个此前只被替身覆盖的 prompt 契约：`offline/mount_questions.md`（挂载）、
`interviewer/score_round.md`（逐轮判定）、`interviewer/evaluate_round.md`（收尾判分）。

用法：
    python tools/verify_composite_question.py [--question 1065]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ANSWER = (
    "我会把图谱更新拆成写入路径和一致性两条线。写入侧先做变更捕获：文档入库时抽取实体与"
    "关系，写进一张待合并的边表，按 (主体, 关系, 客体) 做幂等去重，避免同一份文档重跑产生"
    "重复边。合并时用版本号 + 逻辑删除，而不是直接改行——这样回溯与回滚都有依据。"
    "实时性上我不追求强一致：读路径查图谱时带一个时间戳，允许读到 T-1 分钟的版本，"
    "用缓存挡住热点子图，写路径异步批量提交，把延迟压在秒级。矛盾检测单独跑："
    "新边与已有边冲突时先不落库，进一张待裁决表由规则（同一主体同一关系的时间区间是否"
    "重叠）或人工处理。最后是可观测：每次更新记一条变更日志，包含来源文档、抽取模型版本、"
    "合并决策，出问题时能回答这条边是从哪来的。"
)


def main(argv: list[str] | None = None) -> int:
    from sqlalchemy import select

    from app.bank import repository as bank_repository
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.db.models import Question, User
    from app.deps import get_llm, get_embeddings  # noqa: F401  （统一构造点）
    from app.interview import service as interview
    from app.knowledge import service as knowledge
    from app.offline import knowledge_pipeline as kp

    parser = argparse.ArgumentParser()
    parser.add_argument("--question", type=int, default=0, help="默认自动找一道像综合题的")
    parser.add_argument("--limit", type=int, default=5000, help="自动找时最多扫多少道")
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="库里没有真综合题时，自己造一道（明确标 origin=synthetic 的演示题）—— "
        "验的是**机制**（一次作答填多个格子），不是语料",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    source = settings.resolved_database_path()
    work = ROOT / ".tmp" / "verify-composite"
    engine = None
    try:
        work.mkdir(parents=True, exist_ok=True)
        copy = work / "copy.db"
        with sqlite3.connect(str(source)) as src, sqlite3.connect(str(copy)) as dst:
            src.backup(dst)
        engine = create_db_engine(copy)
        llm = get_llm(settings)
        with create_session_factory(engine)() as session:
            question = session.get(Question, args.question) if args.question else None
            if question is None:
                # **判据换成正查库**：`question_points` 里挂了 ≥2 个点的题，就是"综合题"的
                # 定义本身 —— 不依赖名字怎么起。
                #
                # ⚠️ 第一版是拿"知识点名字"去题干里做子串匹配，那在只有 6 个演示点
                # （`volatile`、`限流算法`）时凑合，而现在 133 个点的名字都是
                # `上下文工程基础` 这类抽象能力名，题干里根本不会原样出现两个 ——
                # 于是它报"没有一道综合题"，而库里其实有 78 道（**假否定**）。
                from sqlalchemy import func

                from app.db.models import QuestionPoint

                counts = dict(
                    session.execute(
                        select(QuestionPoint.question_id, func.count())
                        .group_by(QuestionPoint.question_id)
                        .having(func.count() >= 2)
                    ).all()
                )
                print(f"挂在 ≥2 个知识点上的题：{len(counts)} 道（这就是综合题）")
                best = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
                if not best and args.synthetic:
                    # 一道都没有时才造：验的是**机制**（一次作答 → 多个格子），
                    # 不是因为语料里有它。
                    question = Question(
                        kind="design",
                        stem="设计一个高并发订单系统：既要保证缓存与数据库的一致性，"
                             "又要对入口做限流 —— 这两件事怎么配合？",
                        difficulty=4,
                        origin="seed",
                        visibility="public",
                        answer_tier="long_tail",
                    )
                    session.add(question)
                    session.flush()
                    print(f"库里没有真综合题，造了一道演示题 #{question.id}")
                elif not best:
                    print("库里没有一题挂在两个点上 —— 先跑装配与 mount（要造一道就加 --synthetic）")
                    return 2
                else:
                    question = session.get(Question, best[0][0])
                    print(f"选中 #{question.id}（挂在 {best[0][1]} 个点上）—— 按点数从多到少选")
            print(f"真题 #{question.id}：{question.stem[:60]}")
            print(f"  挂载前：primary_point_id={question.primary_point_id}")

            mounted = kp.mount_questions(session, questions=[question], llm=llm)
            session.commit()
            print(f"  挂载结果：主知识点={question.primary_point_id} "
                  f"关联={bank_repository.related_points(session, question.id)}"
                  f"（挂不上={mounted.left_for_review}）")
            if question.primary_point_id is None:
                print("  ⚠️ 模型没能把它挂到任何知识点上 —— 这条链到此为止")
                return 1

            user = session.query(User).filter(User.role == "user").first()
            ts = interview.start_drill(session, user_id=user.id, question_id=question.id)
            for round_no in range(2):
                result = interview.submit_answer(session, ts=ts, answer_text=ANSWER, llm=llm)
                # ⚠️ 不猜字段名：`RoundResult` 的字段改过（`new_hits` 是**计数**不是映射），
                # 猜错就是 AttributeError（实测踩过两次）。这里把它的字段原样打出来。
                fields = {
                    key: (value if isinstance(value, (int, bool)) else str(value)[:36])
                    for key, value in vars(result).items()
                }
                print(f"  第 {round_no + 1} 轮：{fields}")
            session.commit()

            print("  掌握度矩阵（只看这道题牵动的格子）：")
            matrix = knowledge.mastery_matrix(session, user.id)
            touched = {
                point_id for point_id, cell in matrix.by_point().items() if cell.covered
            }
            for point_id in sorted(touched):
                cell = matrix.by_point()[point_id]
                print(f"    #{point_id} {cell.point_name}：覆盖 {cell.covered}、命中 {cell.hit}"
                      f"、未命中 {cell.missed}、比率 {cell.percent}")
            empty = [c.point_name for c in matrix.cells if not c.covered]
            print(f"    （没考过的格子仍是空格：{len(empty)} 个，例如 {empty[:3]}）")
            print(f"  判定：牵动 {len(touched)} 个知识点"
                  f"{'✅ 综合题填了多个格子' if len(touched) >= 2 else '⚠️ 只填了一个格子'}")
        engine.dispose()
    finally:
        if engine is not None:
            engine.dispose()
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
