"""提候选提示词的 A/B：同一批题，看「改前 / 改后」各出多少条候选（用完可删）。

背景：302 批 × 每批 10 题 → 2706 条候选 ≈ 一题一个点，而 ADR-0002 说这套语料真正的
知识领域只有十几个。预算已经排除（40 题在 16384 下能跑通，但仍出 31 条）—— 所以
嫌疑在提示词：它没要求模型**归并**，模型就给每道题各起了一个名字。

这里只回答一件事：**加一句"尽量少、一个点覆盖多道题"能不能把候选数压下来**。
结论若是"能"，下一轮再把它落成真正的 prompt 文件并全量重跑。

用法：python tools/prompt_ab.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

#: 候选版提示词 —— 与真文件同一组变量（questions / count），只有"粒度"这一段不同
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
    from app.deps import get_llm
    from app.llm import prompts

    settings = Settings()
    llm = get_llm(settings)
    engine = create_db_engine(settings.resolved_database_path())
    try:
        with create_session_factory(engine)() as session:
            pool, _ = bank_repository.list_questions(session, None, limit=10)
        blocks = "\n\n".join(
            f"[题 {q.id}] 题型：{q.kind}｜难度：{q.difficulty}\n题干：{q.stem}" for q in pool
        )
        real = prompts.load("offline/propose_points.md")
        for label, prompt in (
            ("改前（真文件）", real.render(questions=blocks, count=len(pool))),
            ("改后（要求归并）", VARIANT.replace("{{questions}}", blocks)
                                      .replace("{{count}}", str(len(pool)))),
        ):
            proxy = BigBudget(llm, 16384)
            before = llm.usage_total["completion_tokens"]
            started = time.time()
            from app.llm import LLMError

            try:
                data, _ = proxy.chat_json([{"role": "user", "content": prompt}])
                points = (data or {}).get("points") or []
                covered = {qid for p in points for qid in (p.get("question_ids") or [])}
                names = [str(p.get("name")) for p in points]
                print(f"{label}：{len(points)} 个点 / {len(pool)} 道题、覆盖 {len(covered)} 道"
                      f"、完成 {llm.usage_total['completion_tokens'] - before} token、"
                      f"{time.time() - started:.0f}s")
                print(f"   点：{names}")
            except LLMError as e:
                print(f"{label}：失败 {type(e).__name__}: {str(e)[:120]}")
    finally:
        engine.dispose()
        llm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
