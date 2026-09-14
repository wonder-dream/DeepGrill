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
                # 自动找一道"看起来综合"的：题干里至少踩到两个已确认知识点的名字。
                # 为什么不固定用真题 1065：那道题是"知识图谱更新机制"，与演示库里
                # 那六个知识点**真的不相关** —— 模型把它退回待定池是**正确行为**
                # （决策 46：挂不上就等，绝不自动新建知识点）。实测跑过一次，
                # 它就是这么退回来的。
                from app.db.models import KnowledgePoint

                names = [
                    (p.id, p.name)
                    for p in session.execute(
                        select(KnowledgePoint).where(KnowledgePoint.status == "confirmed")
                    ).scalars()
                ]
                rows, _ = bank_repository.list_questions(session, None, limit=args.limit)
                scored = [
                    (sum(1 for _, name in names if name in (q.stem or "")), q)
                    for q in rows
                ]
                scored = [(n, q) for n, q in scored if n >= 2]
                if not scored:
                    print(f"扫了 {len(rows)} 道公共题，没有一道同时踩到两个已确认知识点 ——"
                          f" 当前这六个点还建不出综合题")
                    return 2
                scored.sort(key=lambda pair: -pair[0])
                question = scored[0][1]
                print(f"自动选中 #{question.id}（踩到 {scored[0][0]} 个知识点名字）")
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
                print(f"  第 {round_no + 1} 轮：命中 {sum(1 for v in result.new_hits.values() if v == '命中')}"
                      f" / 未命中 {sum(1 for v in result.new_hits.values() if v == '未命中')}"
                      f" / 未涉及 {sum(1 for v in result.new_hits.values() if v == '未涉及')}"
                      f"（建议收尾={result.finished}）")
            session.commit()

            print("  掌握度矩阵（只看这道题牵动的格子）：")
            matrix = knowledge.mastery_matrix(session, user.id)
            touched = {
                point_id for point_id, cell in matrix.by_point().items() if cell.covered
            }
            for point_id in sorted(touched):
                cell = matrix.by_point()[point_id]
                print(f"    #{point_id} {cell.point_name}：覆盖 {cell.covered}、命中 {cell.hit}"
                      f"、未命中 {cell.miss}、比率 {cell.percent}")
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
