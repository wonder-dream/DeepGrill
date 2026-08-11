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

CHAIN_PROMPT_V3 = """你是资深面试官，正在对候选人进行**深度适配的追问**。目标不是考倒候选人，而是按题目难度探测其思考深度：难度低的题答得完整清晰就结束，难度高的题才逐层深挖。

题目：{stem}
题型侧重：{qtype}
题目难度：{difficulty}/5（{difficulty_name}）
追问档位：{tier_note}
目标深度：{target_note}
高分标准（good_criteria）：
{good}
扣分特征（bad_criteria）：
{bad}

深度阶梯（每轮追问必须比上一轮更深一层；同层可以更尖锐）：
L1 概念/定义
L2 原理/机制
L3 权衡/取舍
L4 边界/反例/极端场景
L5 横向联系/知识广度

对话历史：
{history}

候选人最新回答：
{answer}

{knowledge_section}

每轮必须：
1. 先评估本轮回答的两个维度：
   - quality：correct（方向正确且基本准确）/ partial（部分正确或有明显缺口）/ wrong（方向错误或答错）/ unsure（答不出、回避或明确说不会）
   - completeness：complete（完整覆盖高分标准 good_criteria 的全部要点）/ partial（只覆盖部分要点）/ incomplete（基本没覆盖要点）
2. 据此决定下一轮（严格按追问档位，低难度题绝对不要为了凑深度强行追问）：
   - 浅挖档（1-2 星）：completeness=complete 且 quality=correct → 直接 finish（最多追问一次轻拓展即可收尾）；partial → 追 1 轮缺口后收尾；wrong/unsure → 最多再给 1 次机会确认后收尾，绝不深挖
   - 中挖档（3 星）：completeness=complete 且 quality=correct → 可再追 1-2 轮权衡/边界拓展（不超过 L4）后收尾；partial → 追缺口；wrong/unsure → 最多再给 1 次机会确认后收尾
   - 深挖档（4-5 星）：completeness=complete 且 quality=correct → 继续加深一层追问，直到达到目标深度 L{target_level} 即收尾；partial → 同层追挖缺口；wrong/unsure → 连续 2 次答不出即收尾
3. level 标注规则：level 指**本轮追问问题**所处的深度阶梯（概念=L1、原理=L2、权衡=L3、边界/反例=L4、横向联系=L5），按问题本身的深度如实标注，**与候选人回答好坏无关**——即使候选人答不出来，问题问的是边界就是 L4

只输出 JSON，不要其他文字：
{{"action": "continue" 或 "finish", "followup": "下一轮追问或收尾语", "quality": "correct|partial|wrong|unsure", "completeness": "complete|partial|incomplete", "level": 本轮追问所在层级 1-5}}"""

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
        knowledge: str | None = None,
    ):
        from ..difficulty import probe_tier_for

        self._session_id = session_id
        self._question = question
        self._llm = llm
        self._judge_model = judge_model
        self._max_rounds = max_rounds
        self._target_level = target_level
        self._knowledge = knowledge
        self._tier = probe_tier_for(question.difficulty)  # light/medium/deep
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
        prev_quality = self._last_quality()
        followup, action, level, quality, completeness = self._ask_llm(answer)
        self._persist_attempt(
            round_no,
            is_followup=round_no > 0,
            answer=answer,
            feedback=followup,
            level=level,
            quality=quality,
        )
        if level is not None:
            self._max_level = max(self._max_level, level)
        if self._should_finish(action, round_no, quality, prev_quality, completeness):
            self._finished = True
        return RoundResult(interviewer_message=followup, finished=self._finished)

    def finish(self, reference: str | None = None, knowledge: str | None = None) -> Judgment:
        """结束追问链：调用 M10 整轮判分（带最大追问层级 + 质量轨迹 + 可选参考/知识）并落库；会话标记 finished。"""
        qualities = [
            a.quality for a in self._attempts() if a.quality
        ] or None
        # 浅挖档（1-2 星）不传 max_level：judge 按难度自然校准 depth，
        # 避免"探到低层级=低分"惩罚低难度题
        max_level = self._max_level if self._question.difficulty >= 3 else None
        judgment = judge(
            self._question,
            self._transcript(),
            self._judge_model,
            self._llm,
            session_id=self._session_id,
            reference=reference,
            knowledge=knowledge or self._knowledge,
            max_level=max_level,
            quality_trace=qualities,
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

    def _should_finish(
        self, action: str, round_no: int, quality: str | None, prev_quality: str | None, completeness: str | None
    ) -> bool:
        """判定是否收尾：LLM 自觉 finish / 轮数上限 / 档位兜底。

        档位兜底（LLM 违反 prompt 规则时强制收尾）：
        - light（1-2 星）：complete+correct 直接收尾；partial 连续 2 轮收尾（只追 1 轮缺口）；
          连续 2 次 wrong/unsure 收尾（给 1 次机会后）
        - medium（3 星）：complete 且已探到目标深度（L4）收尾；连续 2 次答差收尾
        - deep（4-5 星）：complete 且已探到目标深度（L5）收尾（达标即收，不再要求 2 轮 correct）；
          连续 2 次答差收尾
        prev_quality 须为本轮之前的上轮质量（在 persist 前查询，否则会读到本轮自己）。
        """
        if action == "finish" or round_no >= self._max_rounds - 1:
            return True
        if quality in ("wrong", "unsure") and prev_quality in ("wrong", "unsure"):
            logger.info(
                "chain forced finish for session %s (two bad answers while llm said continue)",
                self._session_id,
            )
            return True
        tier = self._tier
        if tier == "light":
            if quality == "correct" and completeness == "complete":
                return True  # 回答完整清晰 → 直接收尾
            if quality == "partial" and prev_quality == "partial":
                return True  # 只追 1 轮缺口
            return False
        if tier == "medium":
            if quality == "partial" and prev_quality == "partial":
                return True  # 同层缺口最多追 2 次
            if completeness == "complete" and self._max_level >= self._target_level:
                return True
            return False
        if completeness == "complete" and self._max_level >= self._target_level:
            return True  # 深挖档：达标即收
        return False

    def _last_quality(self) -> str | None:
        with get_session() as session:
            return session.scalars(
                select(Attempt.quality)
                .where(Attempt.session_id == self._session_id)
                .order_by(Attempt.round_no.desc())
                .limit(1)
            ).first()

    def _ask_llm(self, answer: str) -> tuple[str, str, int | None, str | None, str | None]:
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
                return DEGRADED_HINT, "continue", None, None, None
        if not isinstance(parsed, dict):
            return DEGRADED_HINT, "continue", None, None, None
        action = parsed.get("action")
        followup = parsed.get("followup")
        if not isinstance(followup, str) or not followup.strip():
            followup = DEGRADED_HINT
        level = parsed.get("level")
        if not isinstance(level, int) or not 1 <= level <= 5:
            level = None
        quality = parsed.get("quality")
        if quality not in ("correct", "partial", "wrong", "unsure"):
            quality = None
        completeness = parsed.get("completeness")
        if completeness not in ("complete", "partial", "incomplete"):
            completeness = None
        return followup, action if action == "finish" else "continue", level, quality, completeness

    def _build_prompt(self, answer: str) -> str:
        from ..difficulty import DIFFICULTY_NAMES

        tier_note = {
            "light": "浅挖档：回答完整清晰即可结束，最多轻拓展一问；绝不深挖",
            "medium": "中挖档：追到权衡/边界（L3-L4）即收尾，不强行横向",
            "deep": "深挖档：逐层深挖到目标深度，达标即收尾",
        }.get(self._tier, "")
        target_note = {
            "light": "不设强制的深度目标，以回答完整清晰为准",
            "medium": f"目标深度 L{min(self._target_level, 4)}（权衡/边界）",
            "deep": f"目标深度 L{self._target_level}（横向联系，全链深挖）",
        }.get(self._tier, "")

        history = "\n".join(self._history_lines()) or "（无）"
        knowledge_section = (
            "以下为相关知识资料（供追问时核对要点、发现候选人的知识缺口，不要直接复述原文）：\n"
            f"{self._knowledge}"
            if self._knowledge
            else ""
        )
        return CHAIN_PROMPT_V3.format(
            stem=self._question.stem,
            qtype=self._question.type.value,
            difficulty=self._question.difficulty,
            difficulty_name=DIFFICULTY_NAMES.get(self._question.difficulty, "未知"),
            target_level=self._target_level,
            tier_note=tier_note,
            target_note=target_note,
            good="\n".join(f"- {c}" for c in self._question.good_criteria),
            bad="\n".join(f"- {c}" for c in self._question.bad_criteria),
            history=history,
            answer=answer,
            knowledge_section=knowledge_section,
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
        self, round_no: int, *, is_followup: bool, answer: str, feedback: str, level: int | None, quality: str | None
    ) -> None:
        with get_session() as session:
            attempt = Attempt(
                session_id=self._session_id,
                round_no=round_no,
                is_followup=is_followup,
                answer_text=answer,
                feedback_text=feedback,
                level=level,
                quality=quality,
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
    knowledge: str | None = None,
) -> ChainSession:
    """从 attempts 表重建上下文；会话已 finished 则恢复为终态（继续调用抛 ChainStateError）。"""
    chain = ChainSession(
        session_id,
        question,
        llm,
        judge_model=judge_model,
        max_rounds=max_rounds,
        target_level=target_level,
        knowledge=knowledge,
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
