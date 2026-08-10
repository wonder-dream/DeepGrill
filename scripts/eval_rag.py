"""RAG 检索层评估：卡码 Go 正样本 + 题库跨主题负样本 × 3 配置对比。

跑法：
    uv run python scripts/eval_rag.py

配置：
    A 单查询纯向量（baseline）    B 多查询纯向量（无文本路）    C 多查询+文本路（当前实现）

指标：Hit@5 / MRR@5 / Precision@5（正样本 40 题，相关块 = title 含该页 slug）
      误命中率（负样本 20 题 Redis/MySQL/Java 题库题，命中知识库任意块即误命中）
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import get_session, init_db
from app.embed import Embedder
from app.models import Question, QuestionType
from app.retrieval import KnowledgeIndex

K = 5
OUT_DIR = Path("data/knowledge/kamacoder")


def slug_terms(slug: str) -> list[str]:
    return re.findall(r"[a-zA-Z]+", slug) or []


def load_queries() -> tuple[list[tuple[str, str]], list[str]]:
    manifest = json.loads((OUT_DIR / "manifest.json").read_text(encoding="utf-8"))
    pos = [(slug, title) for slug, title in manifest.items()]
    with get_session() as s:
        rows = s.scalars(
            select(Question).where(Question.type == QuestionType.knowledge)
        ).all()
    neg = []
    for q in rows:
        if any(t in ("Redis", "MySQL", "Java", "并发") for t in (q.tags or [])):
            neg.append(q.stem)
        if len(neg) >= 20:
            break
    return pos, neg


def relevant(hits, slug: str) -> int:
    """第一个相关块的位置（0-based），无则 -1。相关 = title 含该页 slug。"""
    for i, (title, _c) in enumerate(hits):
        if slug in title:
            return i
    return -1


def run_cfg(name, idx, embedder, queries, pos_mode=True):
    hits = mrr = prec = 0.0
    for item in queries:
        if pos_mode:
            slug, title = item
            texts = [title] + slug_terms(slug)
        else:
            title = item
            texts = [title]
        vecs = embedder.encode(texts)
        if name == "A":
            out = idx.search(vecs[0], k=K)
        elif name == "B":
            cand = {}
            for v in vecs:
                for cid, sc in idx._vector_scores(v, K * 2).items():
                    cand[cid] = max(cand.get(cid, 0.0), sc)
            ids = sorted(cand, key=cand.get, reverse=True)[:K]
            out = idx._chunks_for({i: cand[i] for i in ids}, K)
        else:  # C
            out = idx.search_multi(vecs, texts, k=K)
        if pos_mode:
            r = relevant(out, slug)
            if r >= 0:
                hits += 1
                mrr += 1.0 / (r + 1)
                prec += sum(1 for t, _ in out if slug in t) / len(out)
        else:
            hits += 1 if out else 0  # 跨主题命中任意块即误命中
    n = len(queries)
    if pos_mode:
        print(f"| {name} | {hits/n*100:.1f}% | {mrr/n:.3f} | {prec/n:.3f} |")
    else:
        print(f"| {name} | 误命中率 {hits/n*100:.1f}%（{int(hits)}/{n}） |")


def main() -> None:
    init_db("sqlite:///data/interview.db")
    pos, neg = load_queries()
    print(f"正样本 {len(pos)} 题 / 负样本 {len(neg)} 题")
    embedder = Embedder()
    idx = KnowledgeIndex()

    print("\n### 检索层评估（Hit@5 / MRR@5 / Precision@5）")
    print("| 配置 | Hit@5 | MRR@5 | Precision@5 |")
    print("|---|---|---|---|")
    for name in ("A", "B", "C"):
        run_cfg(name, idx, embedder, pos, pos_mode=True)

    print("\n### 跨主题误命中（Redis/MySQL/Java 题 20 道）")
    print("| 配置 | 误命中 |")
    print("|---|---|")
    for name in ("A", "B", "C"):
        run_cfg(name, idx, embedder, neg, pos_mode=False)


if __name__ == "__main__":
    main()
