"""M10 判分：四维评分 + 总分合成（D17）+ 评语/参考答案/薄弱点标签。

prompt 版本化（PROMPT_VERSION 变更需同步更新测试与 fixture，防漂移）。
判分失败重试 1 次后返回 status=failed 的 Judgment（不抛错中断流程），由调用方持久化。
"""
import logging

from ..difficulty import DIFFICULTY_NAMES
from ..errors import JudgeError, LLMError, LLMJsonError
from ..models import Judgment, Question
from ..tags import MAX_WEAK_TAGS, TAG_VOCABULARY, tag_vocab_text

logger = logging.getLogger(__name__)

PROMPT_VERSION = "judge_v7"  # v7：注入知识库标准参照（RAG，judge_v6 增加）

DIMS = ("accuracy", "completeness", "clarity", "depth")
WEIGHTS = (0.3, 0.3, 0.2, 0.2)
MAX_TRANSCRIPT_CHARS = 50_000

STATUS_OK = "ok"
STATUS_FAILED = "failed"

JUDGE_SYSTEM_V4 = """你是资深面试官，按随题判分标准对候选人回答做四维评分。

题型：{qtype}
判分侧重（按题型差异化）：
- knowledge：准确性优先，概念准确、要点完整
- design：完整性与深度优先，覆盖场景、方案取舍、容量权衡
- project：真实性与反思深度优先，基于真实经历、讲清难点与取舍

题目：{stem}
标签：{tags}
难度：{difficulty}/5（{difficulty_name}）
判分期望：按难度校准——1-2 级答全要点即可高分；3 级须展示权衡；4-5 级必须在 depth/completeness 体现方案选型与实战取舍，否则按期望扣分
{depth_note}
{quality_trace_section}

 只输出 JSON，不要其他文字：
{{"scores": {{"accuracy": 0-100, "completeness": 0-100, "clarity": 0-100, "depth": 0-100}}, "review": "评语", "reference_answer": "参考答案", "weak_tags": ["0-3 个薄弱主题标签"]}}

weak_tags 必须从以下词表中选择 0-3 个（薄弱主题，与题目标签同词表，便于同类题复习），不得使用词表外的词：
{vocab}

{reference_section}

{knowledge_section}"""

JUDGE_USER_V1 = """高分标准（good_criteria）：
{good}

扣分特征（bad_criteria）：
{bad}

完整对话记录：
{transcript}"""


def judge(
    question: Question,
    transcript: list[dict],
    model: str,
    llm,
    *,
    session_id: int | None = None,
    reference: str | None = None,
    max_level: int | None = None,
    quality_trace: list[str] | None = None,
    knowledge: str | None = None,
) -> Judgment:
    """对完整对话判分；LLM 失败重试 1 次，仍失败返回 status=failed 的 Judgment。

    reference：库内同类高分回答片段（可选），非空时注入 prompt 作标准参照。
    knowledge：知识库检索片段（RAG，可选），非空时注入作知识标准参照（judge_v7）。
    max_level：深挖追问探到的最大层级（L1-L5，可选），depth 维度按此校准。
    quality_trace：逐轮回答质量序列（correct/partial/wrong/unsure，可选），depth 校准依据。
    """
    messages = _build_messages(question, transcript, reference, max_level, quality_trace, knowledge)
    try:
        parsed = _call_llm(llm, messages)
        judgment = _parse_judgment(parsed, model, reference)
    except (JudgeError, LLMError, LLMJsonError) as e:
        logger.warning("judge failed for question %s: %s", question.id, e)
        judgment = _failed_judgment(model, str(e))
    if session_id is not None:
        judgment.session_id = session_id
    return judgment


def compute_total(scores: dict) -> int:
    """总分合成（D17）：accuracy×0.3 + completeness×0.3 + clarity×0.2 + depth×0.2。"""
    return int(
        round(sum(w * _to_score(scores.get(dim)) for dim, w in zip(DIMS, WEIGHTS)))
    )


def _call_llm(llm, messages: list[dict]):
    try:
        return llm.complete(messages, json_schema={})
    except (LLMError, LLMJsonError):
        try:
            return llm.complete(messages, json_schema={})
        except (LLMError, LLMJsonError) as e:
            raise JudgeError(f"judge llm failed after retry: {e}") from e


