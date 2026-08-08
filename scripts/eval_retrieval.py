"""检索评估（Phase 2 §9.3）：对比 text / vector / hybrid 的 hit_rate@5 / MRR@5。

评估集：16 对"同知识点改措辞"题（来自真实题库的近似对，2026-08-08 构建），
格式 (query_stem, expected_question_id)。跑法：
    uv run python scripts/eval_retrieval.py
验收线（DESIGN §9.3）：vector ≥ text 且 hybrid ≥ vector，达标才接 judge。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import get_session, init_db
from app.embed import Embedder
from app.models import Question
from app.retrieval import search_questions

K = 5

# (query 题干, 期望命中的题目 id)；query 为语义改写（低词汇重叠），检验语义检索而非字面匹配
EVAL_SET = [
    ("自动代码审查工具应该如何识别并修复潜在缺陷？", 14),
    ("给一份上百页的长文档自动生成内容摘要，方案怎么设计？", 27),
    ("企业文档库的智能问答：上传 Word/PDF 后如何检索回答？", 23),
    ("Agent 的链式编排相比直接调用大模型有何优势？", 16),
    ("Transformer 里如何给位置信息编码，RoPE 怎么做的？", 33),
    ("为什么各家大模型普遍采用 7B、13B 这类参数量？", 264),
    ("举几个例子说明少样本提示怎么用？", 22),
    ("LangChain 和 LangGraph 在编排 Agent 时各自适合什么？", 218),
    ("关键词检索和语义检索各自的短板是什么？", 216),
    ("vLLM 用什么技巧大幅降低显存占用？", 262),
    ("大模型怎么学会调用外部工具？", 223),
    ("长文档检索时中间段落为什么容易漏掉？", 215),
    ("多头注意力的几种变体有什么区别？", 32),
    ("模型上下文协议解决的是什么问题？", 3),
    ("小团队做 AI 应用怎么避开大厂的资源碾压？", 263),
    ("Agent 相比直接问大模型多出了什么能力？", 210),
]


def main() -> None:
    init_db("sqlite:///data/interview.db")
    with get_session() as session:
        all_q = list(session.scalars(select(Question)))
    embedder = Embedder()

    print(f"评估集 {len(EVAL_SET)} 条，题库 {len(all_q)} 题，k={K}\n")
    print(f"{'method':<10}{'hit_rate@5':<12}{'MRR@5':<10}")
    print("-" * 34)
    results = {}
    for method in ("text", "vector"):
        hits, mrr = _eval(method, all_q, embedder, 0.5)
        results[method] = (hits, mrr)
        print(f"{method:<10}{hits:<12.3f}{mrr:<10.3f}")
    print("\nhybrid 权重扫描（vector 占比）：")
    best = (0.0, None)
    for w in (0.5, 0.6, 0.7, 0.8, 0.9):
        hits, mrr = _eval("hybrid", all_q, embedder, w)
        print(f"hybrid(w={w})      {hits:<12.3f}{mrr:<10.3f}")
        if mrr > best[0]:
            best = (mrr, w)
    print(f"\n最优权重 w={best[1]}（MRR {best[0]:.3f}）")
    final_hits, final_mrr = _eval("hybrid", all_q, embedder, best[1])
    print("\n验收线：vector >= text 且 hybrid(最优权重) >= vector")
    ok = (
        results["vector"][0] >= results["text"][0]
        and final_hits >= results["vector"][0]
    )
    print("达标" if ok else "不达标（不上线）")


def _eval(method, all_q, embedder, weight) -> tuple[float, float]:
    hits = 0
    mrr = 0.0
    for query_stem, expected_id in EVAL_SET:
        top = search_questions(
            query_stem,
            all_q,
            method=method,
            k=K,
            embedder=embedder,
            vector_weight=weight,
        )
        rank = next((i + 1 for i, q in enumerate(top) if q.id == expected_id), None)
        if rank is not None:
            hits += 1
            mrr += 1.0 / rank
    return hits / len(EVAL_SET), mrr / len(EVAL_SET)


if __name__ == "__main__":
    main()
