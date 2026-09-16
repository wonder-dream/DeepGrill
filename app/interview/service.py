"""面试领域的业务规则：建面试、记一轮、判一道题（ADR-0005：`db` 第一个参数）。

这里住三件事，每一件都有明确的边界：

1. **建会话**（`start_interview` / `start_drill`）—— 扣额度点、落 `interviews` 与
   `sessions`。**先扣点再建**：额度不足时不该留下半场面试。
2. **记一轮**（`submit_answer`）—— 调面试官、把命中快照落库、必要时收尾。
3. **判一道题**（`evaluate_session`）—— 四维分 + 评语，写 `evaluations`。

⚠️ 一条从 v1 继承的具体 bug 警告（`docs/v1行为规格.md` §3.6）：
**取"上一轮的命中快照"必须在本轮写库之前查**，否则读到的是本轮自己 ——
于是"这一轮有没有新命中"永远算成 0，追问会在第二轮就停。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.db.models import Attempt, Evaluation, Interview, Question
from app.db.models import Session_ as InterviewSession
from app.errors import InvalidInput, NotFound
from app.interview import rules
from app.interview.rules import HitSnapshot
from app.llm import LLMError, ProseFilter, prompts, split_prose_and_json

logger = logging.getLogger(__name__)

#: 一场模拟面试的题数（MVP）。追问上限按题给（ADR-0001：编排参数，不由难度推导）。
INTERVIEW_QUESTION_COUNT = 3
DEFAULT_MAX_ROUNDS = 3

#: 题会话已经收尾（或整场面试已放弃）时再提交一轮的提示。**只有一处措辞**：
#: 非流式那条路抛异常、流式那条路回 JSON，两条路用同一句话（两处各写一句，
#: 迟早会分叉成"两个意思"）。
SESSION_FINISHED_MESSAGE = "这道题已经答完了 —— 刷新页面看看结果"

#: 还有一场没结束的面试时，第二次开场的拒绝文案（决策 97）。
UNFINISHED_MESSAGE = "你还有一场面试没结束 —— 先把它答完，或者放弃它"

#: 已经放弃的面试里再提交一轮的提示（决策 97）。
ABANDONED_MESSAGE = "这场面试已经放弃了 —— 它不会再继续"

#: 存进 `attempts.llm_error` 的原因长度上限。模型侧的错误消息本身已经截断过一次
#: （响应正文最多 200 字），这里再兜一道 —— 一列诊断信息不该能顶大一行。
_FAILURE_REASON_MAX = 500


def require_active(session: Session, ts: InterviewSession) -> None:
    """这一轮还能不能答。不能就抛 `InvalidInput`（预期内，不是 500）。

    为什么需要这道闸：`sessions.status` 收尾之后，页面上的表单**照样能再提交**
    （旧标签页、后退、双击）。实测重放能把 `attempts` 顶过 `max_rounds`，而每一轮
    都是**一次真的模型调用** —— 既花钱，又让"这场面试问了几轮"变成不可信的数字。
    控制流在代码手里（ADR-0001）：达没达成"结束"，不该由按钮还在不在决定。

    ⚠️ **两道闸缺一不可**（决策 97）：`sessions.status` 管"这道题答完了"，
    `interviews.status` 管"整场被放弃了"。只看前者的话，放弃一场之后旧标签页仍然
    能接着答 —— 每一轮照样是一次真的模型调用，而用户以为自己已经放弃了它。

    ⚠️ 权威判定在 `run_round()` 自己（服务层）。流式那两条路额外**在开始流之前**
    调一次，因为状态码在流开始之后就改不了了。
    """
    if ts.status != "active":
        raise InvalidInput(SESSION_FINISHED_MESSAGE)
    interview = session.get(Interview, ts.interview_id)
    if interview is not None and interview.status != "active":
        raise InvalidInput(ABANDONED_MESSAGE)


@dataclass
class RoundResult:
    """一轮的结果：判定 + 下一问 + 是否收尾。"""

    attempt_id: int
    snapshot: HitSnapshot
    new_hits: int
    followup: str
    finished: bool
    llm_failed: bool = False


@dataclass
class EvaluationResult:
    scores: dict[str, int]
    total_score: int
    review: str
    status: str  # ok / failed


# ---------------------------------------------------------------------------
# 建面试
# ---------------------------------------------------------------------------
def start_drill(session: Session, *, user_id: int, question_id: int) -> InterviewSession:
    """单题追问（1 个额度点）。返回**题会话**（`sessions` 那一层）。"""
    question = bank_repository.find_question(session, question_id, user_id)
    if question is None:
        raise NotFound("这道题不存在或不可见")
    interview = _create_interview(session, user_id=user_id, mode="drill", question_ids=[question_id])
    return sessions_of(session, interview.id)[0]


def start_interview(
    session: Session, *, user_id: int, question_ids: list[int] | None = None
) -> Interview:
    """模拟面试（6 个额度点）：抽多题 → 逐题提问 → 每题追问。

    `question_ids` 给了就用它（用户自选）；没给就在**可见范围内**抽最近几道。
    抽题只走 `bank` 的仓储 —— 于是私有题的隔离规则自动生效。
    """
    if question_ids:
        usable: list[int] = []
        for qid in question_ids:
            if bank_repository.find_question(session, qid, user_id) is None:
                raise NotFound(f"题目 {qid} 不存在或不可见")
            usable.append(qid)
    else:
        rows, _ = bank_repository.list_questions(
            session, user_id, limit=INTERVIEW_QUESTION_COUNT
        )
        # 仓储按 id 倒序取"最近的"，这里再翻正 —— 让第 1 题真的排在第 1 位。
        # 不翻正也能跑，但计划里写的是 `[2, 1]` 而题会话的 seq 是 1、2：
        # 于是"第 1 题"在页面上显示的是 id=2 的那道，读日志与读页面会得出不同结论
        # （实测为此排查了一轮）。顺序是产品语义的一部分，不该由查询的排序默认值决定。
        usable = [q.id for q in reversed(rows)]
    if not usable:
        raise InvalidInput("题库里还没有可用的题，先跑 python -m app.cli seed")

    return _create_interview(
        session, user_id=user_id, mode="interview", question_ids=usable[:INTERVIEW_QUESTION_COUNT]
    )


def _create_interview(
    session: Session, *, user_id: int, mode: str, question_ids: list[int]
) -> Interview:
    from app.account import service as account

    # 一场没结束就不许开第二场（决策 97）。**必须在这里拦**：它是两条开场路
    # （`start_interview` / `start_drill`）唯一的汇合点，而漏掉任何一条 = 规则形同
    # 不存在。也要在**扣点之前**拦 —— 否则"没开成还扣了 6 点"是第二种坏结果。
    if unfinished_interview(session, user_id) is not None:
        raise InvalidInput(UNFINISHED_MESSAGE)

    cost = account.spend_units(session, user_id, mode)  # 额度不足会抛 QuotaExhausted
    interview = Interview(
        user_id=user_id,
        mode=mode,
        plan={"question_ids": question_ids, "max_rounds": DEFAULT_MAX_ROUNDS},
        status="active",
        quota_charged=cost,
    )
    session.add(interview)
    session.flush()

    for seq, qid in enumerate(question_ids, start=1):
        session.add(
            InterviewSession(
                interview_id=interview.id,
                question_id=qid,
                seq=seq,
                status="active",
                max_rounds=DEFAULT_MAX_ROUNDS,
            )
        )
    session.flush()
    return interview


# ---------------------------------------------------------------------------
# 记一轮
# ---------------------------------------------------------------------------
def submit_answer(
    session: Session,
    *,
    ts: InterviewSession,
    answer_text: str,
    llm,
    input_mode: str = "text",
    stt_text: str | None = None,
) -> RoundResult:
    """答一轮（**非流式**那条路：表单提交）。把事件流跑完，只取结果。

    真正干活的是 `run_round()`；这里只是"不要那些增量"。两条入口共用同一份实现，
    是这一笔里最重要的一件事 —— 抄一遍"判定 → 落库 → 收尾"的代码，就是又一处
    会漂的地方（收尾判据改一次要改两处，而漏改的那处不会报错）。
    """
    result: RoundResult | None = None
    for event in run_round(
        session,
        ts=ts,
        answer_text=answer_text,
        llm=llm,
        input_mode=input_mode,
        stt_text=stt_text,
    ):
        if isinstance(event, RoundResult):
            result = event
    assert result is not None, "run_round 必须以 RoundResult 收尾（这是它的契约）"
    return result


def run_round(
    session: Session,
    *,
    ts: InterviewSession,
    answer_text: str,
    llm,
    input_mode: str = "text",
    stt_text: str | None = None,
    stream: bool = False,
) -> Iterator[str | RoundResult]:
    """一轮的完整过程，**边产出面试官的话、边把这一轮落库**。

    产出的东西按顺序：先是面试官那句话的**增量**（`str`，可能多段），最后是一个
    `RoundResult`。流式的端点把增量发成 SSE，非流式的端点把它们丢掉。

    `input_mode` / `stt_text` 是决策 32/33 的两列：语音轮次里 `stt_text` 是**转写
    原文**、`answer_text` 是**实际送进判分的文本**。两列分开存的理由写在
    `migrations/0001_initial.sql` 的 `attempts` 注释里（清晰度要有原始素材），而
    "不设确认环节"意味着它们**通常相同**。录音本身不经过这里（决策 33）。
    它同时决定判分 prompt 里那句**转写容错**（决策 85）：语音轮里被写错的术语
    不算他答错，语音这条链上的识别错误不该记进掌握度矩阵。

    ⚠️ **先取上一轮快照，再写本轮**（§3.6 的具体 bug，重写极易再犯）。
    """
    require_active(session, ts)
    previous = current_snapshot(session, ts.id)  # ← 必须在写库之前
    round_no = next_round_no(session, ts.id)

    question = session.get(Question, ts.question_id)
    if question is None:
        raise NotFound("这道题的题目行不见了")

    criteria = bank_repository.criteria_of_question(session, question)
    history = transcript(session, ts.id)
    prompt = prompts.load("interviewer/score_round.md").render(
        stem=question.stem,
        criteria=_criteria_block(criteria),
        history=history or "（这是第一轮）",
        answer=answer_text or "（候选人没有作答）",
        asr_note=_asr_note(input_mode),
    )

    decision_failed = False
    #: 判定失败的原因（迁移 0006）。**只进库、不进 prompt** —— 它是给排查用的。
    failure_reason: str | None = None
    prose = ""
    try:
        if stream:
            # 逐段放行"分隔行之前"的内容 —— 结构（JSON）一个字节都不给用户看
            prose_filter = ProseFilter()
            raw_parts: list[str] = []
            for chunk in llm.stream([{"role": "user", "content": prompt}]):
                raw_parts.append(chunk)
                visible = prose_filter.feed(chunk)
                if visible:
                    yield visible
            tail = prose_filter.finish()
            if tail:
                yield tail
            raw = "".join(raw_parts)
        else:
            raw = llm.chat([{"role": "user", "content": prompt}]).text

        prose, data = split_prose_and_json(raw)
        updates, model_finish = _parse_round(data, criteria)
    except LLMError as e:
        # 降级可以，静默不行（AGENTS.md §3.1）：这一轮记为"全部未涉及"、
        # 给出固定提示、并把 llm_failed 交给调用方展示。会话**不中断**。
        #
        # ⚠️ 这里必须**显式把全部考察点写进快照**，不能只留一个空 updates：
        # 空字典合并进累积快照等于"这一轮什么都没记"，而"考了没答"必须能被
        # 表达（掌握度矩阵的分母靠它，见 rules.HitSnapshot 的注释）。
        #
        # 原因**同时**写进 `attempts.llm_error`（迁移 0006）：只写日志的话，这一行
        # 与"模型正常但一条都没答到"在库里长得一模一样（§3.1 要可查询的记录）。
        logger.warning("第 %d 轮判定失败：%s", round_no, e)
        decision_failed = True
        failure_reason = str(e)[:_FAILURE_REASON_MAX]
        updates = {c.id: rules.NOT_COVERED for c in criteria}
        prose = "（面试官这轮没接上，继续说你的思路就好）"
        model_finish = False
        if stream:
            # 流式路径下用户已经盯着屏幕等了：这句话必须**发出去**，否则那一轮
            # 看起来什么都没发生。非流式路径不需要（页面上会重新渲染整页）。
            yield prose

    snapshot = previous.merge(updates)
    new_hits = rules.count_new_hits(previous, snapshot)

    attempt = Attempt(
        session_id=ts.id,
        round_no=round_no,
        is_followup=1 if round_no > 1 else 0,
        input_mode=input_mode,
        stt_text=stt_text,
        answer_text=answer_text,
        feedback_text=prose,
        hits=snapshot.to_json(),
        llm_error=failure_reason,
    )
    session.add(attempt)
    try:
        session.flush()
    except IntegrityError as e:
        # 同一轮被提交了两次（双击 / 两个标签页同时发）：两边的 `next_round_no()`
        # 都读到同一个号，第二个到这里的 INSERT 会撞 `UNIQUE(session_id, round_no)`。
        # 实测这条路以前以 **500** 逃出去 —— 但"这一轮已经在别处记下了"是**预期内**
        # 的失败（同一次点击的第二次），该给人话。
        #
        # 回滚是必要的：会话上已经没有可以提交的东西（本轮只加了这一行 attempt），
        # 而一个"脏"会话会让依赖它的收尾钩子在提交时再炸一次。
        session.rollback()
        raise InvalidInput("这一轮已经在别处提交过了 —— 刷新页面看看结果") from e

    finished = rules.should_finish(
        round_no=round_no,
        max_rounds=ts.max_rounds,
        previous=previous,
        current=snapshot,
        model_suggests_finish=model_finish,
    )
    if finished:
        ts.status = "finished"

    # ⚠️ 必须是 `yield` 而不是 `return`：这个函数现在是**生成器**，`return X` 会变成
    # `StopIteration(X)` —— 调用方拿不到结果（第一版就是这么写的，13 个测试当场变红）。
    yield RoundResult(
        attempt_id=attempt.id,
        snapshot=snapshot,
        new_hits=new_hits,
        followup=prose,
        finished=finished,
        llm_failed=decision_failed,
    )


#: 客户端在 SSE 中途断掉时，那一轮落库的反馈文案与失败原因。**同一个字符串是有意的**：
#: 页面上看到的话与库里查到的原因必须是同一句（两处各写一遍迟早会分叉）。
ABORTED_ROUND_MESSAGE = "（这一轮在生成过程中断了 —— 没有判分结果，刷新页面继续）"


def record_aborted_round(
    session: Session,
    *,
    session_id: int,
    answer_text: str,
    input_mode: str = "text",
    stt_text: str | None = None,
    reason: str = ABORTED_ROUND_MESSAGE,
) -> Attempt | None:
    """把"生成途中断掉"的那一轮按**降级**落库（§3.1：不许整轮静默消失）。

    实测（第 1 轮 probe16 / probe19 都复现）：SSE 收到第一段话之后客户端关掉连接 ——
    那一轮在库里**一点痕迹都没有**（`attempts` 为空、会话还停在上一轮）。用户"答了却
    像没答"，报告与掌握度也当它没发生过。这与其它降级路径（模型挂了、输出不可解析）
    应当一致：全部记"未涉及"、把原因写进 `llm_error`（迁移 0006），会话照旧可以继续答。

    ⚠️ 它**不推进收尾判据**：这一轮没有判定结果，`should_finish` 无从谈起。
    代价是轮次号被占掉一个（`UNIQUE(session_id, round_no)`）—— 与"模型调用失败也要
    记一轮"是同一种取舍：那次尝试真的发生过。

    ⚠️ 只在会话仍是 `active`、题还在时写；撞了唯一约束就放弃（另一份请求已经记下了
    这一轮，这里不该抢，也不该让异常逃出去）。
    """
    ts = session.get(InterviewSession, session_id)
    if ts is None or ts.status != "active":
        return None
    question = session.get(Question, ts.question_id)
    criteria = bank_repository.criteria_of_question(session, question) if question else []
    previous = current_snapshot(session, session_id)
    snapshot = previous.merge({c.id: rules.NOT_COVERED for c in criteria})
    round_no = next_round_no(session, session_id)
    attempt = Attempt(
        session_id=session_id,
        round_no=round_no,
        is_followup=1 if round_no > 1 else 0,
        input_mode=input_mode,
        stt_text=stt_text,
        answer_text=answer_text,
        feedback_text=ABORTED_ROUND_MESSAGE,
        hits=snapshot.to_json(),
        llm_error=reason,
    )
    session.add(attempt)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return attempt


def _criteria_block(criteria) -> str:
    if not criteria:
        return "（这道题还没有考察点定义 —— 按题干自己判断要点）"
    return "\n".join(f"{c.id}. {c.text}" for c in criteria)


def _parse_round(data: object, criteria) -> tuple[dict[int, str], bool]:
    """把模型的 JSON 解析成 `(判定, 是否建议收尾)`。

    ⚠️ **不再从 JSON 里取面试官那句话**：两段式回复里那句话在分隔行**前面**
    （`split_prose_and_json` 拆出来的 `prose`）。放两份会不一致 —— 而流式显示的
    是前面那一份，写进 `attempts.feedback_text` 的却是后面那一份。

    容忍度是刻意的：模型可能少给一条考察点、给未知的 `criterion_id`、给出非法
    `status`。这些都不该让整轮失败 —— **但也不该静默**：漏掉的考察点按
    `未涉及` 记（那是"没问到"，语义上最保守），未知 id 丢弃并记 warning。
    """
    known = {c.id for c in criteria}
    updates: dict[int, str] = {}
    payload = data if isinstance(data, dict) else {}
    for item in payload.get("hits") or []:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("criterion_id")
        if raw_id is None:
            continue
        try:
            cid = int(raw_id)
        except (TypeError, ValueError):
            continue
        status = str(item.get("status") or "")
        if cid not in known:
            logger.warning("模型给了一个不存在的考察点 id：%s", cid)
            continue
        if status not in rules.STATUSES:
            logger.warning("模型给了非法状态 %r（考察点 %s），按未涉及记", status, cid)
            status = rules.NOT_COVERED
        updates[cid] = status

    missing = known - set(updates)
    for cid in missing:
        updates[cid] = rules.NOT_COVERED
    if missing:
        logger.warning("模型漏了 %d 条考察点，按未涉及记：%s", len(missing), sorted(missing))

    # ⚠️ **严格判 `is True`**，不是 `bool(...)`：模型真的会回字符串 `"false"`
    # （实测：引号包着的布尔），而 `bool("false")` 是 True —— 于是第一轮就收尾，
    # 6 个额度点只买到一轮。宁可不收尾：多问一轮的代价远小于白瞎一场面试。
    return updates, payload.get("should_finish") is True


# ---------------------------------------------------------------------------
# 判一道题
# ---------------------------------------------------------------------------
def evaluate_session(session: Session, *, ts: InterviewSession, llm) -> EvaluationResult:
    """题会话级的**最终**评分（不是逐轮评分），写 `evaluations`。

    失败**也要落库**（`status='failed'`）—— §4.4 标为继承：v1 的"会话永远停在
    判分中"就是因为失败没有落库也没有可见状态。

    **一个题会话只有一行**（`UNIQUE(session_id)`，决策 89）：收尾是幂等的
    ——`finish_interview()` 允许被重复调用（页面层就会），于是这里必须 **upsert**。
    以前每次收尾都 INSERT 一行，第二行会让 `GET /me/export` 的
    `scalar_one_or_none()` 抛 `MultipleResultsFound` ⇒ **那个用户的导出永久 500**
    （实测：重放一轮之后收尾两次即可复现）。
    """
    question = session.get(Question, ts.question_id)
    criteria = bank_repository.criteria_of_question(session, question) if question else []
    snapshot = current_snapshot(session, ts.id)
    mode = _input_mode(session, ts.id)

    scores = dict.fromkeys(rules.DIMENSIONS, 0)
    review = "判分失败，这道题没有得分记录。"
    status = "failed"
    try:
        data, _ = llm.chat_json(
            [
                {
                    "role": "user",
                    "content": prompts.load("interviewer/evaluate_session.md").render(
                        stem=question.stem if question else "",
                        criteria=_snapshot_block(criteria, snapshot),
                        history=transcript(session, ts.id, include_answers=True),
                        clarity_hint=_clarity_hint(mode),
                        asr_note=_asr_note(mode),
                    ),
                }
            ]
        )
        payload = data if isinstance(data, dict) else {}
        scores = rules.normalize_scores(payload.get("scores"))
        review = str(payload.get("review") or "").strip() or "（模型没有给出评语）"
        status = "ok"
    except LLMError as e:
        logger.warning("题会话 %s 判分失败：%s", ts.id, e)

    evaluation = session.execute(
        select(Evaluation).where(Evaluation.session_id == ts.id)
    ).scalar_one_or_none()
    if evaluation is None:
        evaluation = Evaluation(session_id=ts.id)
        session.add(evaluation)
    # 重算**覆盖**同一行（不是追加）：唯一索引管的是"不可能有两行"，
    # 这里管的是"第二次收尾要真的把新判定写进去"。
    evaluation.scores = scores
    evaluation.total_score = rules.total_score(scores)
    evaluation.review = review
    evaluation.status = status
    session.flush()
    return EvaluationResult(
        scores=scores,
        total_score=rules.total_score(scores),
        review=review,
        status=status,
    )


def _clarity_hint(mode: str) -> str:
    """`clarity` 是四维里唯一含义随输入模态变化的维度（ADR-0009）。"""
    if mode == "voice":
        return "本题是**语音作答**：clarity = 表达流畅、少口头禅（测的是说话）。"
    return "本题是**打字作答**：clarity = 结构清晰、有条理（测的是组织与排版）。"


def _asr_note(mode: str) -> str:
    """**转写容错**（决策 85）：语音轮的术语错字不算他答错。

    为什么必须显式写进 prompt：ADR-0009 把「下游模型本就能读通」当成"不设确认
    环节"的理由，而那条假设只在**错得读不通**时成立。「拉格」（RAG）、「哈西表」
    （哈希表）这类同音错字的句子是**通的** —— 不说，模型就照错字判成概念错误，
    而那个判定会进掌握度矩阵（记下来的是识别的错，不是候选人的水平）。

    ⚠️ 边界也写在提示里：**放宽的只有「字写错了」**。整段话里没有对应某条考察点
    的意思时仍记未命中 —— 少了这句，「宁松勿严」会退化成"每条考察点都能被猜成
    命中"，矩阵立刻失真。

    打字作答**不给**这道口子：那里的错字是他自己敲的，且提交前有机会改。
    """
    if mode == "voice":
        return (
            "候选人的话是**语音转写**出来的，而这是一场技术面试 —— 术语密集，"
            "同音/近音字被写错是常态（「红黑书」= 红黑树、「拉格」= RAG、"
            "「哈西表」= 哈希表、「萎曲」= 微服务）。判定时按**语音上说得通**"
            "去还原他的意思：\n"
            "① 只要某条考察点的术语**听起来可能是他说的那个词**、上下文也对得上，"
            "就算他提到了 —— 不要因为字写错就记未命中，也不要要求他写出正确写法；\n"
            "② 追问时用**正确的术语写法**，不要复读转写里的错词；\n"
            "③ ⚠️ 但**不补他没说的内容**：整段话里压根没有对应某条考察点的意思时，"
            "怎么还原都补不出来 —— 那条仍记 `未命中` / `未涉及`。"
            "放宽的只有「字写错了」，不是「他没答」。"
        )
    return "候选人的话是**打字**输入的，没有转写这一环 —— 术语写错就是他用错了，照常判。"


def _snapshot_block(criteria, snapshot: HitSnapshot) -> str:
    if not criteria:
        return "（这道题没有考察点定义）"
    lines = []
    for c in criteria:
        lines.append(f"{c.id}. {c.text} —— {snapshot.statuses.get(c.id, rules.NOT_COVERED)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 读辅助
# ---------------------------------------------------------------------------
def attempts_of(session: Session, session_id: int) -> list[Attempt]:
    return list(
        session.execute(
            select(Attempt).where(Attempt.session_id == session_id).order_by(Attempt.round_no)
        )
        .scalars()
        .all()
    )


def current_snapshot(session: Session, session_id: int) -> HitSnapshot:
    """**最后一行**就是当前快照 —— 累积快照让"此刻的状态"变成读一行（决策 28）。"""
    rows = attempts_of(session, session_id)
    if not rows:
        return HitSnapshot()
    return HitSnapshot.from_json(rows[-1].hits)


def next_round_no(session: Session, session_id: int) -> int:
    return len(attempts_of(session, session_id)) + 1


def _input_mode(session: Session, session_id: int) -> str:
    rows = attempts_of(session, session_id)
    return rows[-1].input_mode if rows else "text"


def transcript(session: Session, session_id: int, *, include_answers: bool = True) -> str:
    """把这一题的轮次铺成文字，喂给模型。

    `include_answers=False` 时不带候选人原话（追问决策只需要看判定），
    `True` 时带上（判分要看完整表述 —— §4.8「整轮统一判分」）。
    """
    lines: list[str] = []
    for a in attempts_of(session, session_id):
        if include_answers and a.answer_text:
            lines.append(f"[第 {a.round_no} 轮] 候选人：{a.answer_text}")
        if a.feedback_text:
            lines.append(f"[第 {a.round_no} 轮] 面试官：{a.feedback_text}")
    return "\n".join(lines)


def sessions_of(session: Session, interview_id: int) -> list[InterviewSession]:
    return list(
        session.execute(
            select(InterviewSession)
            .where(InterviewSession.interview_id == interview_id)
            .order_by(InterviewSession.seq)
        )
        .scalars()
        .all()
    )


def _sessions_of(session: Session, interview: Interview) -> list[InterviewSession]:
    return sessions_of(session, interview.id)


def get_session_row(session: Session, session_id: int, user_id: int) -> InterviewSession:
    """取一个题会话，并**校验它属于这位用户的面试**。

    越权在这里拦：`sessions` 自己没有 user_id，归属靠 `interviews`。少了这个 join
    条件，任何人改一下 URL 里的 id 就能答别人的题、看别人的判定。
    """
    row = session.execute(
        select(InterviewSession)
        .join(Interview, Interview.id == InterviewSession.interview_id)
        .where(InterviewSession.id == session_id, Interview.user_id == user_id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("这个会话不存在")
    return row


# ---------------------------------------------------------------------------
# 首页要用的两件事（决策 3 / 23）
# ---------------------------------------------------------------------------
def active_interviews(session: Session, user_id: int, *, limit: int = 5) -> list[Interview]:
    """**还没收尾**的面试 —— 首页的「继续未完成会话」（决策 23 的第二项）。

    `status='active'` 就是"没收尾"：`finished` 出过报告，`abandoned` 是用户主动
    放弃的。两种都不该出现在"继续"里 —— 把它们列出来会让人以为还有活要干。

    **没有回收者，也不需要**：它是一条按 user 过滤、带 limit 的查询，不往内存里
    存任何东西（AGENTS.md §3.2 管的是"进内存的东西"）。
    """
    return list(
        session.execute(
            select(Interview)
            .where(Interview.user_id == user_id, Interview.status == "active")
            .order_by(Interview.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def list_interviews(
    session: Session, user_id: int, *, limit: int | None = None
) -> list[Interview]:
    """这个人的面试记录，新的在前（决策 96）。

    `limit=None` = 全部。两种用法各有一处：`/me` 的摘要要 `limit=10`，而面试记录页
    （`/me/interviews`）要整页 —— 所以"取哪些"这件事留在领域里，页面只决定怎么摆。

    为什么**不能**按 `status` 过滤：记录页要的是"我面过哪些"，包括 `abandoned`
    （用户主动放弃的）—— 把它藏掉，用户会以为那场面试凭空消失了。要"继续哪一场"
    请用 `active_interviews`。

    没有回收者，也不需要：一条按 user 过滤、带 limit 的查询，不往内存里存东西。
    """
    stmt = (
        select(Interview)
        .where(Interview.user_id == user_id)
        .order_by(Interview.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars().all())


def resume_target(session: Session, user_id: int) -> tuple[Interview, InterviewSession] | None:
    """「继续面试」要跳去的那一页：这个人**最新的、还没答完的题会话**（决策 96）。

    一条查询就够 —— 不是"先取活跃面试、再逐个查它的会话"。后者在"有好几场活跃面试、
    前几场都答完了"时是 N+1，而这一项**每个页面都要跑**（它在侧边栏上）。

    返回 `(面试, 题会话)`；没有未完成的会话时返回 None（那时侧边栏不显示这个入口）。
    """
    row = session.execute(
        select(InterviewSession, Interview)
        .join(Interview, Interview.id == InterviewSession.interview_id)
        .where(
            Interview.user_id == user_id,
            Interview.status == "active",
            InterviewSession.status == "active",
        )
        .order_by(Interview.id.desc(), InterviewSession.seq)
        .limit(1)
    ).first()
    if row is None:
        return None
    return row[1], row[0]


def unfinished_interview(session: Session, user_id: int) -> Interview | None:
    """这个人**还没结束**的那一场面试（没有就 None）。用于"一场没结束不许开第二场"。

    与 `active_interviews(limit=1)` 是同一件事，只是调用点要的是一个对象或 None，
    而不是一个列表 —— 开场前的闸、错误页上的「继续 / 放弃」两个按钮都想知道它。
    """
    rows = active_interviews(session, user_id, limit=1)
    return rows[0] if rows else None


def abandon_interview(session: Session, *, user_id: int, interview_id: int) -> Interview:
    """放弃一场还没结束的面试（决策 97）。

    为什么必须有这条路：决策 97 只堵不疏的话，用户会被锁在一场他不想答完的面试里，
    而**每一轮追问都是一次真的模型调用** —— 那是花钱买罪受。放弃 = 把 `status` 置成
    `abandoned`：它不再出现在「继续未完成」里，「一场没结束不许开第二场」也随之松开。

    三件事刻意如此：

    · **不退还额度点**：扣点发生在开场时（题目已抽出、题会话已建），放弃是用户自己的
      选择。页面上的按钮必须把这句话写出来，否则就是静默的代价。
    · **不删记录**：它留在面试记录页上（决策 96）—— `abandoned` 是个状态，不是消失。
    · **幂等**：已经结束的（`finished` / `abandoned`）原样返回。双击、后退重放、
      两个标签页同时点，都不该变成一堵错误页。
    """
    from app.account import repository as account_repository

    interview = session.get(Interview, interview_id)
    if interview is None or interview.user_id != user_id:
        raise NotFound("这场面试不存在")
    if interview.status == "active":
        interview.status = "abandoned"
        interview.ended_at = account_repository.now_iso()
    return interview


def next_active_session(session: Session, interview_id: int) -> InterviewSession | None:
    """一场面试里**下一道还没答完的题** —— "继续"按钮要跳到的那一页。

    判据是"这一题的题会话还没收尾"（`sessions.status == 'active'`），不是"它答了
    几轮"：一轮都没答和答了但没收尾，都要跳回同一页（页面上本来就显示着已答的轮次）。
    """
    return session.execute(
        select(InterviewSession)
        .where(
            InterviewSession.interview_id == interview_id,
            InterviewSession.status == "active",
        )
        .order_by(InterviewSession.seq)
        .limit(1)
    ).scalar_one_or_none()


def answered_question_ids(session: Session, user_id: int) -> set[int]:
    """这位用户**答过**（答过至少一轮）的题目 id。

    用途只有一处：首页推荐的 `exclude_ids` —— 推荐一道刚答过的题没有意义。
    它跨了三张表（`attempts` → `sessions` → `interviews`），所以放在面试领域里，
    由首页装配层把结果递给 `bank`（决策：领域互不 import）。
    """
    rows = session.execute(
        select(InterviewSession.question_id)
        .join(Interview, Interview.id == InterviewSession.interview_id)
        .join(Attempt, Attempt.session_id == InterviewSession.id)
        .where(Interview.user_id == user_id)
        .distinct()
    ).all()
    return {r[0] for r in rows}
