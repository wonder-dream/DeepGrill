"""报告拼装（ADR-0003）。**只有第 ⑥ 块调 LLM。**

拼装的素材**全都是会被编辑的**（知识点定义会被你审、题目会被隐藏/删除/改挂载），
所以"依据"必须**快照**进 `report_items`，而"结论"（分数、评语、命中）读原表 ——
它们是历史事实，本来就不会变（`docs/v2数据模型.md` §5 的快照边界）。

一句话原则：**历史报告里的「结论」是事实，永不重算；「依据」是引用，必须快照。**
"""

from __future__ import annotations

import logging
import re
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
)
from app.db.models import (
    Session_ as InterviewSession,
)
from app.errors import NotFound
from app.interview import rules
from app.interview import service as interview_service
from app.knowledge import service as knowledge_service
from app.llm import LLMError, prompts

logger = logging.getLogger(__name__)


@dataclass
class SummarySignals:
    """总结的可疑之处。**它不阻断任何事情** —— 页面据此标注一句。

    为什么不拦：这套检查是对**自由文本**做的启发式判断，实测误报过四次（见
    `summary_names_are_grounded`）。把误报的东西删掉，代价是丢掉正确输出；
    标出来让人自己判断，代价只是多一句话。

    （定义在 `Report` 之前，因为它要做 `Report` 的字段默认值。）
    """

    #: 数据里找不到的拉丁技术名词（如凭空出现的 Kubernetes）
    ungrounded_terms: list[str] = field(default_factory=list)
    #: 与数据**矛盾**的说法（如数据里 depth 最低、总结却说 accuracy 是短板）
    contradictions: list[str] = field(default_factory=list)
    #: 数字核查：数据里没有、也算不出来的数字
    unknown_numbers: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.ungrounded_terms or self.contradictions or self.unknown_numbers)


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
    #: 这道题的考察点正文。它有两个用途：喂给判分的 prompt，以及
    #: **让总结的 grounding 检查知道哪些技术名词是合法的**（见 `_known_names`）。
    criteria_texts: list[str] = field(default_factory=list)

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
    #: 总结的可疑之处（**只标注，不阻断** —— 见 `SummarySignals`）
    signals: SummarySignals = field(default_factory=SummarySignals)

    def to_body(self) -> dict:
        """落库的形状。

        **不含 `summary`**（它单独一列，ADR-0003 的决定）、也**不含
        `criteria_texts`** —— 后者是"怎么算出来的"（判分 prompt 与 grounding 检查的
        输入），它已经在同一行的 `criteria` 快照里了，不必存两份。
        """
        return {
            "total_score": self.total_score,
            "items": [
                {k: v for k, v in asdict(i).items() if k != "criteria_texts"}
                for i in self.items
            ],
            "changes": [
                asdict(c) | {"covered_delta": c.covered_delta, "hit_delta": c.hit_delta}
                for c in self.changes
            ],
            "prerequisite_gaps": self.prerequisite_gaps,
            "top_problems": self.top_problems,
            # 信号也要落库 —— 否则"打开报告"这条读路径看不到它，页面就没法标注
            # （实测：原先只在生成路径算过，读回来就丢了）。
            "signals": asdict(self.signals),
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
        criteria_texts=[c.text for c in criteria],
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
        logger.warning("总结为空，改用组装式文案")
        return _fallback_summary(report), "fallback"

    # ⚠️ **全部只记、不拦**（理由见 `summary_names_are_grounded` 与 `check_summary`
    # 的 docstring：这套检查对自由文本做启发式判断，实测误报过四次）。
    # 页面据此标注一句"以逐题回顾为准"。
    report.signals = check_summary(text, report)
    report.signals.ungrounded_terms = sorted(
        set(_candidate_names(text)) - _known_names(report)
    )
    if report.signals.any:
        logger.warning(
            "总结有可疑之处（**已保留**，仅标注）：矛盾=%s 未知数字=%s 数据外名词=%s",
            report.signals.contradictions,
            report.signals.unknown_numbers,
            report.signals.ungrounded_terms,
        )
    return text, "llm"


#: 四维分在数据里的中文说法（报告自己的维度名是英文，总结可能用中文）。两套都要认。
#: （`SummarySignals` 定义在文件上方、`Report` 之前 —— 它要做 `Report` 的字段默认值。）
DIMENSION_ALIASES: dict[str, tuple[str, ...]] = {
    "accuracy": ("accuracy", "准确性", "准确度", "准确"),
    "completeness": ("completeness", "完整度", "完整性", "完整"),
    "clarity": ("clarity", "表达", "清晰度", "清晰"),
    "depth": ("depth", "深度"),
}

#: 「最弱」的措辞。总结说"X 是短板"时用它判定这句话在断言什么。
WEAK_MARKERS = ("最低", "最弱", "短板", "拖后腿", "拖了后腿", "最差", "欠缺最多的")


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text)


