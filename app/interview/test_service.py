"""面试领域：编排循环、判定落库、收尾判据。

用 `tests/fakes.FakeLLM` 预录模型输出 —— 这些测试**不联网、不花钱**，
而它们要钉住的性质全是"代码持有控制流"这件事的细节（ADR-0001）：

· 命中快照是**累积**的，且"已命中"不会被后续轮次打回去
· 收尾的否决权在代码手里（`max_rounds` 到顶强制收尾）
· 连续两轮没有**新**命中就收尾（§3.4 的成本保护）
· 模型漏答考察点 / 给非法状态 / 给未知 id 时的降级**不静默**
· 判分失败要落库（`status='failed'`），不能永远停在"判分中"
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.account import repository as account_repository
from app.account import service as account_service
from app.bank import repository as bank_repository
from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Attempt,
    Criterion,
    Domain,
    Evaluation,
    KnowledgePoint,
    Question,
    User,
)
from app.errors import InvalidInput, NotFound, QuotaExhausted
from app.interview import rules, service
from app.llm import LLMCallError
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply, round_reply

ME = 2
OTHER = 77


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "interview.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        s.add(User(id=ME, email="me@local", username="me", password_hash="h", role="user"))
        # 另一个用户：用来验证"别人的私有题不可见/不可开面试"（外键真的开着）
        s.add(User(id=OTHER, email="other@local", username="o", password_hash="h", role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        point = KnowledgePoint(domain_id=1, name="volatile", status="confirmed")
        s.add(point)
        s.flush()
        for seq, text in enumerate(
            ["保证可见性与有序性", "底层靠内存屏障", "不保证原子性"], start=1
        ):
            s.add(Criterion(point_id=point.id, seq=seq, text=text, shared=0))
        s.flush()
        s.add(
            Question(
                id=1,
                kind="knowledge",
                stem="说说 volatile",
                difficulty=3,
                primary_point_id=point.id,
                origin="seed",
                visibility="public",
            )
        )
        s.commit()
        yield s


def _round_reply(hits: list[tuple[int, str]], followup: str = "继续", finish: object = False):
    """面试官那一轮的回复。**格式的唯一住处是 `tests.fakes.round_reply`** ——
    两段式（散文 + 分隔行 + json）的细节不该在测试里各写一遍。

    `finish` 的类型是 `object`：模型会回字符串 `"false"`（见
    `test_only_a_real_true_asks_for_finish`），那正是这条规则需要被钉住的原因。
    """
    return round_reply(hits=hits, prose=followup, finish=finish)


def _eval_reply(accuracy=80, completeness=70, clarity=90, depth=60, review="还行"):
    return FakeReply(
        data={
            "scores": {
                "accuracy": accuracy,
                "completeness": completeness,
                "clarity": clarity,
                "depth": depth,
            },
            "review": review,
        }
    )


# ---------------------------------------------------------------------------
# 建面试：额度与可见性
# ---------------------------------------------------------------------------
def test_start_drill_charges_one_unit(session: Session) -> None:
    ts = service.start_drill(session, user_id=ME, question_id=1)
    assert ts.question_id == 1
    assert account_service.units_used_today(session, ME) == 1


def test_start_interview_charges_six_units(session: Session) -> None:
    service.start_interview(session, user_id=ME, question_ids=[1])
    assert account_service.units_used_today(session, ME) == 6


def test_quota_exhaustion_refuses_and_leaves_no_half_interview(session: Session) -> None:
    """额度不足时**不留半场面试** —— 扣点与建会话的顺序因此不能反。"""
    from app.account import service as account
    from app.db.models import Interview

    # 用常量算"还能开几场"，不写死次数：每日上限是标定出来的（决策 71）
    affordable = account.DAILY_UNITS // account.COST["interview"]
    assert affordable >= 1
    for _ in range(affordable):
        row = service.start_interview(session, user_id=ME, question_ids=[1])
        # 决策 97：一场没结束就开不了第二场 —— 所以每场开完立刻放弃，
        # 这条路本身也顺带被覆盖到了。
        service.abandon_interview(session, user_id=ME, interview_id=row.id)

    before = len(session.query(Interview).all())
    with pytest.raises(QuotaExhausted):
        service.start_interview(session, user_id=ME, question_ids=[1])
    assert len(session.query(Interview).all()) == before, "额度不足不该留下面试行"


# ---------------------------------------------------------------------------
# 一场没结束，不许开第二场（决策 97）
# ---------------------------------------------------------------------------
def test_a_second_interview_is_refused_while_one_is_unfinished(session: Session) -> None:
    """两种开场路都被挡住 —— 闸放在两条路唯一的汇合点 `_create_interview` 上。"""
    service.start_interview(session, user_id=ME, question_ids=[1])

    with pytest.raises(InvalidInput) as e:
        service.start_interview(session, user_id=ME, question_ids=[1])
    assert service.UNFINISHED_MESSAGE in str(e.value)

    with pytest.raises(InvalidInput):
        service.start_drill(session, user_id=ME, question_id=1)


def test_the_refusal_does_not_charge_quota(session: Session) -> None:
    """**闸必须在扣点之前**：否则"没开成还扣了 6 点"是第二种坏结果。"""
    from app.account import service as account

    service.start_interview(session, user_id=ME, question_ids=[1])
    charged = account.units_used_today(session, ME)

    with pytest.raises(InvalidInput):
        service.start_interview(session, user_id=ME, question_ids=[1])
    assert account.units_used_today(session, ME) == charged, "被拒的那次不该扣点"


def test_abandoning_frees_the_slot_and_keeps_the_record(session: Session) -> None:
    """放弃 = 松闸，但**不删记录**（决策 96 的记录页上照样看得见）。"""
    from app.db.models import Interview

    first = service.start_interview(session, user_id=ME, question_ids=[1])
    service.abandon_interview(session, user_id=ME, interview_id=first.id)

    assert service.unfinished_interview(session, ME) is None
    assert session.get(Interview, first.id).status == "abandoned"
    assert session.get(Interview, first.id).ended_at is not None
    assert [row.id for row in service.list_interviews(session, ME)] == [first.id]

    second = service.start_interview(session, user_id=ME, question_ids=[1])
    assert second.id != first.id, "放弃之后应该能开新的一场"


def test_abandon_is_idempotent(session: Session) -> None:
    """双击 / 后退重放 / 两个标签页同时点，都不该变成一堵错误页。"""

    row = service.start_drill(session, user_id=ME, question_id=1)
    first = service.abandon_interview(session, user_id=ME, interview_id=row.interview_id)
    ended = first.ended_at
    again = service.abandon_interview(session, user_id=ME, interview_id=row.interview_id)
    assert again.status == "abandoned"
    assert again.ended_at == ended, "第二次不该改写时间戳"


def test_abandoning_someone_elses_interview_is_not_found(session: Session) -> None:
    from app.db.models import Interview

    session.add(Interview(id=77, user_id=OTHER, mode="drill", status="active", quota_charged=1))
    session.commit()
    with pytest.raises(NotFound):
        service.abandon_interview(session, user_id=ME, interview_id=77)


def test_answering_an_abandoned_interview_is_refused(session: Session) -> None:
    """只看 `sessions.status` 不够：放弃之后旧标签页仍然能接着答（每轮都调模型）。"""

    ts = service.start_drill(session, user_id=ME, question_id=1)
    service.abandon_interview(session, user_id=ME, interview_id=ts.interview_id)
    assert ts.status == "active", "放弃整场不该顺手改题会话状态（那是另一层）"

    with pytest.raises(InvalidInput) as e:
        service.require_active(session, ts)
    assert service.ABANDONED_MESSAGE in str(e.value)


def test_the_daily_ration_never_blocks_a_single_interview(session: Session) -> None:
    """每日上限的那条**永远不该破**的约束：一天至少开得了一场完整面试。

    为什么不再钉"1 场 + 1–4 轮"那个区间：那是**标定值 8** 的配比（决策 71 拿真 token
    反推的），而运行值现在被临时调到 32 做测试（`CALIBRATED_DAILY_UNITS` 仍记着 8）。
    钉死具体数字会让"临时放宽"和"重新标定"看起来一样 —— 所以这里只钉约束，
    标定值本身由 `CALIBRATED_DAILY_UNITS` 与台账一起保管。
    """
    from app.account import service as account

    assert account.COST["interview"] <= account.DAILY_UNITS, "至少得能开一场面试"
    assert account.COST["interview"] <= account.CALIBRATED_DAILY_UNITS, (
        "标定值本身也必须能开一场面试（否则标定那次就标错了）"
    )


def test_cannot_start_on_invisible_question(session: Session) -> None:
    session.add(
        Question(
            id=2,
            kind="knowledge",
            stem="别人的私有题",
            difficulty=3,
            owner_user_id=OTHER,
            visibility="private",
            origin="generated",
        )
    )
    session.commit()
    with pytest.raises(NotFound):
        service.start_drill(session, user_id=ME, question_id=2)


def test_interview_refuses_empty_bank(session: Session) -> None:
    session.query(Question).delete()
    session.commit()
    with pytest.raises(InvalidInput):
        service.start_interview(session, user_id=ME, question_ids=[])


# ---------------------------------------------------------------------------
# 记一轮：累积快照
# ---------------------------------------------------------------------------
def test_round_records_cumulative_snapshot(session: Session) -> None:
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未涉及")]),
        # 第二轮：第三轮问到了，且第 2 条答对了 —— 累积快照必须把两者都吸收
        _round_reply([(1, "未涉及"), (2, "命中"), (3, "未命中")]),
    )

    first = service.submit_answer(session, ts=ts, answer_text="volatile 保证可见性", llm=llm)
    assert first.snapshot.statuses == {1: "命中", 2: "未命中", 3: "未涉及"}
    assert first.new_hits == 1

    second = service.submit_answer(session, ts=ts, answer_text="还靠内存屏障", llm=llm)
    assert second.snapshot.statuses == {1: "命中", 2: "命中", 3: "未命中"}, (
        "累积快照：第 1 条已命中不该被后来的「未涉及」打回去，第 2/3 条要被更新"
    )
    assert second.snapshot.covered_ids() == {1, 2, 3}, "被考过 = 命中 + 未命中"
    assert second.new_hits == 1, "本轮新命中的是第 2 条"


def test_first_round_reads_previous_before_writing(session: Session) -> None:
    """**回归测试**（§3.6 的 v1 具体 bug）：上一轮快照必须在写本轮之前取。

    若先写后取，`previous` 会等于本轮自己 → `new_hits` 永远是 0 → 追问在第二轮
    就被"没有新命中"这条收尾判据掐掉。这里用两轮都命中**不同**考察点来验证：
    第二轮的 `new_hits` 必须是 1，而不是 0。
    """
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未涉及"), (3, "未涉及")]),
        _round_reply([(1, "未涉及"), (2, "命中"), (3, "未涉及")], finish=False),
    )
    service.submit_answer(session, ts=ts, answer_text="a", llm=llm)
    second = service.submit_answer(session, ts=ts, answer_text="b", llm=llm)
    assert second.new_hits == 1
    assert second.finished is False, "还有新命中，不该收尾"


def test_attempts_are_persisted_with_round_numbers(session: Session) -> None:
    """状态显式落库（ADR-0001）：轮次、判定、下一问都要在库里能查。"""
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(_round_reply([(1, "命中"), (2, "未涉及"), (3, "未涉及")], "下一问"))
    service.submit_answer(session, ts=ts, answer_text="我的回答", llm=llm)

    rows = session.query(Attempt).filter(Attempt.session_id == ts.id).all()
    assert len(rows) == 1
    assert rows[0].round_no == 1
    assert rows[0].answer_text == "我的回答"
    assert rows[0].feedback_text == "下一问"
    assert rows[0].hits == {"1": "命中", "2": "未涉及", "3": "未涉及"}


# ---------------------------------------------------------------------------
# 收尾判据
# ---------------------------------------------------------------------------
def test_max_rounds_forces_finish_even_if_model_disagrees(session: Session) -> None:
    """**否决权在代码手里**（ADR-0001）：模型说继续，代码到顶就得收。"""
    ts = service.start_drill(session, user_id=ME, question_id=1)
    ts.max_rounds = 2
    session.commit()
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未涉及"), (3, "未涉及")], "继续", finish=False),
        _round_reply([(1, "未涉及"), (2, "命中"), (3, "未涉及")], "继续", finish=False),
    )
    service.submit_answer(session, ts=ts, answer_text="a", llm=llm)
    second = service.submit_answer(session, ts=ts, answer_text="b", llm=llm)
    assert second.finished is True
    assert ts.status == "finished"


def test_two_rounds_without_new_hits_finishes_early(session: Session) -> None:
    """§3.4 的成本保护：在明显不会的点上不要耗轮数。"""
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        _round_reply([(1, "未命中"), (2, "未涉及"), (3, "未涉及")]),
        _round_reply([(1, "未命中"), (2, "未涉及"), (3, "未涉及")]),  # 没有新命中
    )
    first = service.submit_answer(session, ts=ts, answer_text="a", llm=llm)
    assert first.finished is False, "第一轮不该因为这条收尾（那时还没有「上一轮」可言）"
    second = service.submit_answer(session, ts=ts, answer_text="b", llm=llm)
    assert second.finished is True


def test_only_a_real_true_asks_for_finish() -> None:
    """`should_finish` 必须**严格**判 `is True`（实测：模型会回字符串 `"false"`）。

    `bool("false")` 是 True —— 于是一场 6 个额度点的面试在第一轮就收尾，
    买到的只有一个考察点。宁可漏一次"模型想收尾"：多问一轮的代价小得多。
    """
    criteria = [Criterion(id=1, seq=1, text="随便一条", point_id=1)]
    for raw, expected in [
        (True, True),
        ("false", False),
        ("true", False),
        (1, False),
        (None, False),
    ]:
        _, model_finish = service._parse_round(
            {"hits": [{"criterion_id": 1, "status": "命中"}], "should_finish": raw}, criteria
        )
        assert model_finish is expected, f"should_finish={raw!r}"


def test_a_quoted_false_does_not_finish_the_round(session: Session) -> None:
    """同一条规则的端到端版本：那条真实回复要真的走完 `run_round`。"""
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未涉及"), (3, "未涉及")], finish="false")
    )
    result = service.submit_answer(session, ts=ts, answer_text="a", llm=llm)

    assert result.finished is False, '模型给 "should_finish": "false" 时不该收尾'
    assert ts.status == "active", "会话还得是活的 —— 否则用户再也答不了这一题"


def test_a_finished_session_refuses_more_rounds(session: Session) -> None:
    """收尾之后**不能再答**：实测重放能把 `attempts` 顶过 `max_rounds`，而每轮一次真调用。"""
    ts = service.start_drill(session, user_id=ME, question_id=1)
    ts.max_rounds = 1
    session.commit()
    # 只准备**一条**回复：闸门要是没拦住，第二轮会以"队列空了"的形式炸在这里，
    # 而不是把一次模型调用花出去 —— 于是这条测试同时钉住"闸门在调模型之前"。
    llm = FakeLLM().queue(_round_reply([(1, "命中"), (2, "未涉及"), (3, "未涉及")]))
    assert service.submit_answer(session, ts=ts, answer_text="a", llm=llm).finished is True

    with pytest.raises(InvalidInput):
        service.submit_answer(session, ts=ts, answer_text="重放", llm=llm)

    assert len(service.attempts_of(session, ts.id)) == 1
    assert len(llm.calls) == 1, "闸门必须发生在调模型之前（每次调用都是钱）"
    assert not llm.replies, "预录回复不该被消费掉"


def test_a_duplicate_round_number_is_a_clean_conflict(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发提交同一轮：第二个要以**人话**失败，不是 500。

    复现方式是把 `next_round_no` 钉死成"陈旧的读" —— 这正是并发那一刻的样子：
    两边都在对方写库之前读到同一个轮次号，于是第二个的 INSERT 撞
    `UNIQUE(session_id, round_no)`。修之前它作为 `IntegrityError` 逃出路由 → 500。
    """
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未涉及"), (3, "未涉及")]),
        _round_reply([(1, "未涉及"), (2, "命中"), (3, "未涉及")]),
    )
    service.submit_answer(session, ts=ts, answer_text="a", llm=llm)
    session.commit()

    monkeypatch.setattr(service, "next_round_no", lambda *a, **k: 1)
    with pytest.raises(InvalidInput):
        service.submit_answer(session, ts=ts, answer_text="并发的第二份", llm=llm)

    assert len(service.attempts_of(session, ts.id)) == 1, "冲突的那一轮不该留半行"
    # 会话必须还能用：撞了唯一约束之后没回滚的话，后面每一次 flush 都会继续炸
    monkeypatch.undo()
    assert service.submit_answer(session, ts=ts, answer_text="再答一次", llm=llm).attempt_id


