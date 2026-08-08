"""旧题标签迁移（一次性数据修复，可复跑）：LLM 从词表重打词表外标签。

跑法：
    uv run python scripts/migrate_tags.py

只处理含词表外标签的题；单题失败跳过（日志记录），重复执行无变更。
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.config import load_config, secret_value
from app.db import commit, get_session, init_db
from app.llm.llm_client import LLMClient
from app.models import Question
from app.tags import MAX_TAGS, TAG_VOCABULARY, tag_vocab_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_tags")

MIGRATE_PROMPT = """为题目重新打标签：必须从以下词表中选 1-{max_tags} 个最相关的标签，不得使用词表外的词：
{vocab}

题目：{stem}

只输出 JSON，不要其他文字：{{"tags": [标签列表]}}"""


def out_of_vocab(tags: list[str]) -> bool:
    return any(t not in TAG_VOCABULARY for t in tags)


def main() -> None:
    init_db("sqlite:///data/interview.db")
    cfg = load_config(Path("config.yaml"))
    llm = LLMClient(
        cfg.llm.generate_model,
        cfg.llm.base_url,
        secret_value(cfg.llm.api_key_env),
    )

    from sqlalchemy import select

    with get_session() as session:
        questions = list(session.scalars(select(Question)))
    targets = []
    for q in questions:
        if not q.tags:
            continue
        if out_of_vocab(q.tags):
            targets.append(q)
    logger.info("待迁移 %d 题", len(targets))

    ok = failed = 0
    for i, q in enumerate(targets, 1):
        prompt = MIGRATE_PROMPT.format(
            stem=q.stem, vocab=tag_vocab_text(), max_tags=MAX_TAGS
        )
        try:
            parsed = llm.complete([{"role": "user", "content": prompt}], json_schema={})
            raw = parsed.get("tags", []) if isinstance(parsed, dict) else []
            new_tags = [t for t in raw if isinstance(t, str) and t in TAG_VOCABULARY][
                :MAX_TAGS
            ]
            if not new_tags:
                logger.warning("第 %d 题（id=%s）重打结果为空，跳过", i, q.id)
                failed += 1
                continue
            with get_session() as session:
                row = session.get(Question, q.id)
                row.tags = new_tags
                commit(session)
            ok += 1
        except Exception as e:
            logger.warning("第 %d 题（id=%s）失败：%s", i, q.id, str(e)[:120])
            failed += 1
        if i % 20 == 0:
            logger.info("进度 %d/%d（成功 %d 失败 %d）", i, len(targets), ok, failed)

    with get_session() as session:
        questions = list(session.scalars(select(Question)))
    leftover = [q for q in questions if q.tags and out_of_vocab(q.tags)]
    logger.info("完成：成功 %d，失败/跳过 %d，词表外残留 %d 题", ok, failed, len(leftover))


if __name__ == "__main__":
    main()
