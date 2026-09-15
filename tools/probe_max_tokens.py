"""探 `max_tokens` 的上限（用完可删）。

要回答两个不同的问题，分开测：
  ① **API 收不收**这么大的值 —— 用一个极小的 prompt，只让模型回一句
  ② 收的话，**40 道题的提候选装不装得下** —— 用真 prompt + 真题跑一次

`propose_points()` 不接受 max_tokens，所以这里用一个薄代理把参数注进去
（管道将来真要放开预算，也就是同一处改动）。

用法：python tools/probe_max_tokens.py
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


class BigBudget:
    """把 `max_tokens` 抬高的代理 —— 管道将来放开预算就是这一处。"""

    def __init__(self, inner, budget: int) -> None:
        self._inner = inner
        self._budget = budget
        self.usage_total = inner.usage_total

    def chat_json(self, messages, **kwargs):
        kwargs.setdefault("max_tokens", self._budget)
        return self._inner.chat_json(messages, **kwargs)

    def chat(self, messages, **kwargs):
        kwargs.setdefault("max_tokens", self._budget)
        return self._inner.chat(messages, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def main() -> int:
    from app.bank import repository as bank_repository
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.deps import get_llm
    from app.offline import knowledge_pipeline as kp

    settings = Settings()
    llm = get_llm(settings)
    engine = create_db_engine(settings.resolved_database_path())
    try:
        # ① API 收不收 16384
        for budget in (16384, 32768):
            before = dict(llm.usage_total)
            try:
                reply = llm.chat([{"role": "user", "content": "回一个字：好"}], max_tokens=budget)
                used = llm.usage_total["completion_tokens"] - before["completion_tokens"]
                print(f"① max_tokens={budget:<6} 接受 ✅（回「{reply.text[:10]}」，用了 {used} token）")
            except Exception as e:  # noqa: BLE001 —— 探针的意义就是把失败打出来
                print(f"① max_tokens={budget:<6} 拒绝 ❌ {type(e).__name__}: {str(e)[:120]}")
                break

        # ② 40 道题 + 那个预算，装得下吗
        with create_session_factory(engine)() as session:
            pool, _ = bank_repository.list_questions(session, None, limit=40)
        for size in (20, 40):
            proxy = BigBudget(llm, 16384)
            before = dict(llm.usage_total)
            started = time.time()
            result = kp.propose_points(pool[:size], llm=proxy)
            delta = llm.usage_total["completion_tokens"] - before["completion_tokens"]
            reasoning = llm.usage_total["reasoning_tokens"] - before["reasoning_tokens"]
            state = "失败" if result.llm_failed else "成功"
            print(f"② {size:>2} 道题 budget=16384 → {state}：候选 {len(result.candidates)} 条、"
                  f"完成 {delta}（推理 {reasoning}）、{time.time() - started:.0f}s"
                  f"{'  ↳ ' + result.note if result.llm_failed else ''}")
    finally:
        engine.dispose()
        llm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