def test_rules_are_pinned_directly() -> None:
    """规则本身也逐条钉住 —— 它们比编排流程更该被测透，且不需要库与模型。"""
    # 分数合成 .3/.3/.2/.2 + 四舍五入
    assert rules.total_score({"accuracy": 80, "completeness": 80, "clarity": 80, "depth": 80}) == 80
    assert rules.total_score({"accuracy": 100, "completeness": 0, "clarity": 0, "depth": 0}) == 30
    # 越界/非数值一律钳到 0（§4.3：容错优先于严格）
    assert rules.normalize_scores({"accuracy": 999, "completeness": -5, "clarity": "x"}) == {
        "accuracy": 100,
        "completeness": 0,
        "clarity": 0,
        "depth": 0,
    }
    assert rules.normalize_scores(None) == {"accuracy": 0, "completeness": 0, "clarity": 0, "depth": 0}
    # 累积快照的合并方向
    prev = rules.HitSnapshot({1: "命中", 2: "未命中"})
    assert prev.merge({1: "未涉及", 2: "命中", 3: "未涉及"}).statuses == {
        1: "命中",
        2: "命中",
        3: "未涉及",
    }


# ---------------------------------------------------------------------------
# 判分
# ---------------------------------------------------------------------------
def test_evaluate_session_writes_scores_and_review(session: Session) -> None:
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未涉及")]),
        _eval_reply(accuracy=80, completeness=80, clarity=80, depth=80, review="可见性答对了"),
    )
    service.submit_answer(session, ts=ts, answer_text="a", llm=llm)
    result = service.evaluate_session(session, ts=ts, llm=llm)

    assert result.status == "ok"
    assert result.total_score == 80
    assert "可见性答对了" in result.review
    row = session.query(Evaluation).filter(Evaluation.session_id == ts.id).one()
    assert row.status == "ok" and row.total_score == 80


