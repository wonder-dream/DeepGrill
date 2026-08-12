import pytest
from sqlalchemy import select

from app.db import commit
from app.difficulty import target_level_for
from app.errors import ChainStateError, LLMError
from app.judge.chain import DEGRADED_HINT, ChainSession, resume
from app.judge.judge import STATUS_FAILED, STATUS_OK
from app.models import (
    Attempt,
    Judgment,
    Question,
    QuestionType,
    Session,
    SessionKind,
    SessionStatus,
    Source,
    SourceType,
    User,
)
from tests.fakes import FakeLLM

VALID_JUDGMENT = {
    "scores": {"accuracy": 85, "completeness": 70, "clarity": 90, "depth": 60},
    "review": "整体不错",
    "reference_answer": "参考答案",
    "weak_tags": ["RAG"],
}

CONTINUE = {"action": "continue", "followup": "追问：谈谈扩容机制", "quality": "correct", "completeness": "complete", "level": 2}
FINISH = {"action": "finish", "followup": "好的，本轮结束", "quality": "correct", "completeness": "complete", "level": 5}


def round_l2():
    return {"action": "continue", "followup": "追问L2", "quality": "correct", "completeness": "complete", "level": 2}


def round_l3():
    return {"action": "continue", "followup": "追问L3", "quality": "correct", "completeness": "complete", "level": 3}


def round_l4():
    return {"action": "continue", "followup": "追问L4", "quality": "correct", "completeness": "complete", "level": 4}


def finish_l5():
    return {"action": "finish", "followup": "L5 证明充分", "quality": "correct", "completeness": "complete", "level": 5}


def add_session(db):
    source = Source(type=SourceType.manual, source_hash="h1")
    db.add(source)
    commit(db)
    db.refresh(source)
    question = Question(
        source_id=source.id,
        type=QuestionType.knowledge,
        stem="讲一下 HashMap 底层原理",
        good_criteria=["完整、准确"],
        bad_criteria=["答非所问"],
    )
    db.add(question)
    commit(db)
    db.refresh(question)
    user = User(email="chainuser@test.com", username="chainuser", password_hash="x")
    db.add(user)
    commit(db)
    db.refresh(user)
    s = Session(question_id=question.id, user_id=user.id, kind=SessionKind.chain)
    db.add(s)
    commit(db)
    db.refresh(s)
    return s, question


def attempts(db, session_id):
    return list(
        db.scalars(
            select(Attempt)
            .where(Attempt.session_id == session_id)
            .order_by(Attempt.round_no)
        )
    )


# --- happy ---


def test_good_answers_probed_to_l5_then_finish(db):
    """深挖档（4-5 星）：答得好 → 持续深挖到 L5，达标即 finish。"""
    s, q = add_session(db)
    q.difficulty = 4  # 深挖档
    commit(db)
    llm = FakeLLM([round_l2(), round_l3(), round_l4(), finish_l5(), VALID_JUDGMENT])
    chain = ChainSession(s.id, q, llm, judge_model="judge-m", max_rounds=20)

    r1 = chain.next_round("HashMap 底层是数组加链表")
    assert r1["finished"] is False
    assert r1["interviewer_message"] == "追问L2"

    r2 = chain.next_round("链表超 8 转红黑树")
    assert r2["finished"] is False
    assert r2["interviewer_message"] == "追问L3"

    r3 = chain.next_round("红黑树 O(log n)，扩容 2 倍")
    assert r3["finished"] is False

    r4 = chain.next_round("极端碰撞会退化，并发扩容会丢数据")
    assert r4["finished"] is True  # L5 连续 correct → 证明充分

    rows = attempts(db, s.id)
    assert [a.round_no for a in rows] == [0, 1, 2, 3]
    assert [a.level for a in rows] == [2, 3, 4, 5]  # 层级递增落库
    assert chain.rounds_done == 4

    judgment = chain.finish()
    assert judgment.status == STATUS_OK
    assert judgment.total_score == 76  # 85*.3+70*.3+90*.2+60*.2
    judge_content = "\n".join(m["content"] for m in llm.calls[-1])
    assert "追问深度" in judge_content and "L5" in judge_content  # max_level 联动判分


