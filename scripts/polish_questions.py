"""题库清洗与自然化改写（一次性，可复跑）：LLM 分类 keep/rewrite/delete → 删除级联清理 + 改写题干。

跑法：
    uv run python scripts/polish_questions.py

阶段 1：逐题 LLM 判定类别（分类失败保守记 keep，不误删）
阶段 2：delete 类先导出备份（data/backup/polish_deleted.json）再级联删除（sessions→attempts/judgments→question）
阶段 3：rewrite 类 LLM 改写题干（保持语义，句式自然化，一问一题），校验非空/去重后更新
重复执行无副作用（重新分类+改写，已删题不存在）。
"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import load_config, secret_value
from app.db import commit, get_session, init_db
from app.llm.llm_client import LLMClient
from app.models import Question, Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("polish")

BACKUP_DIR = Path("data/backup")

CLASSIFY_PROMPT = """你是面试题库质检员。判断下面这道面试题属于哪一类，只输出 JSON：
{{"verdict": "keep" 或 "rewrite" 或 "delete", "reason": "一句话理由"}}

分类标准：
- delete：非技术面试题——面试者反问（如"贵公司主要做什么业务""你们团队规模"）、闲聊寒暄、面试流程问题（如"还有什么想问的""自我介绍"）、与考核无关的主观话题；手撕算法题（白板手写代码题，如"手写快排""实现一个 LRU 缓存"，本系统只收录口述问答/场景设计类）
- rewrite：题目本身是技术考点但表述不自然——口语化（"看你项目用过X，怎么做的"）、含糊不清（"有没有了解过…"）、一题多问（多个问号拼在一句）、过简（"如何做X？用什么？"）、主观观点题可改为技术向
- keep：表述清晰自然的技术题（"请解释/请描述/请设计/为什么…"句式）

题目：{stem}"""

REWRITE_PROMPT = """将下面的面试题改写得自然、专业：
1. 保持原技术语义与考点不变
2. 统一为"请解释/请描述/请设计/为什么…"等自然问句
3. 一句话一问，不拆题、不合并题
4. 去掉口语化表达（"看你项目…""有没有了解过…"等）
5. 只输出改写后的题干本身，不要输出任何其他文字

原题：{stem}"""


def main() -> None:
    init_db("sqlite:///data/interview.db")
    cfg = load_config(Path("config.yaml"))
    llm = LLMClient(
        cfg.llm.generate_model,
        cfg.llm.base_url,
        secret_value(cfg.llm.api_key_env),
    )

    with get_session() as session:
        questions = list(session.scalars(select(Question).order_by(Question.id)))
    logger.info("待处理 %d 题", len(questions))

    keeps: list[Question] = []
    rewrites: list[Question] = []
    deletes: list[Question] = []
    classify_failed = 0
    for i, q in enumerate(questions, 1):
        try:
            parsed = llm.complete(
                [{"role": "user", "content": CLASSIFY_PROMPT.format(stem=q.stem)}],
                json_schema={},
            )
            verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
        except Exception as e:
            logger.warning("第 %d 题（id=%s）分类失败：%s", i, q.id, str(e)[:120])
            classify_failed += 1
            keeps.append(q)  # 保守：失败按 keep，不误删
            continue
        if verdict == "delete":
            deletes.append(q)
        elif verdict == "rewrite":
            rewrites.append(q)
        else:
            keeps.append(q)
        if i % 20 == 0:
            logger.info(
                "分类进度 %d/%d（keep %d rewrite %d delete %d 失败 %d）",
                i, len(questions), len(keeps), len(rewrites), len(deletes), classify_failed,
            )
    logger.info("分类完成：keep %d / rewrite %d / delete %d / 失败 %d", len(keeps), len(rewrites), len(deletes), classify_failed)

    # --- 阶段 2：删除（先备份，再级联） ---
    if deletes:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"polish_deleted_{datetime.now():%Y%m%d_%H%M%S}.json"
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump([{"id": q.id, "stem": q.stem} for q in deletes], f, ensure_ascii=False, indent=1)
        logger.info("已备份 %d 题待删清单 -> %s", len(deletes), backup_path)
        with get_session() as session:
            for q in deletes:
                sessions = session.scalars(
                    select(Session).where(Session.question_id == q.id)
                ).all()
                for s in sessions:
                    session.delete(s)  # ORM 级联删 attempts/judgments
                row = session.get(Question, q.id)
                if row is not None:
                    session.delete(row)
            commit(session)
        logger.info("已删除 %d 题（含其会话/回答/判分记录）", len(deletes))

    # --- 阶段 3：改写 ---
    if rewrites:
        with get_session() as session:
            existing = {q.stem for q in session.scalars(select(Question))}
        ok = failed = skipped = 0
        for i, q in enumerate(rewrites, 1):
            try:
                new_stem = llm.complete(
                    [{"role": "user", "content": REWRITE_PROMPT.format(stem=q.stem)}],
                    json_schema={},
                )
                if not isinstance(new_stem, str):
                    raise ValueError(f"非字符串输出: {type(new_stem).__name__}")
                new_stem = new_stem.strip()
            except Exception as e:
                logger.warning("第 %d 题（id=%s）改写失败：%s", i, q.id, str(e)[:120])
                failed += 1
                continue
            if not new_stem or len(new_stem) < 6 or new_stem in existing:
                logger.warning("第 %d 题（id=%s）改写结果无效或重复，跳过", i, q.id)
                skipped += 1
                continue
            with get_session() as session:
                row = session.get(Question, q.id)
                if row is None:
                    continue
                row.stem = new_stem
                commit(session)
            existing.add(new_stem)
            ok += 1
            if i % 20 == 0:
                logger.info("改写进度 %d/%d（成功 %d 失败 %d 跳过 %d）", i, len(rewrites), ok, failed, skipped)
        logger.info("改写完成：成功 %d / 失败 %d / 跳过 %d", ok, failed, skipped)

    with get_session() as session:
        total = len(session.scalars(select(Question)).all())
    logger.info("完成：题库余 %d 题（原 %d，删 %d）", total, len(questions), len(deletes))


if __name__ == "__main__":
    main()
