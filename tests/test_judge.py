import pytest

from app.db import commit
from app.errors import LLMError
from app.judge.judge import (
    PROMPT_VERSION,
    STATUS_FAILED,
    STATUS_OK,
    compute_total,
    judge,
)
from app.models import Question, QuestionType, Session, SessionKind, Source, SourceType
from tests.fakes import FakeLLM

VALID = {
    "scores": {"accuracy": 85, "completeness": 70, "clarity": 90, "depth": 60},
    "review": "整体不错，深度欠佳",
    "reference_answer": "应从数据结构和扩容机制讲起",
    "weak_tags": ["RAG"],
}

TRANSCRIPT = [
    {"role": "user", "content": "HashMap 底层是数组加链表"},
    {"role": "interviewer", "content": "什么时候转红黑树？"},
    {"role": "user", "content": "链表长度超过 8 时"},
]


def make_question(type=QuestionType.knowledge, **kw):
    return Question(
        source_id=1,
        type=type,
        stem="讲一下 HashMap 底层原理",
        tags=["Java"],
        difficulty=2,
        good_criteria=["完整、准确", "结构清晰"],
        bad_criteria=["答非所问"],
        **kw,
    )


def add_session(db):
    source = Source(type=SourceType.manual, source_hash="h1")
    db.add(source)
    commit(db)
    db.refresh(source)
    question = Question(source_id=source.id, type=QuestionType.knowledge, stem="题")
    db.add(question)
    commit(db)
    db.refresh(question)
    s = Session(question_id=question.id, kind=SessionKind.chain)
    db.add(s)
    commit(db)
    db.refresh(s)
    return s


# --- happy ---


def test_judge_full_valid_and_persists(db):
    s = add_session(db)
    llm = FakeLLM([VALID])
    judgment = judge(make_question(), TRANSCRIPT, "judge-model", llm, session_id=s.id)

    db.add(judgment)
    commit(db)
    db.refresh(judgment)

    assert judgment.status == STATUS_OK
    assert judgment.scores["accuracy"] == 85
    assert judgment.total_score == compute_total(VALID["scores"])
    assert judgment.review == "整体不错，深度欠佳"
    assert judgment.reference_answer.startswith("应从")
    assert judgment.weak_tags == ["RAG"]
    assert judgment.model == "judge-model"


def test_compute_total_formula():
    assert compute_total({"accuracy": 100, "completeness": 100, "clarity": 100, "depth": 100}) == 100
    assert compute_total({"accuracy": 0, "completeness": 0, "clarity": 0, "depth": 0}) == 0
    assert compute_total({"accuracy": 80, "completeness": 80, "clarity": 90, "depth": 90}) == 84
    assert compute_total({"accuracy": 100}) == 30  # 缺失维度补 0


def test_boundary_scores_0_and_100(db):
    payload = {
        "scores": {"accuracy": 100, "completeness": 0, "clarity": 50, "depth": 50},
        "review": "", "reference_answer": "", "weak_tags": [],
    }
    llm = FakeLLM([payload])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert judgment.scores["accuracy"] == 100
    assert judgment.scores["completeness"] == 0
    assert judgment.total_score == 50  # 100*.3+0*.3+50*.2+50*.2


def test_criteria_injected_into_prompt(db):
    llm = FakeLLM([VALID])
    judge(make_question(), TRANSCRIPT, "m", llm)
    content = "\n".join(m["content"] for m in llm.calls[0])
    assert "完整、准确" in content
    assert "答非所问" in content
    assert "讲一下 HashMap 底层原理" in content


def test_qtype_prompt_differ(db):
    k_llm = FakeLLM([VALID])
    judge(make_question(type=QuestionType.knowledge), TRANSCRIPT, "m", k_llm)
    p_llm = FakeLLM([VALID])
    judge(make_question(type=QuestionType.project), TRANSCRIPT, "m", p_llm)
    k_content = "\n".join(m["content"] for m in k_llm.calls[0])
    p_content = "\n".join(m["content"] for m in p_llm.calls[0])
    assert "准确性优先" in k_content
    assert "真实性与反思深度优先" in p_content


# --- edge ---


def test_missing_fields_use_defaults(db):
    llm = FakeLLM([{}])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert judgment.status == STATUS_OK
    assert judgment.scores == {
        "accuracy": 0, "completeness": 0, "clarity": 0, "depth": 0, "status": "ok",
    }
    assert judgment.review == ""
    assert judgment.reference_answer == ""
    assert judgment.weak_tags == []


