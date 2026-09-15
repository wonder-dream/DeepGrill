"""批大小标定：一次提多少道题才装得进 max_tokens（用完可删）。

背景：`PROPOSE_BATCH = 40` 在 `deepseek-flash` 上**每一批都失败**（推理吃光 8192，
或输出被截断）。演习用假 LLM 验不出这个 —— 假替身不管输入多大都回一小段固定 JSON，
**它能验形状，验不出预算**。所以只能拿真模型扫一遍。

用法：python tools/calibrate_propose_batch.py [5 10 20 40]
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


def main(argv: list[str]) -> int:
    from app.bank import repository as bank_repository
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.deps import get_llm
    from app.offline import knowledge_pipeline as kp

    sizes = [int(a) for a in argv[1:]] or [5, 10, 20]
    settings = Settings()
    engine = create_db_engine(settings.resolved_database_path())
    llm = get_llm(settings)
    print(f"{'批大小':>6} {'结果':>6} {'候选':>5} {'prompt':>8} {'完成':>8} {'推理':>8} {'耗时':>7}")
    try:
        with create_session_factory(engine)() as session:
            pool, _ = bank_repository.list_questions(session, None, limit=max(sizes))
        for size in sizes:
            questions = pool[:size]
            before = dict(llm.usage_total)
            started = time.time()
            result = kp.propose_points(questions, llm=llm)
            after = dict(llm.usage_total)
            delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
            ok = "失败" if result.llm_failed else "成功"
            print(
                f"{size:>6} {ok:>6} {len(result.candidates):>5} "
                f"{delta.get('prompt_tokens', 0):>8} {delta.get('completion_tokens', 0):>8} "
                f"{delta.get('reasoning_tokens', 0):>8} {time.time() - started:>6.1f}s"
            )
            if result.llm_failed:
                print(f"       ↳ {result.note}")
    finally:
        engine.dispose()
        llm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
