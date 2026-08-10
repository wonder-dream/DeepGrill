"""空标签/全库标签重打（一次性数据修复，可复跑）：LLM 从词表按新词表补标。

跑法：
    uv run python scripts/retag_empty_tags.py            # 只补空标签题（词表缺主题导致的 29 题等）
    uv run python scripts/retag_empty_tags.py --all      # 全库重打（词表扩增后纠错，含手动编辑过的题，约 100+ 次调用）

按 id 回填标签；单批失败跳过（日志记录），重复执行无变更。
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import load_config, secret_value
from app.db import commit, get_session, init_db
from app.llm.llm_client import LLMClient
from app.models import Question
from app.tags import MAX_TAGS, TAG_VOCABULARY, tag_vocab_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("retag_empty_tags")

BATCH = 20

RETAG_PROMPT = """为以下题目各选 1-{max_tags} 个最相关的标签，必须从以下词表中选，不得使用词表外的词：
{vocab}

题目列表：
{numbered}

只输出 JSON，不要其他文字：{{"items": [{{"id": 题目id, "tags": [标签列表]}}]}}"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="全库重打（默认只补空标签题）")
    args = parser.parse_args()

    init_db("sqlite:///data/interview.db")
    cfg = load_config(Path("config.yaml"))
    llm = LLMClient(
        cfg.llm.generate_model,
        cfg.llm.base_url,
        secret_value(cfg.llm.api_key_env),
    )

    with get_session() as session:
        questions = list(session.scalars(select(Question)))
    targets = questions if args.all else [q for q in questions if not q.tags]
    logger.info("待打标签 %d 题（%s）", len(targets), "全库" if args.all else "仅空标签")

    ok = failed = 0
    for i in range(0, len(targets), BATCH):
        batch = targets[i : i + BATCH]
        numbered = "\n".join(f"{q.id}. {q.stem}" for q in batch)
        prompt = RETAG_PROMPT.format(
            max_tags=MAX_TAGS, vocab=tag_vocab_text(), numbered=numbered
        )
        try:
            parsed = llm.complete([{"role": "user", "content": prompt}], json_schema={})
            items = parsed.get("items", []) if isinstance(parsed, dict) else []
            by_id = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                qid = item.get("id")
                raw = item.get("tags", [])
                if isinstance(qid, int) and isinstance(raw, list):
                    by_id[qid] = [
                        t for t in raw if isinstance(t, str) and t in TAG_VOCABULARY
                    ][:MAX_TAGS]
            with get_session() as session:
                for q in batch:
                    tags = by_id.get(q.id)
                    if not tags:
                        continue
                    row = session.get(Question, q.id)
                    if row is None:
                        continue
                    row.tags = tags
                    ok += 1
                commit(session)
        except Exception as e:
            logger.warning("批次 %d 失败：%s", i // BATCH + 1, str(e)[:120])
            failed += len(batch)
        logger.info("进度 %d/%d", min(i + BATCH, len(targets)), len(targets))

    logger.info("完成：成功 %d，失败/跳过 %d", ok, failed)


if __name__ == "__main__":
    main()