def test_evaluate_session_is_idempotent(session: Session) -> None:
    """收尾可以被调用两次（页面层就会）—— 第二次**覆盖**同一行，不是追加第二行。

    迁移 0005 之前它每次 INSERT 一行，而两行会让 `GET /me/export` 的
    `scalar_one_or_none()` 抛 `MultipleResultsFound`：那个用户的导出**永久 500**。
    """
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未涉及")]),
        _eval_reply(accuracy=50, review="第一次"),
        _eval_reply(accuracy=90, review="第二次"),
    )
    service.submit_answer(session, ts=ts, answer_text="a", llm=llm)

    first = service.evaluate_session(session, ts=ts, llm=llm)
    second = service.evaluate_session(session, ts=ts, llm=llm)
    assert first.review == "第一次" and second.review == "第二次"

    rows = session.query(Evaluation).filter(Evaluation.session_id == ts.id).all()
    assert len(rows) == 1, "一个题会话只能有一条最终评分（UNIQUE(session_id)，决策 89）"
    assert rows[0].review == "第二次", "第二次收尾要真的把新判定写进去"
    assert rows[0].total_score == second.total_score


def test_evaluate_failure_is_persisted_not_silent(session: Session) -> None:
    """§4.4 继承：判分失败也要落库 —— v1 的"会话永远停在判分中"就是没做到这条。

    断言点选在**库里有一行 status='failed'**，而不是"函数没抛异常"。
    """
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        _round_reply([(1, "未命中"), (2, "未涉及"), (3, "未涉及")]),
        FakeReply(error=LLMCallError("模型挂了")),
    )
    service.submit_answer(session, ts=ts, answer_text="a", llm=llm)
    result = service.evaluate_session(session, ts=ts, llm=llm)

    assert result.status == "failed"
    row = session.query(Evaluation).filter(Evaluation.session_id == ts.id).one()
    assert row.status == "failed"
    assert row.total_score == 0
    assert "判分失败" in row.review


