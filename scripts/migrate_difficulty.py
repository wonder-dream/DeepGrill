"""存量题目难度迁移（一次性数据修复，可复跑）：LLM 按 1-5 分级标准重新标注全量题目难度。

跑法：
    uv run python scripts/migrate_difficulty.py

原 1-3 档无法机械映射到 1-5（旧的 2 可能是新的 2/3/4），必须逐题 LLM 重标。
单题失败跳过（日志记录，保留原值）；重复执行只是重新标注，无副作用。
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text

from app.config import load_config, secret_value
from app.db import commit, get_session, init_db
from app.difficulty import DIFFICULTY_SCALE_TEXT
from app.llm.llm_client import LLMClient
from app.models import Question

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_difficulty")

MIGRATE_PROMPT = """按以下分级标准为题目标注难度（1-5 的整数）：

{difficulty_scale}

题目：{stem}
标签：{tags}

只输出 JSON，不要其他文字：{{"difficulty": 1-5 的整数}}"""


def clamp_difficulty(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return None


def main() -> None:
    init_db("sqlite:///data/interview.db")
    cfg = load_config(Path("config.yaml"))
    llm = LLMClient(
        cfg.llm.generate_model,
        cfg.llm.base_url,
        secret_value(cfg.llm.api_key_env),
    )

    with get_session() as session:
        questions = list(session.scalars(select(Question)))
    logger.info("待重标 %d 题", len(questions))

    ok = failed = skipped = 0
    for i, q in enumerate(questions, 1):
        prompt = MIGRATE_PROMPT.format(
            difficulty_scale=DIFFICULTY_SCALE_TEXT,
            stem=q.stem,
            tags="、".join(q.tags or []) or "无",
        )
        try:
            parsed = llm.complete([{"role": "user", "content": prompt}], json_schema={})
            raw = parsed.get("difficulty", None) if isinstance(parsed, dict) else None
            new_difficulty = clamp_difficulty(raw)
            if new_difficulty is None:
                logger.warning("第 %d 题（id=%s）难度非法 %r，跳过", i, q.id, raw)
                skipped += 1
                continue
            with get_session() as session:
                row = session.get(Question, q.id)
                row.difficulty = new_difficulty
                commit(session)
            ok += 1
        except Exception as e:
            logger.warning("第 %d 题（id=%s）失败：%s", i, q.id, str(e)[:120])
            failed += 1
        if i % 20 == 0:
            logger.info("进度 %d/%d（成功 %d 失败 %d 跳过 %d）", i, len(questions), ok, failed, skipped)

    with get_session() as session:
        rows = session.execute(
            text("SELECT difficulty, COUNT(*) FROM questions GROUP BY difficulty ORDER BY difficulty")
        ).all()
    dist = {r[0]: r[1] for r in rows}
    logger.info("完成：成功 %d，失败 %d，跳过 %d；分布 %s", ok, failed, skipped, dist)


if __name__ == "__main__":
    main()
