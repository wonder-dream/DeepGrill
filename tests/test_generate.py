import pytest

from app.errors import GenerationError, LLMError, LLMJsonError
from app.models import Question, QuestionType, Source, SourceType
from app.pipeline.generate import (
    DEFAULT_BAD_CRITERIA,
    DEFAULT_GOOD_CRITERIA,
    generate_from_source,
    generate_project_questions,
)
from tests.fakes import FakeLLM

VALID_3 = [
    {
        "type": "knowledge",
        "stem": "讲一下 HashMap 底层原理",
        "tags": ["Java", "数据结构"],
        "difficulty": 2,
        "good_criteria": ["能讲清数组+链表结构", "能说明红黑树转换"],
        "bad_criteria": ["只说数组", "说不清扩容"],
    },
    {
        "type": "design",
        "stem": "设计一个短链接系统",
        "tags": ["系统设计"],
        "difficulty": 3,
        "good_criteria": ["考虑读写比例", "有缓存策略"],
        "bad_criteria": ["没有容量估算"],
    },
    {
        "type": "knowledge",
        "stem": "TCP 三次握手为什么不是两次",
        "tags": ["网络"],
        "difficulty": 2,
        "good_criteria": ["讲清半连接队列"],
        "bad_criteria": ["只背课本"],
    },
]


def add_source(db, **kw):
    source = Source(type=kw.pop("type", SourceType.manual), source_hash="src-1", **kw)
    db.add(source)
    from app.db import commit

    commit(db)
    db.refresh(source)
    return source


# --- happy ---


def test_generate_from_source_three_questions(db):
    source = add_source(db, cleaned_text="一面：\n问了 HashMap、短链接、TCP。")
    llm = FakeLLM([VALID_3])
    questions = generate_from_source(source, [], llm)

    assert len(questions) == 3
    assert questions[0].type == QuestionType.knowledge
    assert questions[1].type == QuestionType.design
    assert all(q.source_id == source.id for q in questions)
    assert questions[0].tags == ["Java", "数据结构"]
    assert questions[0].difficulty == 2


def test_generate_batches_by_rounds(db):
    """多轮面经按"一面/二面"分批调用 LLM 再合并（防长输出截断）。"""
    source = add_source(db, cleaned_text="一面：\n问了 HashMap。\n二面：\n问了短链接。")
    llm = FakeLLM([VALID_3[:1], VALID_3[1:2]])
    questions = generate_from_source(source, [], llm)

    assert len(llm.calls) == 2  # 每轮一次调用
    assert [q.stem for q in questions] == [VALID_3[0]["stem"], VALID_3[1]["stem"]]
    assert all(q.source_id == source.id for q in questions)


def test_criteria_bound_to_own_stem(db):
    """一致性：good/bad criteria 与同条目 stem 绑定，不串位。"""
    source = add_source(db)
    llm = FakeLLM([VALID_3])
    questions = generate_from_source(source, [], llm)
    for q, item in zip(questions, VALID_3):
        assert q.good_criteria == item["good_criteria"]
        assert q.bad_criteria == item["bad_criteria"]


def test_generate_project_questions(db):
    resume = add_source(db, type=SourceType.resume, cleaned_text="项目一：Agent 系统\n项目二：RAG 平台")
    payload = [
        {"type": "project", "stem": "讲一下 Agent 系统的技术难点和取舍", "tags": ["Agent"],
         "difficulty": 2, "good_criteria": ["基于真实经历"], "bad_criteria": ["编造"]},
        {"type": "project", "stem": "讲一下 RAG 平台的检索优化", "tags": ["RAG"],
         "difficulty": 2, "good_criteria": ["说清评估"], "bad_criteria": ["泛泛而谈"]},
    ]
    llm = FakeLLM([payload])
    questions = generate_project_questions(resume, limit=1, llm=llm)
    assert len(questions) == 1
    assert questions[0].type == QuestionType.project
    assert "Agent" in questions[0].stem


def test_existing_questions_injected_into_prompt(db):
    source = add_source(db)
    existing = [
        Question(source_id=1, type=QuestionType.knowledge, stem="旧题：Redis 持久化")
    ]
    llm = FakeLLM([[]])
    generate_from_source(source, existing, llm)
    content = llm.calls[0][0]["content"]
    assert "旧题：Redis 持久化" in content


# --- edge ---


