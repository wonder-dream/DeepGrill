"""M8 题目生成：面经 → knowledge/design 题；简历 → project 深挖题。

仅生成并校验，不落库——由 M12 去重（M9）后统一入库，保证 D16 计数与去重语义正确。
"""
import logging

from ..difficulty import DIFFICULTY_SCALE_TEXT
from ..errors import GenerationError, LLMError, LLMJsonError
from ..models import Question, QuestionType, Source
from ..tags import MAX_TAGS, TAG_VOCABULARY, tag_vocab_text

logger = logging.getLogger(__name__)

GENERATE_PROMPT_V2 = """你是面试题生成器。给定一篇真实面试经验文本，提取其中的面试问题，生成开放问答题。

要求：
1. 只输出 JSON 数组，不要输出任何其他文字
2. 每项为对象：{{"type": "knowledge" 或 "design", "stem": 完整题干, "tags": [1-5 个标签], "difficulty": 1-5 的整数, "good_criteria": [2-4 条高分答案标准], "bad_criteria": [2-4 条常见扣分特征]}}
3. stem 必须是自然完整的面试问句（"请解释/请描述/请设计/为什么…"句式），禁止口语化、禁止一题多问
4. type 定义：knowledge = 明确的知识问答（如"讲一下 HashMap 底层原理"）；design = 场景设计题（如"设计一个短链接系统"）
5. good_criteria / bad_criteria 必须与本题强相关，供判分引用
6. 面试官寒暄、闲聊、追问"还有吗"、自我介绍、面试者反问（如"贵公司主要做什么业务""还有什么想问的"）等非面试官出题内容不要生成题目
7. 避免与已有题目重复：已有题目如下
{existing_stems}
8. tags 必须从以下词表中选择 1-5 个，不得使用词表外的词：
{vocab}
9. difficulty 按以下分级标准标注：
{difficulty_scale}

面经文本：
{content}"""

PROJECT_PROMPT_V2 = """你是面试题生成器。从候选人简历中提取项目经历，为每个项目生成一条项目深挖开放问答题。

要求：
1. 只输出 JSON 数组，最多 {limit} 项，不要输出任何其他文字
2. 每项为对象：{{"type": "project", "stem": 基于该项目真实经历的深挖题（如"讲一下 XX 项目的技术难点和取舍"）, "tags": [1-5 个标签], "difficulty": 1-5 的整数, "good_criteria": [2-4 条高分答案标准], "bad_criteria": [2-4 条常见扣分特征]}}
3. stem 必须是自然完整的面试问句（"请解释/请描述/请设计/为什么…"句式），禁止口语化、禁止一题多问
4. 题目必须基于简历中的真实项目经历，可被追问验证；不要编造简历中没有的项目
5. tags 必须从以下词表中选择 1-5 个，不得使用词表外的词：
{vocab}
6. difficulty 按以下分级标准标注：
{difficulty_scale}

简历文本：
{content}"""

MAX_INPUT_CHARS = 12_000
MAX_STEM_LEN = 500
DEFAULT_GOOD_CRITERIA = ["完整、准确、结构清晰"]
DEFAULT_BAD_CRITERIA = ["答非所问"]


def generate_from_source(
    source: Source, existing_questions: list[Question], llm
) -> list[Question]:
    """面经源 → knowledge/design 题；existing 供 prompt 引用防重复。

    按"一面/二面"轮次分批生成（复用 M4 split_rounds）：每轮单独调用 LLM，
    单次输出规模可控，避免长素材一次生成超长 JSON 被 max_tokens 截断
    （docs/分批生成方案.md）；无轮次结构（1 个 round）行为不变。
    """
    from .clean import split_rounds

    rounds = split_rounds(source.cleaned_text)
    questions: list[Question] = []
    for round_text in rounds:
        messages = [
            {
                "role": "user",
                "content": GENERATE_PROMPT_V2.format(
                    content=_truncate(round_text["content"], MAX_INPUT_CHARS),
                    existing_stems=_existing_stems(existing_questions),
                    vocab=tag_vocab_text(),
                    difficulty_scale=DIFFICULTY_SCALE_TEXT,
                ),
            }
        ]
        parsed = _complete_json(llm, messages)
        questions.extend(
            _validate_questions(
                parsed, source.id, (QuestionType.knowledge, QuestionType.design)
            )
        )
    return questions


def generate_project_questions(
    resume_source: Source, limit: int, llm
) -> list[Question]:
    """简历源 → project 深挖题，受 limit 节流。"""
    messages = [
        {
            "role": "user",
            "content": PROJECT_PROMPT_V2.format(
                content=_truncate(resume_source.cleaned_text, MAX_INPUT_CHARS),
                limit=limit,
                vocab=tag_vocab_text(),
                difficulty_scale=DIFFICULTY_SCALE_TEXT,
            ),
        }
    ]
    parsed = _complete_json(llm, messages)
    return _validate_questions(
        parsed[:limit], resume_source.id, (QuestionType.project,)
    )


def _complete_json(llm, messages: list[dict]) -> list:
    try:
        parsed = llm.complete(messages, json_schema={})
    except (LLMError, LLMJsonError) as e:
        raise GenerationError(f"question generation llm failure: {e}") from e
    if not isinstance(parsed, list):
        raise GenerationError(
            f"generation output must be a list, got {type(parsed).__name__}"
        )
    return parsed


def _validate_questions(
    parsed: list, source_id: int, allowed_types: tuple[QuestionType, ...]
) -> list[Question]:
    """结构校验：非法 type/空题干丢弃；criteria 缺省补默认；difficulty 钳制 1-5。"""
    questions = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            qtype = QuestionType(item["type"])
        except (KeyError, ValueError):
            continue
        if qtype not in allowed_types:
            continue
        raw_stem = item.get("stem")
        if not isinstance(raw_stem, str) or not raw_stem.strip():
            continue
        stem = raw_stem.strip()
        raw_tags = item.get("tags", [])
        if not isinstance(raw_tags, list):
            raw_tags = []
        tags = [t for t in raw_tags if isinstance(t, str) and t in TAG_VOCABULARY][
            :MAX_TAGS
        ]
        questions.append(
            Question(
                source_id=source_id,
                type=qtype,
                stem=_truncate(stem, MAX_STEM_LEN),
                tags=tags,
                difficulty=_clamp_difficulty(item.get("difficulty")),
                good_criteria=_criteria_list(
                    item.get("good_criteria"), DEFAULT_GOOD_CRITERIA
                ),
                bad_criteria=_criteria_list(
                    item.get("bad_criteria"), DEFAULT_BAD_CRITERIA
                ),
            )
        )
    return questions


def _criteria_list(value, default: list[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        return list(default)
    return [v for v in value if isinstance(v, str)] or list(default)


def _clamp_difficulty(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 1


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _existing_stems(questions: list[Question]) -> str:
    stems = [f"- {q.stem}" for q in questions[:50]]
    return "\n".join(stems) if stems else "（无）"
