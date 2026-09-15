"""中等规模试跑：3 批 × 40 题，看新提示词 + 16384 预算能出多少个知识点（用完可删）。

**不跑全量**（用户要求先试）。要回答三个数：
  ① 每批出几个点（改前 40 题出 31 个，改后 10 题出 3 个）
  ② 跨批的**同名**能不能被聚类折起来（改后名字是"能力名"，会跨批重复 —— 这正是
     0.86 那个阈值原本假设的情形）
  ③ 逐簇归并后剩几条 → 按这个比例外推到 3017 道题是多少个点

全程在**库的副本**上跑（嵌入也写副本），真库不动。嵌入与 LLM 都真调。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BATCH = 40
BATCHES = 3
BUDGET = 16384

VARIANT = """从下面这批面试题里提炼**知识地图的骨架**。

共有 {{count}} 道题。**要点：知识点必须比题少得多 —— 目标是 1/3 到 1/5。**
一个知识点应当覆盖 **3 到 10 道**同类题；只有实在无处归并的题才单独成点。
宁可少建点、让一个点的考察点更全，也不要给每道题各起一个名字。

判断题干背后的**能力**而不是字面话题：问"volatile 可见性"与问"volatile 内存屏障"
是同一个点。

## 题目

{{questions}}

## 输出格式（严格照做）

只输出一个 json 对象，不要解释、不要 markdown 围栏：

{
  "points": [
    {
      "name": "知识点名（8 字以内，是能力名不是题目名）",
      "definition": "一到两行：这个点在考什么",
      "exclusions": "什么不算这个点（没有就留空）",
      "criteria": ["考察点一", "考察点二", "考察点三"],
      "question_ids": [1, 2, 5]
    }
  ]
}

`question_ids` 只能用上面出现过的题号，且每道题**必须且只能**出现在一个知识点里。
"""


class BigBudget:
    def __init__(self, inner, budget: int) -> None:
        self._inner, self._budget = inner, budget
        self.usage_total = inner.usage_total

    def chat_json(self, messages, **kwargs):
        kwargs.setdefault("max_tokens", self._budget)
        return self._inner.chat_json(messages, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def main() -> int:
    from app.bank import repository as bank_repository
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.deps import get_embeddings, get_llm
    from app.offline import knowledge_pipeline as kp

    settings = Settings()
    work = ROOT / ".tmp" / "trial-assembly"
    engine = None
    try:
        work.mkdir(parents=True, exist_ok=True)
        copy = work / "copy.db"
        with sqlite3.connect(str(settings.resolved_database_path())) as src, sqlite3.connect(
            str(copy)
        ) as dst:
            src.backup(dst)
        engine = create_db_engine(copy)
        llm, embeddings = get_llm(settings), get_embeddings(settings)
        prompt = kp.prompts.load("offline/propose_points.md").__class__(
            name="trial", text=VARIANT
        )

        with create_session_factory(engine)() as session:
            pool, _ = bank_repository.list_questions(session, None, limit=BATCH * BATCHES)
            print(f"试跑：{len(pool)} 道题（{BATCHES} 批 × {BATCH} 道），预算 {BUDGET}")
            started = time.time()
            candidates: list = []
            for index, batch in enumerate(kp.batch_questions(pool, size=BATCH), start=1):
                blocks = "\n\n".join(
                    f"[题 {q.id}] 题型：{q.kind}｜难度：{q.difficulty}\n题干：{q.stem}"
                    for q in batch
                )
                data, _ = BigBudget(llm, BUDGET).chat_json(
                    [{"role": "user",
                      "content": prompt.render(questions=blocks, count=len(batch))}]
                )
                got = kp.propose_points.__globals__  # noqa: F841  （只为了下面用同一个解析）
                from app.offline.knowledge_pipeline import Candidate

                points = (data or {}).get("points") or []
                made = [
                    Candidate(
                        name=str(p.get("name") or "").strip(),
                        definition=str(p.get("definition") or "").strip(),
                        exclusions=str(p.get("exclusions") or "").strip(),
                        criteria=[str(c).strip() for c in (p.get("criteria") or [])][:kp.MAX_CRITERIA],
                        question_ids=[int(i) for i in (p.get("question_ids") or [])],
                    )
                    for p in points
                ]
                made = [c for c in made if c.name]
                print(f"  第 {index} 批（{len(batch)} 道题）→ {len(made)} 个点"
                      f"：{[c.name for c in made]}")
                candidates.extend(made)

            folded = kp.fold_duplicates(candidates)
            print(f"\n① 提候选：{len(candidates)} 条 → 同名折叠后 {len(folded)} 条"
                  f"（{time.time() - started:.0f}s）")

            merged_or = None
            # ② 先在**同一批向量**上扫阈值：近义名（`检索增强生成` vs `RAG检索增强`）
            # 到底要低到哪一档才聚得起来。33 条向量，秒级。
            from app.offline import embedding_store

            texts = {embedding_store.ref_for_text(kp._candidate_text(c)): kp._candidate_text(c)
                     for c in folded}
            vectors, vreport = embedding_store.vectors_for(
                session, items=texts, kind=embedding_store.CANDIDATE, embeddings=embeddings
            )
            ordered = {ref: vectors[ref] for ref in sorted(vectors)}
            print(f"② 嵌入：新算 {vreport.embedded}、复用 {vreport.reused}（{len(ordered)} 条）")
            print(f"  {'阈值':>6} {'簇数':>6} {'最大簇':>7}")
            chosen = None
            for threshold in (0.85, 0.80, 0.75, 0.70, 0.65, 0.60):
                try:
                    groups = kp.cluster_by_score(ordered, threshold=threshold)
                except TypeError:
                    kp.CLUSTER_THRESHOLD = threshold
                    groups = kp.cluster_by_score(ordered)
                sizes = sorted((len(g) for g in groups), reverse=True)
                print(f"  {threshold:>6.2f} {len(groups):>6} {sizes[0]:>7}")
                if chosen is None and len(groups) <= max(3, len(ordered) // 2):
                    chosen = (threshold, groups)
            if chosen is None:
                chosen = (0.60, groups)
            threshold, groups = chosen
            print(f"  → 选中阈值 {threshold:.2f}（第一次让簇数降到候选数的一半以下）")

            by_ref = {embedding_store.ref_for_text(kp._candidate_text(c)): c for c in folded}
            clusters = [[by_ref[ref] for ref in group] for group in groups]
            merged, judgement = kp.juddge_clusters(session, clusters=clusters, llm=llm)
            print(f"③ 逐簇归并：合并 {judgement.merged}、保持 {judgement.kept}、"
                  f"失败 {judgement.failed} → **{len(merged)} 个点**")
            rate = len(merged) / len(pool)
            print(f"\n外推：{rate:.3f} 点/题 → 3017 道题约 {int(rate * 3017)} 个点"
                  f"（改前实测是 2646 个）")
            session.rollback()
    finally:
        if engine is not None:
            engine.dispose()
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