def test_smalltalk_only_returns_empty(db):
    source = add_source(db, cleaned_text="面试官很和蔼，问了问学校情况。")
    llm = FakeLLM([[]])
    assert generate_from_source(source, [], llm) == []


def test_long_stem_truncated(db):
    source = add_source(db)
    payload = [{"type": "knowledge", "stem": "长" * 1000, "tags": [], "difficulty": 1,
                "good_criteria": [], "bad_criteria": []}]
    llm = FakeLLM([payload])
    questions = generate_from_source(source, [], llm)
    assert len(questions[0].stem) <= 503
    assert questions[0].stem.endswith("...")


def test_missing_criteria_use_defaults(db):
    source = add_source(db)
    payload = [{"type": "knowledge", "stem": "没有 criteria 的题"}]
    llm = FakeLLM([payload])
    questions = generate_from_source(source, [], llm)
    assert questions[0].good_criteria == DEFAULT_GOOD_CRITERIA
    assert questions[0].bad_criteria == DEFAULT_BAD_CRITERIA


def test_invalid_type_entries_dropped(db):
    source = add_source(db)
    payload = [
        {"type": "knowledge", "stem": "合法题", "tags": [], "difficulty": 1},
        {"type": "essay", "stem": "非法类型"},
        {"stem": "缺类型"},
    ]
    llm = FakeLLM([payload])
    questions = generate_from_source(source, [], llm)
    assert [q.stem for q in questions] == ["合法题"]


def test_missing_or_garbage_fields_handled(db):
    source = add_source(db)
    payload = [
        {"type": "knowledge", "stem": "   ", "tags": [], "difficulty": 1},
        {"type": "knowledge", "stem": "难度钳制题", "tags": "不是列表", "difficulty": 6},
        {"type": "knowledge", "stem": "难度非法题", "tags": ["x"], "difficulty": "abc"},
    ]
    llm = FakeLLM([payload])
    questions = generate_from_source(source, [], llm)
    assert len(questions) == 2
    assert questions[0].difficulty == 5  # 6 钳制到 5
    assert questions[0].tags == []
    assert questions[1].difficulty == 1  # 非整数回退 1


def test_out_of_vocab_tags_filtered(db):
    """词表外的标签被丢弃，词表内保留。"""
    source = add_source(db)
    payload = [
        {"type": "knowledge", "stem": "标签过滤题", "tags": ["Java", "集合", "RAG"], "difficulty": 1},
    ]
    llm = FakeLLM([payload])
    questions = generate_from_source(source, [], llm)
    assert questions[0].tags == ["Java", "RAG"]  # "集合" 不在词表


def test_tags_capped_at_max(db):
    """tags 超过上限只保留前 MAX_TAGS 个。"""
    source = add_source(db)
    payload = [
        {"type": "knowledge", "stem": "标签上限题", "tags": ["Java", "RAG", "Agent", "Redis", "微调", "网络"], "difficulty": 1},
    ]
    llm = FakeLLM([payload])
    questions = generate_from_source(source, [], llm)
    assert len(questions[0].tags) == 5


def test_non_str_stem_entries_dropped(db):
    """回归：非 str 类型 stem 直接丢弃，不转成垃圾题干。"""
    source = add_source(db)
    payload = [
        {"type": "knowledge", "stem": ["不是字符串"], "tags": [], "difficulty": 1},
        {"type": "knowledge", "stem": 12345, "tags": [], "difficulty": 1},
        {"type": "knowledge", "stem": None, "tags": [], "difficulty": 1},
        {"type": "knowledge", "stem": "  合法的题干  ", "tags": [], "difficulty": 1},
    ]
    llm = FakeLLM([payload])
    questions = generate_from_source(source, [], llm)
    assert [q.stem for q in questions] == ["合法的题干"]


# --- fail ---


def test_llm_json_error_raises_generation_error(db):
    source = add_source(db)
    llm = FakeLLM([LLMJsonError("cannot parse json")])
    with pytest.raises(GenerationError):
        generate_from_source(source, [], llm)


def test_llm_error_raises_generation_error(db):
    source = add_source(db)
    llm = FakeLLM([LLMError("llm down")])
    with pytest.raises(GenerationError):
        generate_from_source(source, [], llm)


def test_output_not_a_list_raises_generation_error(db):
    source = add_source(db)
    llm = FakeLLM([{"type": "knowledge", "stem": "单对象不是数组"}])
    with pytest.raises(GenerationError, match="must be a list"):
        generate_from_source(source, [], llm)