def test_prompt_injects_difficulty_and_target_level(db):
    """难度分级追问：prompt 注入题目难度/档位名与目标深度说明。"""
    s, q = add_session(db)
    q.difficulty = 2
    commit(db)
    llm = FakeLLM([FINISH, VALID_JUDGMENT])
    chain = ChainSession(
        s.id, q, llm, judge_model="m", target_level=target_level_for(q.difficulty)
    )
    chain.next_round("答")
    content = "\n".join(m["content"] for m in llm.calls[0])
    assert "题目难度：2/5（基础）" in content
    assert "追问档位：浅挖档" in content  # 档位注入
    assert "不设强制的深度目标" in content  # 浅挖档以回答完整清晰为准
    assert "绝不深挖" in content  # 浅挖档收尾策略


def test_prompt_injects_knowledge(db):
    """chain 追问：knowledge（RAG）注入 prompt 供面试官核对要点。"""
    s, q = add_session(db)
    llm = FakeLLM([FINISH, VALID_JUDGMENT])
    chain = ChainSession(
        s.id, q, llm, judge_model="m", knowledge="- [Redis] 淘汰策略 LRU 实现细节"
    )
    chain.next_round("答")
    content = "\n".join(m["content"] for m in llm.calls[0])
    assert "相关知识资料" in content
    assert "LRU 实现细节" in content


def test_two_consecutive_bad_answers_finish(db):
    """深挖档：连续 2 次差评（wrong/unsure）→ 判定探到底收尾；单次差评不误杀。"""
    s, q = add_session(db)
    q.difficulty = 5
    commit(db)
    llm = FakeLLM([
        round_l2(),
        {"action": "continue", "followup": "再确认L2", "quality": "wrong", "level": 2},
        {"action": "finish", "followup": "连续两次答不出，探到底", "quality": "unsure", "level": 2},
        VALID_JUDGMENT,
    ])
    chain = ChainSession(s.id, q, llm, judge_model="m", max_rounds=20)

    assert chain.next_round("答1")["finished"] is False  # correct
    assert chain.next_round("答2")["finished"] is False  # 第一次 wrong，再给机会
    r3 = chain.next_round("答3")
    assert r3["finished"] is True  # 第二次差评 → 收尾
    assert len(attempts(db, s.id)) == 3


def test_code_forces_finish_when_llm_violates_rule(db):
    """代码侧兜底：LLM 连续 2 次 wrong 仍返回 continue（违反 prompt 规则）→ 强制收尾。"""
    s, q = add_session(db)
    q.difficulty = 5
    commit(db)
    llm = FakeLLM([
        round_l2(),
        {"action": "continue", "followup": "再确认L2", "quality": "wrong", "level": 2},
        {"action": "continue", "followup": "又答不出还追", "quality": "wrong", "level": 2},
        VALID_JUDGMENT,
    ])
    chain = ChainSession(s.id, q, llm, judge_model="m", max_rounds=20)

    assert chain.next_round("答1")["finished"] is False
    assert chain.next_round("答2")["finished"] is False  # 第一次 wrong
    r3 = chain.next_round("答3")
    assert r3["finished"] is True  # 连续 2 次 wrong + LLM continue → 代码强制收尾

    judgment = chain.finish()
    assert judgment.status == STATUS_OK
    assert len(attempts(db, s.id)) == 3


def test_quality_persisted_and_passed_to_judge(db):
    """quality 逐轮落库；finish 时质量轨迹注入判分 prompt。"""
    s, q = add_session(db)
    q.difficulty = 4
    commit(db)
    llm = FakeLLM([round_l2(), finish_l5(), VALID_JUDGMENT])
    chain = ChainSession(s.id, q, llm, judge_model="m", max_rounds=20)
    chain.next_round("答1")
    chain.next_round("答2")

    rows = attempts(db, s.id)
    assert [a.quality for a in rows] == ["correct", "correct"]

    chain.finish()
    judge_content = "\n".join(m["content"] for m in llm.calls[-1])
    assert "correct → correct" in judge_content  # 质量轨迹传入判分


