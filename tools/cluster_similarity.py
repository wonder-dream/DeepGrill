"""候选向量的相似度分布（numpy 版，几秒出结果；用完可删）。

上一个脚本用纯 Python 跑 7 个阈值，10 分钟没跑完（O(n²)）。这里只算**每条的最近邻
相似度分布** —— 它足以回答"阈值该定在哪一档"：阈值 0.75 意味着只有最近邻 ≥0.75 的
那些条会被合并。

用法：python tools/cluster_similarity.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    from sqlalchemy import select

    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.db.models import Embedding
    from app.offline import embedding_store

    payload = json.loads(
        (ROOT / "data" / "knowledge_proposal.json").read_text(encoding="utf-8")
    )
    candidates = payload.get("candidates") or []
    texts = {
        embedding_store.ref_for_text(f"{c.get('name')}｜{c.get('definition')}"): c
        for c in candidates
    }
    engine = create_db_engine(Settings().resolved_database_path())
    try:
        with create_session_factory(engine)() as session:
            rows = session.execute(
                select(Embedding.ref_id, Embedding.vector).where(
                    Embedding.kind == embedding_store.CANDIDATE
                )
            ).all()
    finally:
        engine.dispose()

    refs, mats = [], []
    for ref, blob in rows:
        if ref in texts:
            refs.append(ref)
            mats.append(embedding_store.unpack_vector(blob))
    matrix = np.asarray(mats, dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    print(f"候选 {len(candidates)} 条，命中向量 {len(refs)} 条，维数 {matrix.shape[1]}")

    sims = matrix @ matrix.T
    np.fill_diagonal(sims, -1.0)          # 自己不算
    nearest = sims.max(axis=1)
    print("\n每条候选的**最近邻**余弦分布：")
    for q in (0.5, 0.75, 0.9, 0.95, 0.99):
        print(f"  P{int(q * 100):<3} = {np.quantile(nearest, q):.3f}")
    print(f"  均值 = {nearest.mean():.3f}  最大 = {nearest.max():.3f}")

    print("\n按阈值看「有多少条能找到至少一个同伴」（近似簇的规模）：")
    for th in (0.86, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50):
        joined = int((nearest >= th).sum())
        print(f"  {th:.2f}  {joined:>5} / {len(refs)} 条有同伴（{joined / len(refs):.0%}）")

    # 用**真实的那份** `cluster_by_score` 量簇大小（连通分量，可能链得比"有同伴"长）
    from app.offline import knowledge_pipeline as kp

    ordered = {ref: [float(x) for x in vec] for ref, vec in zip(refs, mats, strict=True)}
    print("\n真聚类的簇大小（连通分量）：")
    print(f"  {'阈值':>6} {'簇数':>7} {'最大簇':>7} {'≥10 的簇':>9}  说明")
    for th in (0.80, 0.75, 0.70):
        try:
            groups = kp.cluster_by_score(ordered, threshold=th)
        except TypeError:
            kp.CLUSTER_THRESHOLD = th
            groups = kp.cluster_by_score(ordered)
        sizes = sorted((len(g) for g in groups), reverse=True)
        big = sum(1 for n in sizes if n >= 10)
        print(f"  {th:>6.2f} {len(groups):>7} {sizes[0]:>7} {big:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
