"""一次性清理积压源：为所有未生成题目的源批量生成（绕开每日 36 上限）。

跑法：
    uv run python scripts/bootstrap_generate.py

复用 run_daily 全套逻辑（生成→去重→入库→选题，含容错/任务日志/互斥锁），
仅把 max_new_questions 临时放大到 10000（全部消化），sources 传空列表不触发采集。
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.config import load_config, secret_value
from app.db import get_session, init_db
from app.embed import Embedder
from app.llm.llm_client import LLMClient
from app.pipeline.daily import run_daily

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bootstrap_generate")


def main() -> None:
    init_db("sqlite:///data/interview.db")
    cfg = load_config(Path("config.yaml"))
    cfg = cfg.model_copy(
        update={
            "daily": cfg.daily.model_copy(update={"max_new_questions": 10000})
        }
    )  # 临时放大：一次消化全部积压

    llm = LLMClient(
        cfg.llm.generate_model,
        cfg.llm.base_url,
        secret_value(cfg.llm.api_key_env),
    )
    embedder = Embedder()

    report = run_daily(cfg, [], llm, embedder)
    logger.info(
        "入库 %d 题，今日待做 %d，错误 %d 条",
        report["new_questions"],
        len(report["today_questions"]),
        len(report["errors"]),
    )
    for err in report["errors"][:10]:
        logger.warning("错误：%s", err)

    with get_session() as session:
        leftover = session.execute(
            text(
                "SELECT COUNT(*) FROM sources "
                "WHERE id NOT IN (SELECT DISTINCT source_id FROM questions)"
            )
        ).scalar()
    logger.info("剩余无题源：%d", leftover)


if __name__ == "__main__":
    main()
