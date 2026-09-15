"""看一眼提案的质量：点数、每点覆盖几道题、覆盖了多少题、有没有巨簇（用完可留）。

它是"效果好不好"的判据工具 —— 上一轮那句"产出不可用"就是靠这几个数说出来的
（3017 道题 → 2646 个点）。判定标准：
  · **点数**：ADR-0002 说这套语料背后的知识领域只有十几个，所以点数应当在几十到几百，
    而不是几千（一题一个点等于没建知识层）
  · **每题覆盖**：一个点覆盖 3–10 道题才算"能力点"；大量单题点说明模型还在按题起名
  · **未覆盖**：每道题都该落进某个点，否则那部分题永远挂不上
  · **巨簇**：单个点覆盖几百道题说明聚类把不相干的东西链在一起了

用法：python tools/proposal_stats.py [data/knowledge_proposal.json]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else ROOT / "data" / "knowledge_proposal.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") or []
    if not candidates:
        print(f"{path.name} 里没有候选")
        return 1

    sizes = sorted((len(c.get("question_ids") or []) for c in candidates), reverse=True)
    total = sum(sizes)
    covered = {qid for c in candidates for qid in (c.get("question_ids") or [])}
    unusable = [c for c in candidates if c.get("unusable_reason")]
    criteria = Counter(len(c.get("criteria") or []) for c in candidates)

    print(f"提案：{path.name}（domain={payload.get('domain')!r}）")
    print(f"  点数                {len(candidates)}")
    print(f"  覆盖的题            {len(covered)}（题号总数 {total}，重复计 {total - len(covered)}）")
    print(f"  每点覆盖题数        最大 {sizes[0]}、中位 {sizes[len(sizes) // 2]}、最小 {sizes[-1]}")
    print(f"  单题点              {sum(1 for s in sizes if s == 1)} 个"
          f"（{sum(1 for s in sizes if s == 1) / len(sizes):.0%}）")
    print(f"  考察点条数分布      {dict(sorted(criteria.items()))}")
    print(f"  不合格（写不出考察点等） {len(unusable)} 个")
    print(f"\n  点数判定：", end="")
    n = len(candidates)
    if n <= 500:
        print(f"✅ {n} 个点落在可用区间（几十到几百）")
    elif n <= 1000:
        print(f"⚠️ {n} 个点偏碎（目标几十到几百）")
    else:
        print(f"❌ {n} 个点等于没建知识层（一题一个点的量级）")

    print("\n  覆盖题数最多的 10 个点（巨簇检查）：")
    for cand in sorted(candidates, key=lambda c: -len(c.get("question_ids") or []))[:10]:
        print(f"    {len(cand.get('question_ids') or []):>4} 道  {cand.get('name')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
