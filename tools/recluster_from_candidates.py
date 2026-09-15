"""从**已落盘的候选**重跑聚类 + 归并 —— 调阈值/上限时不必再花提候选那笔钱。

这一轮（决策 81/82）加的两个东西就是为它服务的：
  · `data/knowledge_candidates.json`（归并前的 636 条）
  · 嵌入按文本哈希缓存 → 文本不变则**全部命中**，零嵌入成本

用法：python tools/recluster_from_candidates.py
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

CANDIDATES = ROOT / "data" / "knowledge_candidates.json"
PROPOSAL = ROOT / "data" / "knowledge_proposal.json"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python tools/recluster_from_candidates.py")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只跑聚类（纯 Python，零 LLM 调用）：先看形状对不对，再决定要不要付归并的钱",
    )
    args = parser.parse_args(argv)
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.deps import get_embeddings, get_llm
    from app.offline import knowledge_pipeline as kp

    payload = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    candidates = [
        kp.Candidate(
            name=str(c.get("name") or ""),
            definition=str(c.get("definition") or ""),
            exclusions=str(c.get("exclusions") or ""),
            criteria=[str(x) for x in (c.get("criteria") or [])],
            question_ids=[int(x) for x in (c.get("question_ids") or [])],
        )
        for c in payload.get("candidates") or []
    ]
    print(f"从盘上读回候选 {len(candidates)} 条")

    settings = Settings()
    engine = create_db_engine(settings.resolved_database_path())
    llm, embeddings = get_llm(settings), get_embeddings(settings)
    try:
        with create_session_factory(engine)() as session:
            clusters, report = kp.cluster_candidates(
                session, candidates=candidates, embeddings=embeddings
            )
            per_cluster = sorted(
                (len({qid for c in group for qid in c.question_ids}) for group in clusters),
                reverse=True,
            )
            print(f"聚类：新算 {report['embedded']}、复用 {report['reused']} → "
                  f"{len(clusters)} 簇")
            print(f"每簇覆盖题数：最大 {per_cluster[0]}、中位 "
                  f"{per_cluster[len(per_cluster) // 2]}、最小 {per_cluster[-1]}")
            over = [n for n in per_cluster if n > kp.MAX_POINT_QUESTIONS]
            print(f"超过 {kp.MAX_POINT_QUESTIONS} 道题的簇：{len(over)} 个"
                  f"{'（' + str(over[:5]) + '）' if over else ' ✅ 上限生效'}")
            if args.dry_run:
                print("\n--dry-run：到此为止（聚类是纯 Python，零 LLM 调用）")
                return 0
            merged, judgement = kp.juddge_clusters(session, clusters=clusters, llm=llm)
            print(f"归并：合并 {judgement.merged}、保持 {judgement.kept}、"
                  f"失败 {judgement.failed} → {len(merged)} 个点")
            session.commit()
    finally:
        engine.dispose()
        llm.close()

    PROPOSAL.write_text(
        json.dumps(
            kp.ProposalResult(candidates=merged).to_json()
            | {"domain": payload.get("domain") or ""},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"写到 {PROPOSAL}")
    print("下一步：python tools/proposal_stats.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