# ---------------------------------------------------------------------------
# 转写容错（决策 85）
# ---------------------------------------------------------------------------
def test_voice_round_tells_the_model_the_words_came_from_speech(session: Session) -> None:
    """语音轮的转写错字**不算他答错** —— 而这句话必须真的进 prompt。

    ADR-0009 把「下游模型本就能读通」当作不设确认环节的理由，而那条假设只在
    **错得读不通**时成立：「拉格」= RAG 这种同音错字的句子是通的，不说，模型就照
    错字判成概念错误 —— 而那个判定会进掌握度矩阵（记的是识别的错）。
    """
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(_round_reply([(1, "命中"), (2, "未涉及"), (3, "未涉及")]))
    service.submit_answer(
        session,
        ts=ts,
        answer_text="靠内存屏障做的",
        llm=llm,
        input_mode="voice",
        stt_text="靠内存平障做的",
    )

    round_prompt = llm.last_prompt()
    assert "语音转写" in round_prompt, "判分那一段必须知道这是转写来的"
    assert "同音" in round_prompt, "说清错在哪一类字上，模型才敢还原"
    assert "不补他没说的内容" in round_prompt, (
        "宁松勿严的边界必须跟提示一起给出 —— 少了它，每个考察点都能被「猜」成命中"
    )

    # 四维分那条 prompt 要带同一段说明：它评的 accuracy 正是被错字污染的那一维
    #
    # ⚠️ 先断言 `status == "ok"`：`render()` 在**缺占位符时会抛 LLMError**，而
    # `evaluate_session` 把它降级成 status='failed' —— 那样模型根本没被调用，
    # `last_prompt()` 拿到的还是上一轮的 prompt，单看"提示里有没有这句话"会**假绿**。
    llm.on("eval", _eval_reply())
    result = service.evaluate_session(session, ts=ts, llm=llm)
    assert result.status == "ok", "判分失败说明这段提示没渲染成（asr_note 少传就会这样）"
    eval_prompt = llm.last_prompt()
    assert "四维分" in eval_prompt, "确认这是判分那条 prompt，不是上一轮的"
    assert "语音转写" in eval_prompt, "评 accuracy 时也得知道这是转写"