def test_single_bad_answer_not_finished(db):
    """深挖档：单次差评后答好 → 继续深挖（不因偶发卡壳误收尾）。"""
    s, q = add_session(db)
    q.difficulty = 5
    commit(db)
    llm = FakeLLM([
        round_l2(),
        {"action": "continue", "followup": "再确认L2", "quality": "wrong", "level": 2},
        round_l3(),
        finish_l5(),
        VALID_JUDGMENT,
    ])
    chain = ChainSession(s.id, q, llm, judge_model="m", max_rounds=20)

    assert chain.next_round("答1")["finished"] is False
    assert chain.next_round("答2")["finished"] is False  # 单次差评
    r3 = chain.next_round("答3")
    assert r3["finished"] is False  # 答好了继续
    r4 = chain.next_round("答4")
    assert r4["finished"] is True


def test_poor_answers_forced_finish_at_max_rounds(db):
    s, q = add_session(db)
    q.difficulty = 5
    commit(db)
    llm = FakeLLM([CONTINUE, CONTINUE, CONTINUE, VALID_JUDGMENT])
    chain = ChainSession(s.id, q, llm, judge_model="m", max_rounds=3)

    assert chain.next_round("答1")["finished"] is False
    assert chain.next_round("答2")["finished"] is False
    r3 = chain.next_round("答3")
    assert r3["finished"] is True  # 到限强制结束

    assert len(attempts(db, s.id)) == 3
    judgment = chain.finish()
    assert judgment.status == STATUS_OK


def test_light_tier_complete_answer_finishes_immediately(db):
    """浅挖档（1-2 星）：回答完整清晰（complete+correct）→ 第一轮直接收尾，不再强制深挖。"""
    s, q = add_session(db)  # 难度 1
    llm = FakeLLM([
        {"action": "finish", "followup": "答得完整，本轮结束", "quality": "correct", "completeness": "complete", "level": 1},
        VALID_JUDGMENT,
    ])
    chain = ChainSession(s.id, q, llm, judge_model="m")
    r = chain.next_round("HashMap 底层是数组加链表，冲突时链表转红黑树")
    assert r["finished"] is True
    assert len(attempts(db, s.id)) == 1


def test_light_tier_llm_continues_forced_finish(db):
    """浅挖档兜底：LLM 对完整回答仍 continue（违反 prompt）→ 代码按完整度强制收尾。"""
    s, q = add_session(db)
    llm = FakeLLM([
        {"action": "continue", "followup": "还追问", "quality": "correct", "completeness": "complete", "level": 1},
        VALID_JUDGMENT,
    ])
    chain = ChainSession(s.id, q, llm, judge_model="m")
    r = chain.next_round("答完整清晰")
    assert r["finished"] is True  # complete+correct → 代码兜底收尾


def test_light_tier_partial_two_rounds_finish(db):
    """浅挖档：回答有缺口（partial）→ 只追 1 轮缺口后收尾，不无限追问。"""
    s, q = add_session(db)
    llm = FakeLLM([
        {"action": "continue", "followup": "追缺口", "quality": "partial", "completeness": "partial", "level": 1},
        {"action": "continue", "followup": "还有缺口", "quality": "partial", "completeness": "partial", "level": 2},
        VALID_JUDGMENT,
    ])
    chain = ChainSession(s.id, q, llm, judge_model="m")
    assert chain.next_round("答了一部分")["finished"] is False
    r2 = chain.next_round("补了一点")["finished"] is True  # 连续 2 轮 partial → 收尾
    assert r2 is True
    assert len(attempts(db, s.id)) == 2


def test_medium_tier_partial_two_rounds_finish(db):
    """中挖档（3 星）：partial 同层缺口最多追 2 次后收尾，不无限同层追问。"""
    s, q = add_session(db)
    q.difficulty = 3
    commit(db)
    llm = FakeLLM([
        {"action": "continue", "followup": "追缺口1", "quality": "partial", "completeness": "partial", "level": 3},
        {"action": "continue", "followup": "追缺口2", "quality": "partial", "completeness": "partial", "level": 3},
        VALID_JUDGMENT,
    ])
    chain = ChainSession(s.id, q, llm, judge_model="m", target_level=4)
    assert chain.next_round("答了一部分")["finished"] is False
    r2 = chain.next_round("补了一点")["finished"] is True  # 连续 2 轮 partial → 收尾
    assert r2 is True
    assert len(attempts(db, s.id)) == 2