def _allowed_numbers(report: Report) -> set[str]:
    """报告自己的数字 —— 总结引用它们**一定**不算编造。

    包括：总分、四维分、掌握度的命中/被考（两半都要）、以及这些数之间的减法
    （"掌握度 +2"这种是数据的函数）。
    """
    allowed: set[str] = set()
    if report.total_score is not None:
        allowed.add(str(report.total_score))
    for item in report.items:
        allowed.add(str(item.seq))
        allowed.add(str(item.total_score))
        for value in item.scores.values():
            allowed.add(str(value))
    for change in report.changes:
        for value in (
            change.before_covered, change.before_hit,
            change.after_covered, change.after_hit,
            abs(change.covered_delta), abs(change.hit_delta),
        ):
            allowed.add(str(value))
    allowed.add("0")  # "0/0"、"0 条" 这类零值随处可见，且它本身不构成编造
    return allowed


def _asserted_weak_dimension(text: str) -> str | None:
    """总结在断言"哪一维最弱"吗？是就返回那一维。

    判定方式：句子里同时出现**维度别名**与**最弱措辞**。窄且可判定 ——
    只认"同一句里两者都有"，不做跨句推断（跨句推断会开始猜）。
    """
    for sentence in re.split(r"[。；;\n]", text):
        marked = any(marker in sentence for marker in WEAK_MARKERS)
        if not marked:
            continue
        hits = [
            dim for dim, aliases in DIMENSION_ALIASES.items()
            if any(alias in sentence for alias in aliases)
        ]
        # 只认"恰好提到一维"的句子：提了两维的句子（"虽然 accuracy 稳，但 depth 弱"）
        # 无法靠词法判断它到底在断言哪一维，宁可不判（避免误报）。
        if len(hits) == 1:
            return hits[0]
    return None


def _weakest_dimension(report: Report) -> str | None:
    """数据里真正最弱的那一维（各题四维分的平均最低者）。

    只统计判分成功的题：判分失败时四维全是 0，把它们算进来会让"最弱维度"永远是
    失败那一题的某一维（那是记账问题，不是候选人的表现）。
    """
    scored = [i for i in report.items if i.status == "ok" and i.scores]
    if not scored:
        return None
    avg = {
        dim: sum(i.scores.get(dim, 0) for i in scored) / len(scored)
        for dim in rules.DIMENSIONS
    }
    return min(avg, key=lambda d: (avg[d], d))


def check_summary(text: str, report: Report) -> SummarySignals:
    """对总结做**可程序化验证**的检查，返回可疑之处。

    两条断言（都是 ADR-0003 想要的"别与数据矛盾"）：

    ① **数字必须来自数据**（含其函数）。数字是**闭集词汇**，所以这一条能做得精确
       —— 这也是它比"名词是否在数据里"可靠得多的原因：名词是开放集。
    ② **"哪一维最弱"必须与 `evaluations.scores` 一致** —— 这正是 ADR-0003 举的例子
       （"评语说没答到内存屏障，总结却写并发基础扎实"）在报告层面的形态。

    ⚠️ 它**只返回信号**，不阻断。中文编造的名词、跨句的语义矛盾都不在能力范围内
    —— 那些要靠更强的判据（或模型裁判），属后续工作。
    """
    signals = SummarySignals()

    # ① 数字
    allowed = _allowed_numbers(report)
    signals.unknown_numbers = sorted({n for n in _numbers(text) if n not in allowed})

    # ② 最弱维度
    asserted = _asserted_weak_dimension(text)
    actual = _weakest_dimension(report)
    if asserted and actual and asserted != actual:
        signals.contradictions.append(
            f"总结说「{asserted}」最弱，但数据里最低的是「{actual}」"
        )

    return signals


def _known_names(report: Report) -> set[str]:
    """总结里**允许出现**的技术名词集合。

    不只是知识点名 —— 还包括每一题考察点正文里的拉丁词。为什么必须包括它们：
    模型在解释"哪一条没答到"时**必然会复述那条考察点**，而考察点里天然含技术名词。

    实测踩过：`volatile` 那道题的考察点是「与 synchronized 的适用场景区别」，
    总结里写 `synchronized` 是**完全正确**的引用，而第一版检查把它判成"编造"，
    于是**把一段好总结丢掉了**（那次的总结质量明显高于降级文案）。
    这是本项目第三次踩"规则误报"（前两次在 `scripts/check_docs.py` 规则 18 与
    题库的结构断言），所以这次改的是检查本身而不是文案。
    """
    names: set[str] = set()

    def add(text: str | None) -> None:
        if text:
            # 用 update 而不是 `names |= …`：后者的赋值语义会让 `names` 在这个
            # 嵌套函数里变成**局部变量**，于是外层那份永远读不到（UnboundLocalError）。
            names.update(_tokens(text))

    for item in report.items:
        add(item.point_name)
        for criterion in item.criteria_texts:
            add(criterion)
        # 四维分的**维度名**也是报告自己的数据（它们就在 scores 里），模型引用它们
        # 完全合法。实测踩过：总结写"accuracy 65、completeness 60…"被判成编造，
        # 而那是照抄数据。
        for dimension in item.scores:
            add(dimension)
    for change in report.changes:
        add(change.point_name)
    for gap in report.prerequisite_gaps:
        add(gap)
    for problem in report.top_problems:
        add(problem)
    return names


