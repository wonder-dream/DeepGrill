"""RAG 生成层评估 v2：判分参考回答 注入 vs 不注入 A/B，对比式 LLM 裁判。

跑法：
    uv run python scripts/eval_rag_gen.py

- 样本：卡码 Go 8 题（标准答案 = 该页知识块）
- 判分：注入组（带知识）/ 不注入组 各判分一次（judge 真实 DeepSeek）
- 裁判：对比式（A/B/tie，评判 2 次取多数）——pairwise 比绝对打分稳定
- 结论：注入胜出次数占比（注入有益 if 明显 > 不注入）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import load_config, secret_value
from app.db import get_session, init_db
from app.embed import Embedder
from app.judge.judge import judge
from app.llm.llm_client import LLMClient
from app.models import KnowledgeChunk, Question, QuestionType
from app.web.routes import _knowledge_for

SAMPLE_SLUGS = [
    "go_gmp_model",
    "go_goroutine_thread_process",
    "go_channel",
    "go_memory",
    "go_goroutine",
    "go_map_concurrent_panic",
    "go_syncMutex_normal_starvation",
    "go_defer",
]

TRANSCRIPT = [{"role": "user", "content": "先讲核心概念和原理，再补充具体机制和适用场景。"}]

COMPARE_PROMPT = """比较两份参考答案与标准答案的覆盖与准确度，判断哪份更好。

标准答案：
{reference}

回答 A：
{answer_a}

回答 B：
{answer_b}

只输出 JSON，不要其他文字：{{"better": "A" 或 "B" 或 "tie"}}"""


def load_sample() -> list[dict]:
    out = []
    with get_session() as s:
        for slug in SAMPLE_SLUGS:
            chunks = s.scalars(
                select(KnowledgeChunk).where(KnowledgeChunk.title.like(f"%{slug}%"))
            ).all()
            if not chunks:
                continue
            stem = chunks[0].content.split("\n")[0].lstrip("# ").strip()
            reference = "\n".join(c.content for c in chunks)[:3000]
            out.append({"slug": slug, "stem": stem, "reference": reference})
    return out


def make_question(stem: str) -> Question:
    q = Question(
        source_id=1, type=QuestionType.knowledge, stem=stem,
        difficulty=3, good_criteria=["完整、准确、结构清晰"], bad_criteria=["答非所问"],
    )
    q._tags = ["Go"]  # 非持久属性：judge 读题标签
    return q


def judge_once(llm, q, knowledge) -> str | None:
    j = judge(q, TRANSCRIPT, llm._model, llm, knowledge=knowledge)
    if j.status != "ok" or not j.reference_answer:
        return None
    return j.reference_answer


def compare(llm, reference, a, b) -> str | None:
    """A/B 比较，评判 2 次取多数。"""
    votes = []
    for _ in range(2):
        try:
            parsed = llm.complete(
                [{"role": "user", "content": COMPARE_PROMPT.format(
                    reference=reference, answer_a=a, answer_b=b)}],
                json_schema={},
            )
            v = parsed.get("better") if isinstance(parsed, dict) else None
            if v in ("A", "B", "tie"):
                votes.append(v)
        except Exception:
            pass
    if not votes:
        return None
    from collections import Counter

    return Counter(votes).most_common(1)[0][0]


def main() -> None:
    init_db("sqlite:///data/interview.db")
    cfg = load_config(Path("config.yaml"))
    llm = LLMClient(cfg.llm.generate_model, cfg.llm.base_url, secret_value(cfg.llm.api_key_env))
    embedder = Embedder()
    sample = load_sample()
    print(f"样本 {len(sample)} 题（A=注入，B=不注入）\n")

    on_win = off_win = tie = failed = 0
    print("| 题 | 结果 |")
    print("|---|---|")
    for item in sample:
    q = make_question(item["stem"])
    k = _knowledge_for(q.stem, getattr(q, "_tags", []), lambda: embedder, k=5)
        a = judge_once(llm, q, k)
        b = judge_once(llm, q, None)
        if not a or not b:
            failed += 1
            print(f"| {item['slug']} | 判分失败 |")
            continue
        r = compare(llm, item["reference"], a, b)
        if r == "A":
            on_win += 1
        elif r == "B":
            off_win += 1
        elif r == "tie":
            tie += 1
        else:
            failed += 1
        print(f"| {item['slug']} | {'注入更优' if r == 'A' else '不注入更优' if r == 'B' else '相当' if r == 'tie' else '裁判失败'} |")

    total = on_win + off_win + tie
    print(f"\n注入更优 {on_win}/{total}（{on_win/total*100:.0f}%）｜ 不注入更优 {off_win} ｜ 相当 {tie} ｜ 失败 {failed}")


if __name__ == "__main__":
    main()
