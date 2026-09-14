"""给知识点**补题**的离线管道（决策 4：内容来源三类里的"生成题"）。

## 它补的是哪条缺口

基线写着内容来源三类：**种子题 / 生成题（LLM）/ 晋升题**。前两类此前只到一半 ——
LLM 只生成**私有**题（简历那条路），公共池只能靠 v1 导入与用户晋升长大。这个模块
把"生成题"这条补齐：**按已确认的知识点生成公共题**。

## 四个判断

① **入口是"已确认的知识点"**，不是"凭空生成一批题"。题必须有主知识点（基线：
   每道题必须有一个主知识点），而生成题不产生新判据 —— 判分读的是 `criteria` 表里
   那个知识点**已有**的判据（它们是人审过的）。于是"出一批题"这件事天然不会绕过骨架。

② **候选池只收"缺题的点"**：一个知识点已经有 N 道公共题就没必要再补。这一条同时
   是成本控制（决策 16：名额上限是成本阀门）—— 不会因为跑一次命令就生成上千道。

③ **过 `promotion.run_gate` 才算数**（决策 5：自动门禁进池）。生成出来的题**不因为
   是系统写的就免检**：长度、题型、去重、考察点四条一样跑。不过闸的题**不插入**，
   而是进报告（不静默 —— 用户/管理员要能看到"这次生成了几道、被挡下几道、为什么"）。

④ **幂等**：题干已存在就跳过（`stem_exists`）。离线任务会被重跑（心跳回退、手动重跑），
   而"重跑一次多出十道一样的题"是最难收拾的一种脏数据。

## 成本记在哪

它是**离线**调用（决策 8/§3.8），没有哪个用户该为此付额度点，所以账本（`quota_ledger`
按 user 记）里没有它的位置。用量进 `task_logs` 的报告（`tokens` 字段）—— 于是
"这次花了多少"仍然查得到（观测页的「离线任务报告」会显示这一条）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.bank import service as bank_service
from app.bank.promotion import run_gate
from app.db.models import KnowledgePoint
from app.llm import LLMError, prompts

logger = logging.getLogger(__name__)

#: 每个知识点至少该有几道公共题。低于它就要补 —— 与"推荐题"那条一样，
#: 这个数字是**待标定**的（题少了组卷抽不开，题多了没人做）。
TARGET_PER_POINT = 3

#: 一次调用最多生成几道。它是成本阀门：越大越容易撞上"输出被 max_tokens 截断"，
#: 也越容易生成出彼此重复的题。
DEFAULT_COUNT = 3
MAX_COUNT = 6


@dataclass
class PointNeed:
    point: KnowledgePoint
    have: int

    @property
    def missing(self) -> int:
        return max(0, TARGET_PER_POINT - self.have)


@dataclass
class GenerationReport:
    point_id: int
    point_name: str = ""
    created: int = 0
    skipped_duplicate: int = 0
    rejected: list[str] = field(default_factory=list)
    llm_failed: bool = False
    note: str = ""
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id,
            "point_name": self.point_name,
            "created": self.created,
            "skipped_duplicate": self.skipped_duplicate,
            "rejected": self.rejected,
            "llm_failed": self.llm_failed,
            "tokens": self.tokens,
            "message": self.summary(),
        }

    def summary(self) -> str:
        bits = [f"知识点「{self.point_name or self.point_id}」新增 {self.created} 道"]
        if self.skipped_duplicate:
            bits.append(f"重复跳过 {self.skipped_duplicate} 道")
        if self.rejected:
            bits.append(f"门禁挡下 {len(self.rejected)} 道")
        if self.llm_failed:
            bits.append(f"模型失败：{self.note}")
        if self.tokens:
            bits.append(f"{self.tokens} token")
        return "，".join(bits)


def points_needing_questions(
    session: Session, *, target: int = TARGET_PER_POINT, limit: int | None = None
) -> list[PointNeed]:
    """缺题的**已确认**知识点，按"缺得最多"排。

    只收 `status='confirmed'`：草稿点是"私有题集的锚点"之类的临时容器（决策 46：
    骨架的判定权在人手上），给它们补公共题没有意义。
    """
    points = list(
        session.execute(
            select(KnowledgePoint).where(KnowledgePoint.status == "confirmed")
        ).scalars()
    )
    if not points:
        return []
    counts = bank_repository.point_question_counts(session, [p.id for p in points])
    needs = [
        PointNeed(point=p, have=counts.get(p.id, 0))
        for p in points
        if counts.get(p.id, 0) < target
    ]
    needs.sort(key=lambda need: (-need.missing, need.point.id))
    return needs[:limit] if limit else needs


def generate_for_point(
    session: Session,
    *,
    point: KnowledgePoint,
    count: int = DEFAULT_COUNT,
    llm,
) -> GenerationReport:
    """给一个知识点生成 `count` 道公共题（过门禁才插入）。"""
    report = GenerationReport(point_id=point.id, point_name=point.name)
    criteria = bank_repository.criteria_of_point(session, point.id)
    if not criteria:
        # **写不出考察点就不是知识点**（判据①）：没有判据的题无法被客观评判
        report.note = "这个知识点没有考察点定义 —— 先补判据再补题"
        report.rejected.append(report.note)
        return report

    before = dict(getattr(llm, "usage_total", {}) or {})
    try:
        statements = _ask_model(point=point, criteria=criteria, count=count, llm=llm)
    except LLMError as e:
        logger.warning("给知识点 %s 补题失败：%s", point.id, e)
        report.llm_failed = True
        report.note = str(e)
        return report

    for item in statements:
        stem = str(item.get("stem") or "").strip()
        if not stem:
            continue
        if bank_repository.stem_exists(session, stem):
            report.skipped_duplicate += 1
            continue
        question = bank_service.author_public_question(
            session,
            point_id=point.id,
            stem=stem,
            kind=str(item.get("kind") or "knowledge"),
            difficulty=_clamp_difficulty(item.get("difficulty")),
            reference_answer=str(item.get("reference_answer") or "").strip(),
        )
        gate = run_gate(session, question=question)  # 系统出的题**一样过闸**
        if not gate.passed:
            # 不过闸就不插入 —— 而"哪一条没过"要留在报告里（决策 5 的门禁必须是可解释的）
            report.rejected.append(f"{stem[:24]}…：{gate.summary()}")
            session.delete(question)
            session.flush()
            continue
        report.created += 1

    session.flush()
    report.tokens = _usage_delta(llm, before)
    return report


def generate_for_missing(
    session: Session, *, llm, per_point: int = DEFAULT_COUNT, limit: int | None = None
) -> list[GenerationReport]:
    """扫一遍缺题的知识点，逐个补 —— CLI 与离线任务共用这一条。"""
    reports: list[GenerationReport] = []
    for need in points_needing_questions(session, limit=limit):
        count = min(max(need.missing, 1), min(per_point, MAX_COUNT))
        reports.append(generate_for_point(session, point=need.point, count=count, llm=llm))
    return reports


def _ask_model(*, point: KnowledgePoint, criteria: list, count: int, llm) -> list[dict]:
    """一次调用出 `count` 道题。解析失败/字段缺失都由调用方容忍（返回空列表）。"""
    prompt = prompts.load("offline/generate_public_questions.md").render(
        name=point.name,
        definition=point.exclusions or "（没有额外说明）",
        criteria="\n".join(f"{c.id}. {c.text}" for c in criteria),
        count=count,
    )
    data, _ = llm.chat_json([{"role": "user", "content": prompt}])
    payload = data if isinstance(data, dict) else {}
    items = payload.get("questions") or []
    return [item for item in items if isinstance(item, dict)][:MAX_COUNT]


def _clamp_difficulty(raw: object) -> int:
    """难度钳进 1-5（§4.3 的"容错优先于严格"：一次脏输出不该让这一批失败）。

    ⚠️ 与库里那条 `CHECK (difficulty BETWEEN 1 AND 5)` 是**两道闸**：这里是"别让
    模型的一句脏话毁掉整批"，那里是"别让脏值进库"。少一道就会原地抛 IntegrityError。
    """
    try:
        value = int(float(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 3
    return min(5, max(1, value))


def _usage_delta(llm, before: dict[str, int]) -> int:
    after = getattr(llm, "usage_total", None)
    if not isinstance(after, dict):
        return 0
    return sum(
        after.get(k, 0) - before.get(k, 0) for k in ("prompt_tokens", "completion_tokens")
    )
