"""聚类阈值标定：拿**已经落库的 2706 条向量**离线试出该用哪个阈值（用完可删）。

背景：装配跑完了，但 `CLUSTER_THRESHOLD = 0.86` 把 2706 条候选聚成了 **2646 簇**
—— 等于没聚。0.86 是 v1 拿来比**题干**的（v1 用 0.85），而这里比的是**候选名字 +
定义**（短中文短语），短短语的余弦整体偏低。

嵌入有缓存（2706 条已在 `embeddings` 表里），所以**重跑聚类不花钱** ——
这一脚本只读库 + 只算余弦，不调任何模型。

用法：python tools/calibrate_cluster_threshold.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROPOSAL = ROOT / "data" / "knowledge_proposal.json"


def main() -> int:
    from sqlalchemy import select

    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.db.models import Embedding
    from app.offline import embedding_store, knowledge_pipeline as kp

    payload = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") or []
    print(f"提案里的候选：{len(candidates)} 条")

    texts = {}
    for cand in candidates:
        text = kp._candidate_text(kp.Candidate(**{k: cand.get(k) for k in
                                                  ("name", "definition", "exclusions",
                                                   "criteria", "question_ids")}))
        texts[embedding_store.ref_for_text(text)] = text
    print(f"去重后的文本：{len(texts)} 条")

    engine = create_db_engine(Settings().resolved_database_path())
    try:
        with create_session_factory(engine)() as session:
            rows = session.execute(
                select(Embedding.ref_id, Embedding.vector).where(
                    Embedding.kind == embedding_store.CANDIDATE
                )
            ).all()
        vectors = {
            ref: embedding_store.unpack_vector(vector)
            for ref, vector in rows
            if ref in texts
        }
        print(f"库里命中的向量：{len(vectors)} 条（维数 "
              f"{len(next(iter(vectors.values()))) if vectors else 0}）")
        if not vectors:
            print("库里没有对应向量 —— 装配时写的 ref 与现在的文本对不上？")
            return 1

        ordered = {ref: vectors[ref] for ref in sorted(vectors)}
        print(f"\n{'阈值':>6} {'簇数':>7} {'最大簇':>7} {'单例占比':>9}  说明")
        for threshold in (0.86, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55):
            try:
                groups = kp.cluster_by_score(ordered, threshold=threshold)
            except TypeError:  # 那个版本不收 threshold 参数
                kp.CLUSTER_THRESHOLD = threshold
                groups = kp.cluster_by_score(ordered)
            sizes = sorted((len(g) for g in groups), reverse=True)
            singletons = sum(1 for n in sizes if n == 1)
            note = ""
            if len(groups) > 400:
                note = "← 还是一题一个点"
            elif 10 <= len(groups) <= 120:
                note = "← 落在 ADR-0002 说的量级（十几个到几十个）"
            print(f"{threshold:>6.2f} {len(groups):>7} {sizes[0]:>7} "
                  f"{singletons / len(groups):>8.0%}  {note}")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