def test_judgment_persisted_with_session(db):
    s, q = add_session(db)
    llm = FakeLLM([FINISH, VALID_JUDGMENT])
    chain = ChainSession(s.id, q, llm, judge_model="judge-m")
    chain.next_round("答")
    chain.finish()
    judgment = db.scalars(
        select(Judgment).where(Judgment.session_id == s.id)
    ).one()
    assert judgment.model == "judge-m"
    assert judgment.status == STATUS_OK


# --- edge ---


def test_resume_continues_round_numbering(db):
    s, q = add_session(db)
    q.difficulty = 5
    commit(db)
    llm1 = FakeLLM([CONTINUE, CONTINUE])
    chain1 = ChainSession(s.id, q, llm1, judge_model="m", max_rounds=5)
    chain1.next_round("答1")
    chain1.next_round("答2")
    assert chain1.rounds_done == 2

    llm2 = FakeLLM([CONTINUE, CONTINUE, CONTINUE, VALID_JUDGMENT])
    chain2 = resume(s.id, q, llm2, judge_model="m", max_rounds=5)
    assert chain2.rounds_done == 2

    r3 = chain2.next_round("答3")
    assert r3["finished"] is False  # 第 3 轮（round_no=2）继续
    r4 = chain2.next_round("答4")
    assert r4["finished"] is False
    r5 = chain2.next_round("答5")
    assert r5["finished"] is True  # 恢复后剩余 3 轮，第 5 轮强制结束

    rows = attempts(db, s.id)
    assert [a.round_no for a in rows] == [0, 1, 2, 3, 4]
    assert rows[2].is_followup is True


def test_resume_finished_session_is_terminal(db):
    s, q = add_session(db)
    llm = FakeLLM([FINISH, VALID_JUDGMENT])
    chain = ChainSession(s.id, q, llm, judge_model="m")
    chain.next_round("答")
    chain.finish()
    db.expire_all()  # 刷新身份映射，读到链内事务的更新
    assert db.get(Session, s.id).status == SessionStatus.finished
    assert db.get(Session, s.id).ended_at is not None

    llm2 = FakeLLM([])
    resumed = resume(s.id, q, llm2, judge_model="m")
    assert resumed.finished is True
    with pytest.raises(ChainStateError):
        resumed.next_round("再来一轮")


def test_failed_rounds_still_persisted(db):
    s, q = add_session(db)
    llm = FakeLLM([{"action": "continue", "followup": "追问X"}, FINISH, VALID_JUDGMENT])
    chain = ChainSession(s.id, q, llm, judge_model="m")
    chain.next_round("答1")
    chain.next_round("答2")
    rows = attempts(db, s.id)
    assert rows[0].feedback_text == "追问X"
    assert rows[1].feedback_text == "好的，本轮结束"


# --- fail ---


def test_llm_failure_each_round_degrades_and_continues(db):
    s, q = add_session(db)
    llm = FakeLLM([LLMError("down"), LLMError("down"), FINISH, VALID_JUDGMENT])
    chain = ChainSession(s.id, q, llm, judge_model="m", max_rounds=20)

    r1 = chain.next_round("答1")
    assert r1["interviewer_message"] == DEGRADED_HINT
    assert r1["finished"] is False
    assert attempts(db, s.id)[0].feedback_text == DEGRADED_HINT

    r2 = chain.next_round("答2")
    assert r2["finished"] is True


def test_judge_failure_persists_failed_status(db):
    s, q = add_session(db)
    llm = FakeLLM([FINISH, LLMError("judge down"), LLMError("judge down")])
    chain = ChainSession(s.id, q, llm, judge_model="m")
    chain.next_round("答")
    judgment = chain.finish()
    assert judgment.status == STATUS_FAILED
    reloaded = db.scalars(
        select(Judgment).where(Judgment.session_id == s.id)
    ).one()
    assert reloaded.status == STATUS_FAILED
    assert "判分失败" in reloaded.review


def test_next_round_after_finish_raises(db):
    s, q = add_session(db)
    llm = FakeLLM([FINISH, VALID_JUDGMENT])
    chain = ChainSession(s.id, q, llm, judge_model="m")
    chain.next_round("答")
    assert chain.finished is True
    with pytest.raises(ChainStateError):
        chain.next_round("再来一轮")