def _tokens(name: str) -> set[str]:
    """把一个名字拆成"可被提及的片段"，并单独取出其中的拉丁词。

    "JMM 内存模型" 这样带修饰的名字，模型可能只说"内存模型"或只说"JMM" ——
    都算提到了这个名字。所以按分隔符拆开，而不是要求逐字相同。
    """
    parts = {name.strip()}
    parts |= {p.strip() for p in name.replace("（", " ").replace("）", " ").split()}
    parts |= {p.strip() for p in name.split("的")}
    parts |= set(re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{1,}", name))
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
    return set(re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{1,}", text))


def summary_names_are_grounded(text: str, report: Report) -> bool:
    """总结里提到的（拉丁）名字是否都能在数据里找到。

    ⚠️ **它是信号，不是闸门** —— 这一点是**真调之后改的**（见本文件头的说明）。

    ADR-0003 把这条写成了硬约束（"总结里不许出现数据里没有的东西"）并要求
    "可程序化检查"。实现成准入检查之后，它连续**四次误报**，每次都在丢掉正确输出：

    | 误报 | 被误判的东西其实是 |
    |---|---|
    | 汉字 2-4 字片段 | 正常句子本身（「这场面试说明…」的每个片段） |
    | 标题类文本 | 规则本该跳过的东西 |
    | `synchronized` | **考察点正文里的**技术名词 |
    | `accuracy` / `depth` 等 | **报告自己的**四维分维度名 |

    四次都指向同一件事：**"总结里的每个词是否在数据里"这件事，词法匹配做不到**
    —— 它要求模型逐字复述，而"总结"这项任务的本质就是改写与归纳。放宽到子串/相似度
    只是把误报换成漏检，阈值没有正确取值。

    所以现在它只**标注**（页面提示"这段总结里有数据外的说法"），不阻断。
    真正该由程序守的那条（ADR-0003 的原始担心）是**结论性断言与数据矛盾**
    （"评语说没答到内存屏障，总结却写并发基础扎实"）—— 那需要按断言类别做比对，
    不在这条词法检查的能力范围内，属后续工作。

    覆盖不了中文编造（见 `_candidate_names` ），但**这个能力缺口是否还值得补**
    要等上面那条结构性检查做出来再判断。
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
    """模型那次没成功时用的文案 —— **由已落库的数据组装出来**，所以永远与报告
    正文一致、完全可复现、零 token。

    它不是"随便一句兜底"：`_assemble_summary` 是本项目对比过三条路线之后选的
    兜底形态（组装式、受限生成、自由生成），理由是它**从不编造**（数字与措辞
    都直接来自数据）且零成本 —— 而"模型失败"恰恰是最不该再冒险的时刻。
    """
    return _assemble_summary(report)


def _assemble_summary(report: Report) -> str:
    """**不调模型**，把已算出的结论按固定句式拼成一段。

    句式刻意保持"结论 + 数据"的形状（"X 是最低的一项"），因为：
    · 每个分句都能追溯到 `report` 里的一个字段 —— 于是它**不可能编造**
    · 它读起来像体检报告，而 ADR-0003 明确要的就是这个调子
      （"宁可像体检报告，不要像散文"）
    """
    parts: list[str] = []
    if report.total_score is not None:
        parts.append(f"本场平均总分 {report.total_score}。")

    scored = [i for i in report.items if i.status == "ok" and i.scores]
    if scored:
        worst_item = min(scored, key=lambda i: min(i.scores.values()))
        dim = min(worst_item.scores, key=lambda k: worst_item.scores[k])
        parts.append(
            f"第 {worst_item.seq} 题（{worst_item.point_name}）的 "
            f"{dim} 是 {worst_item.scores[dim]} 分，最低的一项。"
        )

    moved = [c for c in report.changes if c.hit_delta > 0]
    if moved:
        c = moved[0]
        parts.append(
            f"「{c.point_name}」的掌握度从 {c.before_hit}/{c.before_covered} "
            f"走到 {c.after_hit}/{c.after_covered}。"
        )

    if report.top_problems:
        parts.append("最需要补的是 " + report.top_problems[0] + "。")
    if report.prerequisite_gaps:
        parts.append("建议先补：" + "、".join(report.prerequisite_gaps) + "。")

    return "".join(parts) or "这场面试没有产生判定数据，无法给出总结。"


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
    """读回**已落库**的报告（不重算）—— 这是"打开报告"走的那条路。

    考察点正文从 `report_items` 的**快照**里取（`snap_criteria`）：它是"当时的
    考察点"，与历史报告显示的内容一致 —— 而不是去读 `criteria` 表拿现在的定义
    （那样报告会随你审知识点而变，正是快照要挡住的）。
    """
    body = interview.report_body or {}
    criteria_by_seq = _criteria_by_seq(session, interview.id)

    items: list[ItemReport] = []
    for raw in body.get("items", []):
        # 旧报告里可能没有 criteria_texts（本字段是后加的）—— 用快照补，缺失就空
        fields = {k: v for k, v in raw.items() if k != "criteria_texts"}
        fields.setdefault("criteria_texts", criteria_by_seq.get(raw.get("seq"), []))
        items.append(ItemReport(**fields))

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
        signals=_signals_from_body(body.get("signals")),
    )


def _signals_from_body(raw: object) -> SummarySignals:
    """把落库的信号读回来。旧报告没有这个字段 → 全是空（不影响渲染）。"""
    if not isinstance(raw, dict):
        return SummarySignals()
    return SummarySignals(
        ungrounded_terms=list(raw.get("ungrounded_terms") or []),
        contradictions=list(raw.get("contradictions") or []),
        unknown_numbers=list(raw.get("unknown_numbers") or []),
    )


def _criteria_by_seq(session: Session, interview_id: int) -> dict[int, list[str]]:
    """从 `report_items` 的快照里取每道题的考察点正文（当时的，不是现在的）。"""
    rows = session.execute(
        select(ReportItem.seq, ReportItem.snap_criteria).where(
            ReportItem.interview_id == interview_id
        )
    ).all()
    out: dict[int, list[str]] = {}
    for seq, snap in rows:
        if isinstance(snap, dict):
            out[seq] = list(snap.get("criteria") or [])
    return out


def attempts_for(session: Session, session_id: int) -> list[Attempt]:
    return interview_service.attempts_of(session, session_id)


def evaluation_for(session: Session, session_id: int) -> Evaluation | None:
    return session.execute(
        select(Evaluation).where(Evaluation.session_id == session_id)
    ).scalar_one_or_none()


def finish_interview_by_id(session: Session, *, interview_id: int, llm) -> Report:
    """按 id 收尾。页面只拿得到 id（URL 里就是 id），所以入口收 id 而不是对象。"""
    interview = session.get(Interview, interview_id)
    if interview is None:
        raise NotFound("这场面试不存在")
    return finish_interview(session, interview=interview, llm=llm)


def next_pending_session(session: Session, *, interview_id: int) -> InterviewSession | None:
    """下一道**还没答完**的题；没有就返回 None（调用方据此决定收尾）。"""
    for ts in interview_service.sessions_of(session, interview_id):
        if ts.status != "finished":
            return ts
    return None


@dataclass
class InterviewPageData:
    """答题页要显示的东西。

    形状由 `web/` 决定（ADR-0010：领域不知道页面长什么样），但它住的这里是
    `pages.py` 该有的位置 —— 只是本领域只有这一个页面，单开一个文件不值当。
    """

    session: InterviewSession
    interview: Interview
    question: Question
    point_name: str
    criteria: list[str]
    rounds: list[Attempt]
    snapshot: rules.HitSnapshot
    seq: int
    total_questions: int


def interview_page_data(session: Session, *, ts: InterviewSession, user_id: int) -> InterviewPageData:
    """答题页的数据。**只读**，不调 LLM。"""
    interview = session.get(Interview, ts.interview_id)
    if interview is None or interview.user_id != user_id:
        raise NotFound("这个会话不存在")
    question = session.get(Question, ts.question_id)
    if question is None:
        raise NotFound("这道题的题目行不见了")

    criteria = bank_repository.criteria_of_question(session, question)
    names = bank_repository.point_names(
        session, {question.primary_point_id} if question.primary_point_id else set()
    )
    return InterviewPageData(
        session=ts,
        interview=interview,
        question=question,
        point_name=names.get(question.primary_point_id or -1, ""),
        criteria=[c.text for c in criteria],
        rounds=interview_service.attempts_of(session, ts.id),
        snapshot=interview_service.current_snapshot(session, ts.id),
        seq=ts.seq,
        total_questions=len(interview_service.sessions_of(session, interview.id)),
    )