def test_out_of_range_scores_clamped(db):
    payload = {
        "scores": {"accuracy": 150, "completeness": -10, "clarity": "abc", "depth": "90"},
        "review": "x", "reference_answer": "y", "weak_tags": ["z"],
    }
    llm = FakeLLM([payload])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert judgment.scores["accuracy"] == 100
    assert judgment.scores["completeness"] == 0
    assert judgment.scores["clarity"] == 0
    assert judgment.scores["depth"] == 90  # 字符串数字可解析


def test_long_transcript_truncated(db):
    long_transcript = [
        {"role": "user", "content": "长" * 60_000}
    ]
    llm = FakeLLM([VALID])
    judge(make_question(), long_transcript, "m", llm)
    user_content = llm.calls[0][1]["content"]  # 第二条为 user 消息
    assert "..." in user_content  # 发生了截断
    assert len(user_content) < 60_000  # 未把完整 6 万字符塞进 prompt


def test_weak_tags_garbage_types_cleaned(db):
    payload = {**VALID, "weak_tags": ["RAG", 123, None]}
    llm = FakeLLM([payload])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert judgment.weak_tags == ["RAG"]


def test_out_of_vocab_weak_tags_filtered(db):
    payload = {**VALID, "weak_tags": ["深度不足", "RAG", "表达不清"]}
    llm = FakeLLM([payload])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert judgment.weak_tags == ["RAG"]  # 能力词不在词表，被过滤


def test_weak_tags_capped_at_max(db):
    payload = {**VALID, "weak_tags": ["Java", "RAG", "Agent", "Redis"]}
    llm = FakeLLM([payload])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert len(judgment.weak_tags) == 3


# --- fail ---


def test_llm_failure_retry_then_failed_status(db):
    s = add_session(db)
    llm = FakeLLM([LLMError("boom"), LLMError("boom")])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm, session_id=s.id)
    assert judgment.status == STATUS_FAILED
    assert len(llm.calls) == 2  # 重试 1 次
    db.add(judgment)
    commit(db)
    db.refresh(judgment)
    assert db.get(type(judgment), judgment.id).status == STATUS_FAILED


def test_llm_failure_retry_recovers(db):
    llm = FakeLLM([LLMError("boom"), VALID])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert judgment.status == STATUS_OK
    assert judgment.total_score == compute_total(VALID["scores"])
    assert len(llm.calls) == 2


def test_output_not_object_failed(db):
    llm = FakeLLM([[1, 2, 3]])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert judgment.status == STATUS_FAILED
    assert "判分失败" in judgment.review


def test_json_error_retry_then_failed(db):
    from app.errors import LLMJsonError

    llm = FakeLLM([LLMJsonError("bad"), LLMJsonError("bad")])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    assert judgment.status == STATUS_FAILED


# --- 回归 ---


def test_prompt_version_frozen():
    """PROMPT_VERSION 变更需显式更新此测试与 fixture。"""
    assert PROMPT_VERSION == "judge_v4"


def test_max_level_injected_into_prompt(db):
    llm = FakeLLM([VALID])
    judge(make_question(), TRANSCRIPT, "m", llm, max_level=4)
    content = "\n".join(m["content"] for m in llm.calls[0])
    assert "追问深度" in content
    assert "L4" in content
    assert "depth 维度按此校准" in content


def test_no_max_level_no_depth_note(db):
    llm = FakeLLM([VALID])
    judge(make_question(), TRANSCRIPT, "m", llm)
    content = "\n".join(m["content"] for m in llm.calls[0])
    assert "追问深度" not in content


def test_reference_injected_into_prompt(db):
    llm = FakeLLM([VALID])
    judge(
        make_question(),
        TRANSCRIPT,
        "m",
        llm,
        reference="- 旧题\n  高分回答内容",
    )
    content = "\n".join(m["content"] for m in llm.calls[0])
    assert "高分回答" in content
    assert "高分回答内容" in content
    assert "不要照抄" in content


def test_no_reference_no_section(db):
    llm = FakeLLM([VALID])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm)
    content = "\n".join(m["content"] for m in llm.calls[0])
    assert "高分回答" not in content  # 空参考不注入参考段（冷启动降级）
    assert judgment.scores.get("reference_used") is None


def test_reference_used_flag(db):
    llm = FakeLLM([VALID])
    judgment = judge(make_question(), TRANSCRIPT, "m", llm, reference="- 旧题\n  高分回答")
    assert judgment.scores["reference_used"] is True
