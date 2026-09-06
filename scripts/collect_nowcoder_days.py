"""牛客批量回补收集（一次性/可复跑）：越过已存在条目继续往前采，直到时间边界（默认 10 天）。

跑法：
    uv run python scripts/collect_nowcoder_days.py            # 采 10 天内
    uv run python scripts/collect_nowcoder_days.py --days 30  # 采 30 天内
    uv run python scripts/collect_nowcoder_days.py --max-pages 50

区别于每日增量（nowcoder.collect 遇已存在即停）：批量模式跳过已存在条目，
按 createdAt 时间边界停止——首次可覆盖窗口内全部面经（含历史未采部分）。
"""
import argparse
import hashlib
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, secret_value
from app.crawler.nowcoder import NowcoderAPI
from app.db import commit, find_source_by_hash, get_session, init_db
from app.models import Source, SourceType
from app.pipeline.clean import clean_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("collect_nowcoder_days")

MAX_PAGES = 30


def import_entry(entry: dict) -> bool:
    """入库单条（已存在跳过）；返回是否新增。"""
    url_hash = hashlib.sha256(entry["url"].encode("utf-8")).hexdigest()
    with get_session() as session:
        if find_source_by_hash(session, url_hash):
            return False
    with get_session() as session:
        source = Source(
            type=SourceType.nowcoder,
            url=entry["url"],
            title=entry["title"],
            cleaned_text=clean_text(entry["content"]),
            source_hash=url_hash,
        )
        session.add(source)
        try:
            commit(session)
        except Exception:
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10, help="收集时间窗口（天）")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, help="最大拉页数")
    args = parser.parse_args()

    init_db("sqlite:///data/interview.db")
    cfg = load_config(Path("config.yaml"))
    cookie = secret_value(cfg.nowcoder.cookie_env)
    api = NowcoderAPI(cookie, cfg.nowcoder.request_interval, cfg.nowcoder.retries)

    cutoff = datetime.now(timezone.utc).timestamp() * 1000 - args.days * 24 * 3600 * 1000
    logger.info("时间边界：近 %d 天（cutoff=%d）", args.days, int(cutoff))

    new = skipped = stopped = 0
    for page in range(1, args.max_pages + 1):
        entries = api.get_list(page)
        if not entries:
            logger.info("第 %d 页无条目，结束", page)
            break
        page_new = 0
        for e in entries:
            created = e.get("created_at") or 0
            if created and created < cutoff:
                stopped += 1
                continue
            if import_entry(e):
                new += 1
                page_new += 1
            else:
                skipped += 1
        logger.info("第 %d 页：新增 %d / 已存在 %d / 超窗 %d", page, page_new, skipped, stopped)
        time.sleep(0.3)
    logger.info("完成：新增 %d 个源，已存在 %d，超时间窗 %d", new, skipped, stopped)


if __name__ == "__main__":
    main()