def test_typed_round_tells_the_model_there_is_no_transcription(session: Session) -> None:
    """打字作答**不放宽**（决策 85 的边界②）—— 那里的错字是他自己敲的。

    这条同时钉住"按模态分岔"这件事本身：两段说明共用一个渲染槽，谁都不能被
    写成两种模态都发。
    """
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(_round_reply([(1, "命中"), (2, "未涉及"), (3, "未涉及")]))
    service.submit_answer(session, ts=ts, answer_text="靠内存屏障", llm=llm)

    prompt = llm.last_prompt()
    assert "打字" in prompt, "打字轮要明确说没有转写这一环"
    assert "同音" not in prompt, "打字轮不该拿到那句容错说明"


# ---------------------------------------------------------------------------
# 降级不静默（AGENTS.md §3.1）
# ---------------------------------------------------------------------------
def test_llm_failure_records_the_round_as_not_covered(session: Session) -> None:
    """判定失败时：会话不中断、这一轮仍落库、全部记"未涉及"、并且**调用方知道**。"""
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(FakeReply(error=LLMCallError("超时")))
    result = service.submit_answer(session, ts=ts, answer_text="我的回答", llm=llm)

    assert result.llm_failed is True
    assert result.snapshot.statuses == {1: "未涉及", 2: "未涉及", 3: "未涉及"}
    assert session.query(Attempt).filter(Attempt.session_id == ts.id).count() == 1


