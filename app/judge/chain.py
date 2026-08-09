"""M11 追问链：深挖式多轮面试对话状态机（Deep-Probe Chain）。

- 五层深度阶梯（L1 概念 → L2 原理 → L3 权衡 → L4 边界 → L5 横向），每轮追问 ≥ 上一层
- 回答质量驱动策略：correct 继续加深；wrong/unsure 连续 2 次判定"探到底"收尾
- 每轮落 attempts（round_no/is_followup/answer/feedback/level），resume 全量重建上下文（D18）
- 轮数上限 max_rounds（D15，默认 20）：LLM 可提前 finish，到限强制结束
- 单轮 LLM 失败重试 1 次，仍失败降级为固定提示"请继续"，不中断会话
- finish() 调 M10 判分（传最大追问层级 max_level，judge v4 按深度校准）并落库
"""
import logging
from datetime import datetime
from typing import TypedDict

from sqlalchemy import select

from ..db import commit, get_session
from ..errors import ChainStateError, LLMError, LLMJsonError
from ..models import Attempt, Judgment, Question, Session, SessionStatus
from .judge import judge

logger = logging.getLogger(__name__)

CHAIN_PROMPT_V3 = """你是资深面试官，正在对候选人进行**逐层深挖**的追问。目标不是考倒候选人，而是探测其思考深度与认知广度：答对就继续加深，直到探到其真实水平为止。

题目：{stem}
题型侧重：{qtype}
题目难度：{difficulty}/5（{difficulty_name}）
目标深度：本题目按难度分级追问——低难度题浅挖（概念/原理/权衡即可），高难度题深挖到底；达到 L{target_level} 即算证明充分，不必强求更深。
高分标准（good_criteria）：
{good}
扣分特征（bad_criteria）：
{bad}

深度阶梯（每轮追问必须比上一轮更深一层；同层可以更尖锐）：
L1 概念/定义：确认基础概念是否准确
L2 原理/机制：追问底层实现原理
L3 权衡/取舍：为什么这样设计而不是那样
L4 边界/反例/极端场景：什么情况下会失效
L5 横向联系/知识广度：与相关知识点/框架/场景的关联

对话历史：
{history}

候选人最新回答：
{answer}

每轮必须：
1. 先评估候选人本轮回答质量（quality）：
   - correct：准确且完整
   - partial：部分正确或有明显缺口
   - wrong：方向错误或答错
   - unsure：答不出、回避或明确说不会
2. 据此决定下一轮：
   - correct → 必须继续加深一层追问（除非已到目标深度 L{target_level} 且最近两轮均为 correct，才算证明充分）
   - partial → 同层追挖缺口，或降一层确认基础是否牢固
   - wrong/unsure → 若最近一轮同样是 wrong/unsure（连续 2 次答不出/答错），本轮收尾；否则再给一次机会确认

只输出 JSON，不要其他文字：
{{"action": "continue" 或 "finish", "followup": "下一轮追问或收尾语", "quality": "correct|partial|wrong|unsure", "level": 本轮追问所在层级 1-5}}

finish 只允许两种情况：连续 2 次 wrong/unsure 探到底；或已到目标深度 L{target_level} 且最近两轮均 correct 证明充分。其他情况一律 continue。"""

DEGRADED_HINT = "请继续"


class RoundResult(TypedDict):
    interviewer_message: str
    finished: bool


