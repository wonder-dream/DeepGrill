"""全量装配的**演习**：拿 3007 道真题把管道跑一遍，但不花一分钱。

## 为什么先演习

真的装配要花真钱（逐簇归并是 LLM 调用），而且它是几十分钟的量级 ——
**把钱花在一个会中途崩掉的管道上是最亏的**。所以先用假嵌入 + 假 LLM 把**规模**跑一遍：
分批、聚类、逐簇归并的形状完全不变，只有模型换成替身。

它专门盯那些"小数据看不出来"的问题：

· 3007 道题分成 76 批时，跨批重复能不能被 `fold_duplicates` 收掉
· 嵌入缓存命中率（重跑应当几乎全是 `reused`，否则每次都在重新烧钱）
· **SQLite 的绑定变量上限**：`IN (...)` 塞几千个 id 会直接报错，
  而单元测试里只有六道题，永远撞不到
· 各阶段耗时与堆峰值（2C2G 上内存是硬约束）

用法：
    python tools/assembly_rehearsal.py            # 一遍
    python tools/assembly_rehearsal.py --twice    # 两遍，看第二遍的缓存命中
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class FakeEmbeddings:
    """确定性的词袋哈希向量（复用生产里那份），**不联网**。"""

    def __init__(self) -> None:
        from app.llm.embeddings import FakeEmbeddings as Real

        self._inner = Real()
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return self._inner.embed(texts)


class FakeLLM:
    """按 prompt 形状给一条**结构正确**的假回复。

    它不需要"答得好"—— 它要证明的是管道能把这 76 份回复接起来、把 `question_ids`
    一路带到最后。所以它从 prompt 里把题号抠出来，原样放进候选里。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.kinds: dict[str, int] = {}

    def chat_json(self, messages, **kwargs):
        self.calls += 1
        prompt = messages[0]["content"]
        # 判据是**载荷形状**：提候选的 prompt 里有 `[题 N]` 行，归并的没有。
        # 用"归并"这种词会判错（模板里写的是"合并"）—— 实测踩过：91 次归并调用
        # 被当成提候选，于是每簇都"失败"，而真因是替身判错了种类。
        kind = "propose" if "[题 " in prompt else "merge"
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        ids = [int(m) for m in re.findall(r"\[题 (\d+)\]", prompt)]

        if kind == "merge":
            # 名字固定给：候选块在 prompt 里可能缩进或跟在引导句后面，
            # 用正则去"抠第一个名字"会抠不到，于是整簇被当成归并失败 ——
            # 那是**替身**的毛病，不该让它污染演习结果（实测踩过：91 簇全"失败"）。
            return {
                "name": f"合并后的知识点（{len(ids) or 1} 条候选）",
                "definition": "演习用的定义",
                "exclusions": "",
                "criteria": ["演习判据一", "演习判据二"],
            }, None
        # 每两道路合成一条候选：造出"跨批重复"的形状，逼 fold_duplicates 干活
        points = [
            {
                "name": f"演习知识点 {chunk[0]}",
                "definition": "演习用的定义",
                "exclusions": "",
                "criteria": ["演习判据"],
                "question_ids": chunk,
            }
            for chunk in (ids[i : i + 2] for i in range(0, len(ids), 2))
        ]
        return {"points": points}, None

    def chat(self, messages, **kwargs):
        raise AssertionError("演习里不该走纯文本这条路")

    def close(self) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    from app.bank import repository as bank_repository
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.offline import knowledge_pipeline as kp

    parser = argparse.ArgumentParser(prog="python tools/assembly_rehearsal.py")
    parser.add_argument("--twice", action="store_true", help="跑两遍，看第二遍的缓存命中")
    parser.add_argument("--batch", type=int, default=kp.PROPOSE_BATCH)
    args = parser.parse_args(argv)

    source = Settings().resolved_database_path()
    work = ROOT / ".tmp" / "rehearse-assembly"
    engine = None
    try:
        work.mkdir(parents=True, exist_ok=True)
        copy = work / "copy.db"
        with sqlite3.connect(str(source)) as src, sqlite3.connect(str(copy)) as dst:
            src.backup(dst)
        print(f"副本：{copy}（{copy.stat().st_size / 1e6:.1f} MB）")

        engine = create_db_engine(copy)
        for round_no in range(1, (2 if args.twice else 1) + 1):
            with create_session_factory(engine)() as session:
                questions = bank_repository.public_questions(session)
                embeddings, llm = FakeEmbeddings(), FakeLLM()
                print(f"\n=== 第 {round_no} 遍：{len(questions)} 道公共题，批大小 {args.batch}")
                tracemalloc.start()
                started = time.time()

                proposal = kp.propose_batched(
                    session, questions=questions, llm=llm, batch_size=args.batch
                )
                print(
                    f"  ① 分批提候选：{proposal.batches} 批、候选 {len(proposal.candidates)} 条"
                    f"（失败 {proposal.failed_batches} 批）[{time.time() - started:.1f}s]"
                )
                clusters, report = kp.cluster_candidates(
                    session, candidates=proposal.candidates, embeddings=embeddings
                )
                print(
                    f"  ② 嵌入聚类：新算 {report['embedded']}、复用 {report['reused']}"
                    f" → {len(clusters)} 簇 [{time.time() - started:.1f}s]"
                )
                merged, judgement = kp.juddge_clusters(session, clusters=clusters, llm=llm)
                print(
                    f"  ③ 逐簇归并：合并 {judgement.merged}、保持 {judgement.kept}、"
                    f"失败 {judgement.failed} → {len(merged)} 条 [{time.time() - started:.1f}s]"
                )
                claimed = sorted({qid for c in merged for qid in c.question_ids})
                print(f"  被认领的题：{len(claimed)} 道（其余留在待定池）")
                print(f"  LLM 调用 {llm.calls} 次 {llm.kinds}；嵌入调用 {embeddings.calls} 次")
                _, peak = tracemalloc.get_traced_memory()
                print(f"  Python 堆峰值：{peak / 1e6:.1f} MB")
                session.commit()

            with create_session_factory(engine)() as session:
                import sqlalchemy as sa

                from app.db.models import Embedding

                cached = session.execute(
                    sa.select(sa.func.count()).select_from(Embedding)
                ).scalar_one()
                print(f"  嵌入缓存表里现在有 {cached} 条向量")
    finally:
        if engine is not None:
            engine.dispose()
        shutil.rmtree(work, ignore_errors=True)
        print("\n（副本已删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