def test_model_missing_criteria_or_bad_ids_is_tolerated_but_logged(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """模型的输出不完美时：**不整轮失败，也不静默**。

    · 漏掉的考察点 → 记"未涉及"（语义最保守）并记 warning
    · 未知 criterion_id → 丢弃并记 warning
    · 非法 status → 记"未涉及"并记 warning
    """
    ts = service.start_drill(session, user_id=ME, question_id=1)
    llm = FakeLLM().queue(
        round_reply(
            hits=[(1, "命中"), (99, "命中"), (2, "半对")],  # 未知 id / 非法状态
            prose="继续",
        )
    )
    with caplog.at_level("WARNING"):
        result = service.submit_answer(session, ts=ts, answer_text="a", llm=llm)

    assert result.snapshot.statuses == {1: "命中", 2: "未涉及", 3: "未涉及"}
    text = caplog.text
    assert "不存在的考察点" in text
    assert "非法状态" in text
    assert "漏了" in text


# ---------------------------------------------------------------------------
# 越权
# ---------------------------------------------------------------------------
def test_get_session_row_rejects_other_users_session(session: Session) -> None:
    """`sessions` 自己没有 user_id，归属靠 `interviews` join —— 少了它，改 URL 就能答别人的题。"""
    from app.db.models import Interview
    from app.db.models import Session_ as InterviewSession

    session.add(
        Interview(id=500, user_id=OTHER, mode="drill", status="active", quota_charged=1)
    )
    session.add(
        InterviewSession(id=600, interview_id=500, question_id=1, seq=1, status="active", max_rounds=3)
    )
    session.commit()

    with pytest.raises(NotFound):
        service.get_session_row(session, 600, user_id=ME)
    # 本人取得到
    mine = service.start_drill(session, user_id=ME, question_id=1)
    assert service.get_session_row(session, mine.id, user_id=ME).id == mine.id


def test_bank_repository_still_enforces_visibility_for_interview(session: Session) -> None:
    """面试域拿题也必须经过 bank 的可见性入口 —— 否则可以对别人的私有题开面试。"""
    session.add(
        Question(
            id=3,
            kind="knowledge",
            stem="私有题",
            difficulty=3,
            owner_user_id=OTHER,
            visibility="private",
            origin="generated",
        )
    )
    session.commit()
    assert bank_repository.find_question(session, 3, ME) is None
    assert account_repository.find_user_by_email(session, "me@local") is not None
