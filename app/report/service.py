"""报告拼装（ADR-0003）。**只有第 ⑥ 块调 LLM。**

拼装的素材**全都是会被编辑的**（知识点定义会被你审、题目会被隐藏/删除/改挂载），
所以"依据"必须**快照**进 `report_items`，而"结论"（分数、评语、命中）读原表 ——
它们是历史事实，本来就不会变（`docs/v2数据模型.md` §5 的快照边界）。

一句话原则：**历史报告里的「结论」是事实，永不重算；「依据」是引用，必须快照。**
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.db.models import (
    Attempt,
    Criterion,
    Evaluation,
    Interview,
    KnowledgePoint,
    Question,
    ReportItem,
    Session_ as InterviewSession,
)
from app.interview import rules, service as interview_service
from app.knowledge import service as knowledge_service
from app.llm import LLMError, prompts

logger = logging.getLogger(__name__)


@dataclass
class ItemReport:
    """③ 逐题回顾的一行。"""

    seq: int
    session_id: int
    stem: str
    point_name: str
    scores: dict[str, int] = field(default_factory=dict)
    total_score: int = 0
    review: str = ""
    status: str = "ok"
    hits: dict[str, str] = field(default_factory=dict)

    @property
    def missed(self) -> list[str]:
        """这道题里**没答到**的考察点 id（掌握度与问题清单的原料）。"""
        return [cid for cid, status in self.hits.items() if status == rules.MISS]


@dataclass
class MatrixChange:
    """② 掌握度矩阵的变化：只放**真的变了**的格子。"""

    point_id: int
    point_name: str
    before_covered: int
    before_hit: int
    after_covered: int
    after_hit: int

    @property
    def covered_delta(self) -> int:
        return self.after_covered - self.before_covered

    @property
    def hit_delta(self) -> int:
        return self.after_hit - self.before_hit


@dataclass
class Report:
    """整份报告。`report_body` 是落库的那部分（①③④⑤ + ②）。"""

    interview_id: int
    mode: str
    status: str
    total_score: int | None
    items: list[ItemReport]
    changes: list[MatrixChange]
    prerequisite_gaps: list[str]
    top_problems: list[str]
    summary: str = ""
    summary_source: str = "llm"  # llm / fallback

    def to_body(self) -> dict:
        """落库的形状（**不含 summary** —— 它单独一列，ADR-0003 的决定）。"""
        return {
            "total_score": self.total_score,
            "items": [asdict(i) for i in self.items],
            "changes": [asdict(c) | {"covered_delta": c.covered_delta, "hit_delta": c.hit_delta} for c in self.changes],
            "prerequisite_gaps": self.prerequisite_gaps,
            "top_problems": self.top_problems,
        }


def finish_interview(session: Session, *, interview: Interview, llm) -> Report:
    """面试收尾：判每题 → 拼报告 → 落库。**幂等**（重复调用只会重算并覆盖）。"""
    sessions = interview_service.sessions_of(session, interview.id)
    items = [_evaluate_and_snapshot(session, ts=ts, llm=llm) for ts in sessions]
    changes = _matrix_changes(session, interview=interview)
    missing_point_ids = _missing_point_ids(session, items)
    gaps = knowledge_service.prerequisite_gaps(session, missing_point_ids)
    problems = _top_problems(session, items)

    report = Report(
        interview_id=interview.id,
        mode=interview.mode,
        status=interview.status,
        total_score=_average_total(items),
        items=items,
        changes=changes,
        prerequisite_gaps=gaps,
        top_problems=problems,
    )
    report.summary, report.summary_source = _summarize(report, llm=llm)

    interview.report_body = report.to_body()
    interview.report_summary = report.summary
    interview.status = "finished"
    from app.account import repository as account_repository

    interview.ended_at = account_repository.now_iso()
    session.flush()
    return report


# ---------------------------------------------------------------------------
# ③ 逐题：判定 + **快照依据**
# ---------------------------------------------------------------------------
def _evaluate_and_snapshot(session: Session, *, ts: InterviewSession, llm) -> ItemReport:
    """判一道题，并把**依据**快照进 `report_items`。

    快照什么 / 不快照什么是有边界的（数据模型 §5）：
    **快照依据**（会变）：题干、题型、难度、知识点名、考察点与 good/bad criteria
    **不快照结论**（是历史事实）：分数、评语读 `evaluations`，命中读 `attempts`
    """
    result = interview_service.evaluate_session(session, ts=ts, llm=llm)
    question = session.get(Question, ts.question_id)
    criteria = bank_repository.criteria_of_question(session, question) if question else []
    snapshot = interview_service.current_snapshot(session, ts.id)
    names = bank_repository.point_names(
        session, {question.primary_point_id} if question and question.primary_point_id else set()
    )

    existing = session.execute(
        select(ReportItem).where(
            ReportItem.interview_id == ts.interview_id, ReportItem.seq == ts.seq
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = ReportItem(interview_id=ts.interview_id, seq=ts.seq)
        session.add(existing)
    existing.session_id = ts.id
    existing.snap_stem = question.stem if question else ""
    existing.snap_question_kind = question.kind if question else None
    existing.snap_difficulty = question.difficulty if question else None
    existing.snap_point_name = (
        names.get(question.primary_point_id or -1, "") if question else ""
    )
    existing.snap_criteria = {
        "criteria": [c.text for c in criteria],
        # 题目级 criteria 只作为**历史证据**进快照，不参与判分（决策 49 的纪律）
        "good_criteria": question.good_criteria if question else None,
        "bad_criteria": question.bad_criteria if question else None,
    }
    session.flush()

    return ItemReport(
        seq=ts.seq,
        session_id=ts.id,
        stem=existing.snap_stem or "",
        point_name=existing.snap_point_name or "",
        scores=result.scores,
        total_score=result.total_score,
        review=result.review,
        status=result.status,
        hits=snapshot.to_json(),
    )


# ---------------------------------------------------------------------------
# ② 矩阵变化
# ---------------------------------------------------------------------------
def _matrix_changes(session: Session, *, interview: Interview) -> list[MatrixChange]:
    """② 「本场之前」与「本场之后」相减。**只放真的变了的格子。**

    "之前"是用同一套聚合、少算这一场（`exclude_interview_ids`）——
    而不是维护历史副本：重算是确定的，副本会随用户注销失去意义。
    """
    after = knowledge_service.mastery_matrix(session, interview.user_id)
    before = knowledge_service.mastery_matrix(
        session, interview.user_id, exclude_interview_ids={interview.id}
    )
    before_by_id = before.by_point()

    changes: list[MatrixChange] = []
    for cell in after.cells:
        old = before_by_id.get(cell.point_id)
        old_covered = old.covered if old else 0
        old_hit = old.hit if old else 0
        if (old_covered, old_hit) != (cell.covered, cell.hit):
            changes.append(
                MatrixChange(
                    point_id=cell.point_id,
                    point_name=cell.point_name,
                    before_covered=old_covered,
                    before_hit=old_hit,
                    after_covered=cell.covered,
                    after_hit=cell.hit,
                )
            )
    return changes


# ---------------------------------------------------------------------------
# ④ 前置缺口
# ---------------------------------------------------------------------------
def _missing_point_ids(session: Session, items: list[ItemReport]) -> set[int]:
    """这一场里**有未命中考察点**的那些知识点 —— 前置缺口的起点。"""
    missed_criteria: set[int] = set()
    for item in items:
        missed_criteria |= {int(cid) for cid in item.missed}
    if not missed_criteria:
        return set()
    rows = session.execute(
        select(Criterion.point_id).where(Criterion.id.in_(sorted(missed_criteria)))
    ).all()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# ⑤ 主要问题清单
# ---------------------------------------------------------------------------
def _top_problems(session: Session, items: list[ItemReport], *, limit: int = 5) -> list[str]:
    """⑤ 漏得最多的考察点，按知识点归类。

    **按"漏的次数"排序**而不是按出现顺序：一个考察点在多道题里反复没答到，
    才是这场面试最该说的事。
    """
    counts: dict[str, int] = {}
    for item in items:
        for cid in item.missed:
            counts[cid] = counts.get(cid, 0) + 1

    if not counts:
        return []
    rows = session.execute(
        select(
            Criterion.id,
            Criterion.text,
            KnowledgePoint.name,
        )
        .join(KnowledgePoint, KnowledgePoint.id == Criterion.point_id)
        .where(Criterion.id.in_([int(c) for c in counts]))
    ).all()
    ranked = sorted(rows, key=lambda r: (-counts[str(r[0])], r[2]))
    return [f"{name}：{text}" for _cid, text, name in ranked[:limit]]


# ---------------------------------------------------------------------------
# ⑥ 那段总结（**唯一的 LLM**）
# ---------------------------------------------------------------------------
def _summarize(report: Report, llm) -> tuple[str, str]:
    """总结只收到**已算出的结论**，不收到完整对话（ADR-0003 的硬约束）。

    不输入完整对话记录的理由：判分器已经读过一遍，再读一遍等于重新判断一次 ——
    那是幻觉的主要入口。总结的角色是朗读者，不是裁判。

    失败 → 降级为一句**确定性**的话并标明来源（AGENTS.md §3.1：降级可以，静默不行）。
    """
    facts = _facts_block(report)
    try:
        data, _ = llm.chat_json(
            [
                {
                    "role": "user",
                    "content": prompts.load("interviewer/report_summary.md").render(facts=facts),
                }
            ]
        )
    except LLMError as e:
        logger.warning("报告总结生成失败，降级为确定性文案：%s", e)
        return _fallback_summary(report), "fallback"

    text = ""
    if isinstance(data, dict):
        text = str(data.get("summary") or data.get("text") or "").strip()
    if not text:
        logger.warning("总结为空，降级为确定性文案")
        return _fallback_summary(report), "fallback"
    if not summary_names_are_grounded(text, report):
        # ADR-0003 的硬约束：总结里不许出现数据里没有的东西。
        # **降级而不是重试**：重试同一个模型往往还是编，而报告不能因此打不开。
        logger.warning("总结里出现了数据里没有的知识点名，降级为确定性文案：%r", text[:80])
        return _fallback_summary(report), "fallback"
    return text, "llm"


def _known_names(report: Report) -> set[str]:
    names: set[str] = set()
    for item in report.items:
        names |= _tokens(item.point_name)
    for change in report.changes:
        names |= _tokens(change.point_name)
    for gap in report.prerequisite_gaps:
        names |= _tokens(gap)
    for problem in report.top_problems:
        names |= _tokens(problem.split("：")[0])
    return names


def _tokens(name: str) -> set[str]:
    """把一个知识点名拆成"可被提及的片段"。

    "JMM 内存模型" 这样带修饰的名字，模型可能只说"内存模型"或只说"JMM" ——
    都算提到了这个名字。所以按分隔符拆开，而不是要求逐字相同。
    """
    parts = {name.strip()}
    parts |= {p.strip() for p in name.replace("（", " ").replace("）", " ").split()}
    parts |= {p.strip() for p in name.split("的")}
    return {p for p in parts if p}


def _candidate_names(text: str) -> set[str]:
    """从总结里挑出"**可能**是知识点名"的片段。

    判据故意很窄，**宁可漏检也不能误报**。理由与 `scripts/check_docs.py` 里那条
    "一条满屏误报的规则比没有规则更糟"完全相同 —— 会拦住正常文案的检查会被绕过，
    于是等于没有。

    只查**拉丁词**（`Kubernetes` / `RAG` / `JVM` 这类技术名词）：它们是"凭空出现"
    的最强信号，而且不含任何常见词，误报率接近零。

    ⚠️ **已知局限（写下来，而不是假装它不存在）**：中文的"编造知识点"在词法上
    与"这句话本身"不可区分。第一版试过"提取 2-4 个汉字连续片段再比对"，结果
    把「这场面试说明…」的每一个片段都判成编造 —— 整段正常文案全被拦下。
    要做到真检查中文名，得先有**知识点名词表**（`knowledge_points` 全表在库里），
    再对"像名词的片段"做一次相似度匹配 —— 那是"阈值标定"那一类工作（§未决 6），
    属于 MVP 之后。所以这里只保证：**术语级幻觉会被拦下**。
    """
    import re

    return set(re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{1,}", text))


def summary_names_are_grounded(text: str, report: Report) -> bool:
    """总结里提到的（拉丁）名字是否都能在数据里找到 —— 可程序化检查，ADR-0003。

    覆盖不了中文编造（见 `_candidate_names` 的局限说明），但它覆盖的正是
    "幻觉引入不存在的技术名词"这一类 —— 也就是 ADR-0003 真正担心的那件事。
    """
    known = _known_names(report)
    for name in _candidate_names(text):
        if any(name in k or k in name for k in known):
            continue
        return False
    return True


def _facts_block(report: Report) -> str:
    lines: list[str] = []
    if report.total_score is not None:
        lines.append(f"本场平均总分：{report.total_score}")
    for item in report.items:
        dims = "、".join(f"{k} {v}" for k, v in item.scores.items())
        lines.append(f"第 {item.seq} 题（知识点 {item.point_name}）：总分 {item.total_score}，{dims}")
    if report.changes:
        lines.append("掌握度变化：")
        for c in report.changes:
            lines.append(
                f"  {c.point_name}：命中 {c.before_hit}/{c.before_covered} → "
                f"{c.after_hit}/{c.after_covered}"
            )
    if report.prerequisite_gaps:
        lines.append("该补的前置知识点：" + "、".join(report.prerequisite_gaps))
    if report.top_problems:
        lines.append("漏得最多的考察点：")
        lines.extend(f"  {p}" for p in report.top_problems)
    return "\n".join(lines) or "（这场面试没有产生任何判定数据）"


def _fallback_summary(report: Report) -> str:
    """降级文案。它**只由已落库的数据拼**，所以永远与报告正文一致。"""
    if report.total_score is None:
        return "这场面试没有产生判定数据，无法给出总结。"
    worst = report.changes[0].point_name if report.changes else None
    parts = [f"本场平均总分 {report.total_score}。"]
    if worst:
        parts.append(f"变化最大的是「{worst}」。")
    if report.prerequisite_gaps:
        parts.append("建议先补：" + "、".join(report.prerequisite_gaps) + "。")
    if report.top_problems:
        parts.append("最需要补的是 " + report.top_problems[0] + "。")
    return "".join(parts)


def _average_total(items: list[ItemReport]) -> int | None:
    """① 本场总分 = 各题总分的平均（没有题就没有分数，返回 None 而不是 0）。"""
    scored = [i.total_score for i in items if i.status == "ok"]
    if not scored:
        return None
    return round(sum(scored) / len(scored))


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def get_report(session: Session, interview: Interview) -> Report:
    """读回**已落库**的报告（不重算）—— 这是"打开报告"走的那条路。"""
    body = interview.report_body or {}
    items = [ItemReport(**raw) for raw in body.get("items", [])]
    changes = [
        MatrixChange(
            point_id=raw["point_id"],
            point_name=raw["point_name"],
            before_covered=raw["before_covered"],
            before_hit=raw["before_hit"],
            after_covered=raw["after_covered"],
            after_hit=raw["after_hit"],
        )
        for raw in body.get("changes", [])
    ]
    return Report(
        interview_id=interview.id,
        mode=interview.mode,
        status=interview.status,
        total_score=body.get("total_score"),
        items=items,
        changes=changes,
        prerequisite_gaps=list(body.get("prerequisite_gaps", [])),
        top_problems=list(body.get("top_problems", [])),
        summary=interview.report_summary or "",
        summary_source="stored",
    )


def attempts_for(session: Session, session_id: int) -> list[Attempt]:
    return interview_service.attempts_of(session, session_id)


def evaluation_for(session: Session, session_id: int) -> Evaluation | None:
    return session.execute(
        select(Evaluation).where(Evaluation.session_id == session_id)
    ).scalar_one_or_none()