class ChainSession:
    def __init__(
        self,
        session_id: int,
        question: Question,
        llm,
        *,
        judge_model: str = "",
        max_rounds: int = 20,
        target_level: int = 5,
    ):
        self._session_id = session_id
        self._question = question
        self._llm = llm
        self._judge_model = judge_model
        self._max_rounds = max_rounds
        self._target_level = target_level
        self._finished = False
        self._max_level = 0

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def rounds_done(self) -> int:
        return len(self._attempts())

    def next_round(self, answer: str) -> RoundResult:
        """提交回答 → (interviewer_message, finished)；内部落 attempts 并驱动 LLM。"""
        if self._finished:
            raise ChainStateError("chain session already finished")
        round_no = self.rounds_done
        followup, action, level = self._ask_llm(answer)
        self._persist_attempt(
            round_no,
            is_followup=round_no > 0,
            answer=answer,
            feedback=followup,
            level=level,
        )
        if level is not None:
            self._max_level = max(self._max_level, level)
        if action == "finish" or round_no >= self._max_rounds - 1:
            self._finished = True
        return RoundResult(interviewer_message=followup, finished=self._finished)

    def finish(self, reference: str | None = None) -> Judgment:
        """结束追问链：调用 M10 整轮判分（带最大追问层级 + 可选参考）并落库；会话标记 finished。"""
        judgment = judge(
            self._question,
            self._transcript(),
            self._judge_model,
            self._llm,
            session_id=self._session_id,
            reference=reference,
            max_level=self._max_level or None,
        )
        with get_session() as session:
            session.add(judgment)
            row = session.get(Session, self._session_id)
            if row is not None:
                row.status = SessionStatus.finished
                row.ended_at = datetime.now()
            commit(session)
            session.refresh(judgment)
        self._finished = True
        return judgment

    def _ask_llm(self, answer: str) -> tuple[str, str, int | None]:
        messages = [{"role": "user", "content": self._build_prompt(answer)}]
        try:
            parsed = self._llm.complete(messages, json_schema={})
        except (LLMError, LLMJsonError):
            try:
                parsed = self._llm.complete(messages, json_schema={})
            except (LLMError, LLMJsonError) as e:
                logger.warning(
                    "chain round degraded for session %s: %s", self._session_id, e
                )
                return DEGRADED_HINT, "continue", None
        if not isinstance(parsed, dict):
            return DEGRADED_HINT, "continue", None
        action = parsed.get("action")
        followup = parsed.get("followup")
        if not isinstance(followup, str) or not followup.strip():
            followup = DEGRADED_HINT
        level = parsed.get("level")
        if not isinstance(level, int) or not 1 <= level <= 5:
            level = None
        return followup, action if action == "finish" else "continue", level

    def _build_prompt(self, answer: str) -> str:
        from ..difficulty import DIFFICULTY_NAMES

        history = "\n".join(self._history_lines()) or "（无）"
        return CHAIN_PROMPT_V3.format(
            stem=self._question.stem,
            qtype=self._question.type.value,
            difficulty=self._question.difficulty,
            difficulty_name=DIFFICULTY_NAMES.get(self._question.difficulty, "未知"),
            target_level=self._target_level,
            good="\n".join(f"- {c}" for c in self._question.good_criteria),
            bad="\n".join(f"- {c}" for c in self._question.bad_criteria),
            history=history,
            answer=answer,
        )

    def _history_lines(self) -> list[str]:
        lines = []
        for a in self._attempts():
            lines.append(f"候选人: {a.answer_text}")
            if a.feedback_text:
                lines.append(f"面试官: {a.feedback_text}")
        return lines

    def _transcript(self) -> list[dict]:
        transcript = []
        for a in self._attempts():
            transcript.append({"role": "user", "content": a.answer_text})
            if a.feedback_text:
                transcript.append({"role": "interviewer", "content": a.feedback_text})
        return transcript

    def _attempts(self) -> list[Attempt]:
        with get_session() as session:
            return list(
                session.scalars(
                    select(Attempt)
                    .where(Attempt.session_id == self._session_id)
                    .order_by(Attempt.round_no)
                )
            )

    def _persist_attempt(
        self, round_no: int, *, is_followup: bool, answer: str, feedback: str, level: int | None
    ) -> None:
        with get_session() as session:
            attempt = Attempt(
                session_id=self._session_id,
                round_no=round_no,
                is_followup=is_followup,
                answer_text=answer,
                feedback_text=feedback,
                level=level,
            )
            session.add(attempt)
            commit(session)


def resume(
    session_id: int,
    question: Question,
    llm,
    *,
    judge_model: str = "",
    max_rounds: int = 20,
    target_level: int = 5,
) -> ChainSession:
    """从 attempts 表重建上下文；会话已 finished 则恢复为终态（继续调用抛 ChainStateError）。"""
    chain = ChainSession(
        session_id,
        question,
        llm,
        judge_model=judge_model,
        max_rounds=max_rounds,
        target_level=target_level,
    )
    with get_session() as session:
        row = session.get(Session, session_id)
        if row is not None and row.status == SessionStatus.finished:
            chain._finished = True
        levels = session.scalars(
            select(Attempt.level).where(Attempt.session_id == session_id)
        ).all()
        chain._max_level = max([lvl for lvl in levels if lvl is not None] or [0])
    return chain