def _parse_judgment(parsed, model: str, reference: str | None = None) -> Judgment:
    if not isinstance(parsed, dict):
        raise JudgeError(
            f"judge output must be an object, got {type(parsed).__name__}"
        )
    raw_scores = parsed.get("scores")
    scores = {
        dim: _to_score(raw_scores.get(dim)) if isinstance(raw_scores, dict) else 0
        for dim in DIMS
    }
    scores["status"] = STATUS_OK
    if reference:
        scores["reference_used"] = True  # 参考检索可见性（Phase 2 §9.3，沿用 scores JSON 存状态先例）
    weak_tags = parsed.get("weak_tags", [])
    if not isinstance(weak_tags, list):
        weak_tags = []
    weak_tags = _normalize_weak_tags(weak_tags)
    return Judgment(
        scores=scores,
        total_score=compute_total(scores),
        review=_as_str(parsed.get("review")),
        reference_answer=_as_str(parsed.get("reference_answer")),
        weak_tags=weak_tags,
        model=model,
    )


def _failed_judgment(model: str, error: str) -> Judgment:
    return Judgment(
        scores={"status": STATUS_FAILED},
        review=f"判分失败：{error}",
        model=model,
    )


def _normalize_weak_tags(weak_tags: list) -> list[str]:
    """薄弱标签清洗：词表内保留；词表外与某词表词有包含关系则映射到该词（如「Java 并发」→「Java」），
    否则丢弃（能力词如「深度不足」无映射）；去重并限 MAX_WEAK_TAGS 个。"""
    result: list[str] = []
    for tag in weak_tags:
        if not isinstance(tag, str) or not tag.strip():
            continue
        mapped = tag if tag in TAG_VOCABULARY else _nearest_vocab_tag(tag)
        if mapped and mapped not in result:
            result.append(mapped)
    return result[:MAX_WEAK_TAGS]


def _nearest_vocab_tag(tag: str) -> str | None:
    """词表外标签 → 与词表词有包含关系（互为子串）时映射，否则 None。"""
    for vocab in TAG_VOCABULARY:
        if tag in vocab or vocab in tag:
            return vocab
    return None


def _to_score(value) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _build_messages(
    question: Question,
    transcript: list[dict],
    reference: str | None = None,
    max_level: int | None = None,
    quality_trace: list[str] | None = None,
    knowledge: str | None = None,
) -> list[dict]:
    tags = ", ".join(getattr(question, "_tags", []) or []) or "（无）"
    if reference:
        reference_section = (
            "以下为库内同类题的高分回答（仅作标准参照，评估回答质量时参考其要点，不要照抄）：\n"
            f"{reference}"
        )
    else:
        reference_section = ""
    if knowledge:
        knowledge_section = (
            "以下为相关知识资料（权威知识点，供核对答案正确性与参考答案撰写，不要照抄原文）：\n"
            f"{knowledge}"
        )
    else:
        knowledge_section = ""
    if max_level:
        depth_note = (
            f"追问深度：本轮深挖追问探到 L{max_level}（共 5 层：概念→原理→权衡→边界→横向）。\n"
            "depth 维度按此校准：能答到高层级 = 高分；被追问到低层级就答不出 = 低分。"
        )
    else:
        depth_note = ""
    if quality_trace:
        quality_trace_section = (
            "逐轮回答质量轨迹（按轮次先后）：" + " → ".join(quality_trace) + "\n"
            "depth 校准依据：轨迹多 correct/partial 且逐步加深 = 高分；多 wrong/unsure 或一路卡壳 = 低分。"
        )
    else:
        quality_trace_section = ""
    system = JUDGE_SYSTEM_V4.format(
        qtype=question.type.value,
        stem=question.stem,
        tags=tags,
        difficulty=question.difficulty,
        difficulty_name=DIFFICULTY_NAMES.get(question.difficulty, "未知"),
        depth_note=depth_note,
        quality_trace_section=quality_trace_section,
        vocab=tag_vocab_text(),
        reference_section=reference_section,
        knowledge_section=knowledge_section,
    )
    user = JUDGE_USER_V1.format(
        good="\n".join(f"- {c}" for c in question.good_criteria),
        bad="\n".join(f"- {c}" for c in question.bad_criteria),
        transcript=_format_transcript(transcript),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _format_transcript(transcript: list[dict]) -> str:
    lines = []
    for turn in transcript:
        if not isinstance(turn, dict):
            continue
        lines.append(f"{turn.get('role', '?')}: {turn.get('content', '')}")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS] + "..."
    return text
