"""入库前题目质量筛选（RAG 需求补充）：LLM 批量判定 keep/rewrite/delete → 仅 keep 入库。

- 复用 polish_questions 的分类标准，改为生成流程入库前调用（批量 20/批）
- delete（非技术题/闲聊/反问）、rewrite（口语化/含糊/一题多问）→ 丢弃（宁缺毋滥，题库源充足）
- 批次失败/缺失 → 保守保留（不误删，与 polish 一致）
"""
import logging

from ..errors import LLMError, LLMJsonError

logger = logging.getLogger(__name__)

QUALITY_BATCH = 20

QUALITY_FILTER_PROMPT = """你是面试题库质检员。逐条判断下面每道面试题属于哪一类：
- delete：非技术面试题——面试者反问、闲聊寒暄、面试流程问题（"还有什么想问的""自我介绍"）、与考核无关的主观话题；手撕算法题（白板手写代码题，如"手写快排""实现一个 LRU 缓存"，本系统只收录口述问答/场景设计类）
- rewrite：技术考点但表述不自然——口语化（"看你项目用过X，怎么做的"）、含糊不清（"有没有了解过…"）、一题多问、过简、主观观点题
- keep：表述清晰自然的技术题（"请解释/请描述/请设计/为什么…"句式）

题目列表：
{numbered}

只输出 JSON，不要其他文字：{{"items": [{{"index": 序号, "verdict": "keep" 或 "rewrite" 或 "delete"}}]}}"""


def filter_quality(questions, llm) -> tuple[list, dict]:
    """入库前质量筛选：返回 (保留题, 统计)。rewrite/delete 丢弃；失败保守保留。"""
    if not questions:
        return questions, {"keep": 0, "discard": 0, "failed": 0}
    kept: list = []
    stats = {"keep": 0, "discard": 0, "failed": 0}
    for i in range(0, len(questions), QUALITY_BATCH):
        batch = questions[i : i + QUALITY_BATCH]
        numbered = "\n".join(f"{j + 1}. {q.stem[:200]}" for j, q in enumerate(batch))
        try:
            parsed = llm.complete(
                [{"role": "user", "content": QUALITY_FILTER_PROMPT.format(numbered=numbered)}],
                json_schema={},
            )
            items = parsed.get("items", []) if isinstance(parsed, dict) else []
            verdicts: dict[int, str] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                idx = item.get("index")
                v = item.get("verdict")
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    continue
                if isinstance(v, str) and v in ("keep", "rewrite", "delete"):
                    verdicts[idx] = v
            for j, q in enumerate(batch):
                v = verdicts.get(j + 1)
                if v == "keep":
                    kept.append(q)
                    stats["keep"] += 1
                elif v in ("rewrite", "delete"):
                    stats["discard"] += 1  # 质量不佳：丢弃（源充足，宁缺毋滥）
                else:
                    kept.append(q)  # 缺失判定：保守保留
                    stats["failed"] += 1
        except (LLMError, LLMJsonError, Exception) as e:
            logger.warning("quality filter batch failed (保守保留 %d 题): %s", len(batch), str(e)[:100])
            kept.extend(batch)
            stats["failed"] += len(batch)
    logger.info("质量筛选：keep %d / 丢弃 %d / 失败保守保留 %d", stats["keep"], stats["discard"], stats["failed"])
    return kept, stats
