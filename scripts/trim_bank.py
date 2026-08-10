"""题库裁剪（一次性，可复跑幂等）：2293 → 100 题均衡样本 + 保留题标签按新词表重打。

跑法：
    uv run python scripts/trim_bank.py

策略：
- 新词表 12 分类中「有货」的 10 类（Go/前端当前 0 题自动跳过），每类抽 10 题
- 分类内按难度 1-5 轮转取最新题（id 倒序）；难度取尽则轮转跳过，直到凑满或该类取尽
- 其余题目级联删除（judgments → attempts → sessions → user_picks → questions，FK 安全）
- 保留题中标签含新词表外词的 → LLM 按新词表重打（每批 ≤20，可复跑）
- 防重复执行：题库数 < 500 时拒绝（已裁剪过）

执行前请先备份 data/interview.db（data/backup/）。
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text

from app.config import load_config, secret_value
from app.db import commit, get_session, init_db
from app.llm.llm_client import LLMClient
from app.models import Question
from app.tags import MAX_TAGS, TAG_CATEGORIES, TAG_VOCABULARY, tag_vocab_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("trim_bank")

TARGET = 100
CATEGORY_QUOTA = 10
BATCH = 20

RETAG_PROMPT = """为以下题目各选 1-{max_tags} 个最相关的标签，必须从以下词表中选，不得使用词表外的词：
{vocab}

题目列表：
{numbered}

只输出 JSON，不要其他文字：{{"items": [{{"id": 题目id, "tags": [标签列表]}}]}}"""


def _in_bind(tags: list[str]) -> tuple[str, dict]:
    """展开 IN 占位符（SQLite 不支持元组绑定）。"""
    ph = ",".join(f":t{i}" for i in range(len(tags)))
    return ph, {f"t{i}": t for i, t in enumerate(tags)}


def pick_category_questions(session, cat_tags: list[str], quota: int) -> list[int]:
    """分类内难度轮转取最新题：难度 1-5 循环，每轮取该难度 id 最大的一题。"""
    picked: list[int] = []
    difficulty = 1
    rounds_without_new = 0
    while len(picked) < quota:
        ph, binds = _in_bind(cat_tags)
        p_ph = ",".join(f":p{i}" for i in range(len(picked))) or ":p0"
        p_binds = {f"p{i}": v for i, v in enumerate(picked)}
        if not picked:
            p_binds["p0"] = 0
        rows = session.execute(
            text(
                f"SELECT DISTINCT q.id FROM questions q, json_each(q.tags) "
                f"WHERE value IN ({ph}) AND q.difficulty = :d AND q.id NOT IN ({p_ph}) "
                f"ORDER BY q.id DESC LIMIT 1"
            ),
            {**binds, "d": difficulty, **p_binds},
        ).all()
        if rows:
            picked.append(rows[0][0])
            rounds_without_new = 0
        else:
            rounds_without_new += 1
            if rounds_without_new >= 5:  # 5 个难度都取尽 → 分类无更多题
                break
        difficulty = difficulty % 5 + 1
    return picked[:quota]


def main() -> None:
    init_db("sqlite:///data/interview.db")
    with get_session() as session:
        total = len(session.scalars(select(Question.id)).all())
        if total < 500:
            logger.info("题库已裁剪（当前 %d 题），跳过（防重复执行）", total)
            return

    # 1. 按分类配额抽题
    picked_ids: list[int] = []
    with get_session() as session:
        for name, cat_tags in TAG_CATEGORIES:
            ph, binds = _in_bind(list(cat_tags))
            n = session.execute(
                text(
                    f"SELECT COUNT(DISTINCT q.id) FROM questions q, json_each(q.tags) "
                    f"WHERE value IN ({ph})"
                ),
                binds,
            ).scalar()
            if n == 0:
                logger.info("分类 %s 无题，跳过", name)
                continue
            qids = pick_category_questions(session, list(cat_tags), CATEGORY_QUOTA)
            picked_ids.extend(qids)
            logger.info("分类 %s：抽 %d 题（货量 %d）", name, len(qids), n)
    picked_set = set(picked_ids)
    if len(picked_set) < 100:
        # 补足：从通用/LLM 核心等大类按 id 倒序补
        with get_session() as session:
            extra = session.scalars(
                select(Question.id)
                .where(Question.id.not_in(picked_set))
                .order_by(Question.id.desc())
                .limit(TARGET - len(picked_set))
            ).all()
        picked_set.update(extra)
    picked_ids = sorted(picked_set)
    logger.info("保留 %d 题", len(picked_ids))

    # 2. 级联删除其余题
    keep_ph, keep_binds = _in_bind([str(i) for i in picked_ids])
    with get_session() as session:
        session.execute(
            text(
                "DELETE FROM judgments WHERE session_id IN "
                f"(SELECT id FROM sessions WHERE question_id NOT IN ({keep_ph}))"
            ),
            keep_binds,
        )
        session.execute(
            text(
                "DELETE FROM attempts WHERE session_id IN "
                f"(SELECT id FROM sessions WHERE question_id NOT IN ({keep_ph}))"
            ),
            keep_binds,
        )
        session.execute(
            text(f"DELETE FROM sessions WHERE question_id NOT IN ({keep_ph})"),
            keep_binds,
        )
        session.execute(
            text(f"DELETE FROM user_picks WHERE question_id NOT IN ({keep_ph})"),
            keep_binds,
        )
        session.execute(
            text(f"DELETE FROM questions WHERE id NOT IN ({keep_ph})"),
            keep_binds,
        )
        commit(session)

    # 3. 保留题中词表外标签 → LLM 重打
    cfg = load_config(Path("config.yaml"))
    llm = LLMClient(
        cfg.llm.generate_model,
        cfg.llm.base_url,
        secret_value(cfg.llm.api_key_env),
    )
    with get_session() as session:
        kept = list(
            session.scalars(
                select(Question)
                .where(Question.id.in_(picked_ids))
                .order_by(Question.id)
            )
        )
    targets = [
        q for q in kept if any(t not in TAG_VOCABULARY for t in (q.tags or []))
    ]
    logger.info("需重打标签 %d 题（词表外标签）", len(targets))
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
    logger.info("重打完成：成功 %d，失败/跳过 %d", ok, failed)

    # 4. 统计
    with get_session() as session:
        total = len(session.scalars(select(Question.id)).all())
        rows = session.execute(
            text(
                "SELECT type, difficulty, COUNT(*) FROM questions "
                "GROUP BY type, difficulty ORDER BY type, difficulty"
            )
        ).all()
    logger.info("裁剪后题库：%d 题", total)
    for r in rows:
        logger.info("  %s 难度%s: %d", r[0], r[1], r[2])


if __name__ == "__main__":
    main()
