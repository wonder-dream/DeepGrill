"""反复确认：覆盖是否真的完整、拆簇是否可复现（用完即删）。

用户要"仔细检查、反复确认"，所以这里只查**能查的事实**：

  ① **哪些题没被任何候选认领** —— 上一轮统计说覆盖 3014/3017，差 3 道。是哪 3 道？
     为什么？（覆盖不全会让那些题永远挂不上，正是验收判据里"待定池为 0"关心的事）
  ② **拆簇可复现吗** —— 同一批候选连跑两次，簇的划分必须**逐字节相同**，否则
     "调参 → 看结果"这个方法本身就不可信（同样的输入得到不同输出，说明有随机性）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.deps import get_embeddings
    from app.offline import knowledge_pipeline as kp

    payload = json.loads((ROOT / "data" / "knowledge_candidates.json").read_text(encoding="utf-8"))
    raw = payload.get("candidates") or []
    candidates = [
        kp.Candidate(
            name=str(c.get("name") or ""),
            definition=str(c.get("definition") or ""),
            exclusions=str(c.get("exclusions") or ""),
            criteria=[str(x) for x in (c.get("criteria") or [])],
            question_ids=[int(x) for x in (c.get("question_ids") or [])],
        )
        for c in raw
    ]

    # ① 覆盖
    claimed = {qid for c in candidates for qid in c.question_ids}
    conn = sqlite3.connect(Settings().resolved_database_path())
    public = {row[0] for row in conn.execute(
        "SELECT id FROM questions WHERE owner_user_id IS NULL"
    )}
    missing = sorted(public - claimed)
    print(f"① 覆盖：候选认领 {len(claimed)} 道、题库公共题 {len(public)} 道、"
          f"**未被认领 {len(missing)} 道**")
    for qid in missing[:6]:
        row = conn.execute("SELECT stem FROM questions WHERE id = ?", (qid,)).fetchone()
        print(f"    #{qid} {str(row[0])[:56] if row else '（查不到）'}")
    extra = sorted(claimed - public)
    if extra:
        print(f"    ⚠️ 候选里认领了不在公共池里的题号：{extra[:6]}")

    # ② 拆簇可复现吗
    settings = Settings()
    engine = create_db_engine(settings.resolved_database_path())
    embeddings = get_embeddings(settings)
    try:
        signatures = []
        for round_no in (1, 2):
            with create_session_factory(engine)() as session:
                clusters, report = kp.cluster_candidates(
                    session, candidates=candidates, embeddings=embeddings
                )
            signature = sorted(
                sorted(c.name for c in group) for group in clusters
            )
            signatures.append(signature)
            print(f"② 第 {round_no} 次聚类：{len(clusters)} 簇、嵌入新算 "
                  f"{report['embedded']}、复用 {report['reused']}")
        same = signatures[0] == signatures[1]
        print(f"   两次划分{'完全相同 ✅ 可复现' if same else '不同 ❌ 有随机性'}")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
