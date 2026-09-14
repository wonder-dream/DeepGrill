"""知识层构建管道（决策 26 / 44 / 46 / 47，ADR-0002）。

把题库变成**知识地图 + 每题的知识点归属**。工序与判据在
`docs/知识层构建管道.md`（那份文档声明"实现后由本领域的代码与 prompt 替代"），
本模块是它的落地。

## 四个阶段，各自可重跑

```
① 提候选      propose_points(questions)   LLM 从题干+criteria 提候选知识点
② 归并        （见下：MVP 用折叠去重，全量期用嵌入聚类）
③ 人审        review queue → apply_review  通过 / 否决 / 合并
④ 挂载        mount_questions()            每题 → 一个主知识点
```

**⑤ 前置依赖边**（`--` 决策 48）没有做，理由在 `derive_edges` 的 docstring 里 ——
它是全链最弱一环，而"从名字推边基本在猜"。诚实地留空比装满猜测好。

## 为什么这一笔的归并用"折叠去重"而不是嵌入聚类

决策 44 定的是「两侧去重」：先用幂等哈希折叠**完全相同的候选**，再跨批用嵌入聚
类。MVP 期题库是 8 道种子题，一次调用就能提完所有候选，跨批重复根本不会发生 ——
所以这里只做①的**归一化折叠**（`normalize_name`），把嵌入聚类留到全量期。

**这不是"先不做"，是"按数据量选工序"**：判据在 `docs/知识层构建管道.md`
（分批才需要嵌入，因为分批必然跨批重复）。全量期要补的是"按批提候选 + 嵌入粗筛
+ 逐簇 LLM 判断"，而那三样都要等 v1 导入之后才有意义。
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Criterion, Domain, KnowledgePoint, Question
from app.llm import LLMError, prompts

logger = logging.getLogger(__name__)

#: 一条知识点下的考察点通常 2-5 条（决策 26 / 判据①）。
MIN_CRITERIA = 2
MAX_CRITERIA = 6


@dataclass
class Candidate:
    """LLM 提出来的一条候选知识点。"""

    name: str
    definition: str = ""
    exclusions: str = ""
    criteria: list[str] = field(default_factory=list)
    #: 支持它的题目 id（用来判断这条候选是"真结构"还是"一道题的孤例"）
    question_ids: list[int] = field(default_factory=list)

    @property
    def unusable_reason(self) -> str | None:
        """判据① 的机器可判部分：**写不出考察点就不是知识点**。

        人的判断仍不可替代（`docs/知识层构建管道.md` 的验收表：抽查 3 个批次看
        有没有把"一道题的主题"当成知识点）。这里只挡掉机器能确定的那几种。
        """
        if not self.name.strip():
            return "没有名字"
        if len(self.criteria) < MIN_CRITERIA:
            return f"考察点少于 {MIN_CRITERIA} 条（写不出「什么算答到」）"
        return None


@dataclass
class ProposalResult:
    candidates: list[Candidate] = field(default_factory=list)
    llm_failed: bool = False
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        """落盘用（**审核页需要它跨进程存在**）。

        为什么不放内存：人审可能隔天做（几百条要看很久），而且审核页是另一个进程。
        写成 JSON 文件是这里最轻的持久化 —— 它天然会过期（题变了就该重跑），
        所以不该进库当业务数据。
        """
        return {
            "candidates": [
                {
                    "name": c.name,
                    "definition": c.definition,
                    "exclusions": c.exclusions,
                    "criteria": c.criteria,
                    "question_ids": c.question_ids,
                    # 把判据① 的结论一起存下来 —— 审核页要显示"这条为什么可疑"
                    "unusable_reason": c.unusable_reason,
                }
                for c in self.candidates
            ],
            "llm_failed": self.llm_failed,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, raw: object) -> ProposalResult:
        if not isinstance(raw, dict):
            return cls(note="提案文件格式不对")
        out = cls(llm_failed=bool(raw.get("llm_failed")), note=str(raw.get("note") or ""))
        for item in raw.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            out.candidates.append(
                Candidate(
                    name=str(item.get("name") or ""),
                    definition=str(item.get("definition") or ""),
                    exclusions=str(item.get("exclusions") or ""),
                    criteria=[str(c) for c in (item.get("criteria") or [])],
                    question_ids=[int(i) for i in (item.get("question_ids") or []) if str(i).isdigit()],
                )
            )
        return out


def normalize_name(name: str) -> str:
    """候选名的**归一化**键，用于折叠同一批次里的重复。

    归一化 = NFKC（全角转半角）+ 去空白与常见标点 + 小写。这套规则继承 v1 的
    去重归一化（`docs/v1行为规格.md` §2.5 标为「继承」）—— 它解决的是同一个问题：
    同一个东西被写成不同形状。

    **它只折叠"字面就一样"的**，不做语义归并 —— 后者是聚类与 LLM 判断的职责
    （决策 44）。把两件事混在一起，会让"为什么这两条被合并了"无法回答。
    """
    s = unicodedata.normalize("NFKC", name or "").strip().lower()
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"[·・\-—_/\\()（）\[\]【】:：,，.。]+", "", s)
    return s


def propose_points(questions: list[Question], *, llm) -> ProposalResult:
    """① 从一批题里提候选知识点。

    ⚠️ **题量少时才这么用**：一次调用提完所有候选。全量期（三千道题）必须分批 +
    嵌入聚类（决策 44），因为分批必然跨批重复，而"一次全局归并"会卡在上下文长度上。
    """
    if not questions:
        return ProposalResult(note="没有题可提")

    blocks = []
    for q in questions:
        blocks.append(f"[题 {q.id}] 题型：{q.kind}｜难度：{q.difficulty}\n题干：{q.stem}")
    payload = "\n\n".join(blocks)

    try:
        data, _ = llm.chat_json(
            [
                {
                    "role": "user",
                    "content": prompts.load("offline/propose_points.md").render(
                        questions=payload, count=len(questions)
                    ),
                }
            ]
        )
    except LLMError as e:
        logger.warning("提候选失败：%s", e)
        return ProposalResult(llm_failed=True, note=f"提候选失败：{e}")

    result = ProposalResult()
    payload_obj = data if isinstance(data, dict) else {}
    for raw in payload_obj.get("points") or []:
        if not isinstance(raw, dict):
            continue
        criteria = [str(c).strip() for c in (raw.get("criteria") or []) if str(c).strip()]
        result.candidates.append(
            Candidate(
                name=str(raw.get("name") or "").strip(),
                definition=str(raw.get("definition") or "").strip(),
                exclusions=str(raw.get("exclusions") or "").strip(),
                criteria=criteria[:MAX_CRITERIA],
                question_ids=[int(i) for i in (raw.get("question_ids") or []) if str(i).isdigit()],
            )
        )
    result.candidates = fold_duplicates(result.candidates)
    return result


def fold_duplicates(candidates: list[Candidate]) -> list[Candidate]:
    """把归一化后同名的候选折成一条（**并集**它们的题目与考察点）。

    这是决策 44 的第一步（"幂等哈希折叠完全相同"）在候选层的最小实现。
    """
    folded: dict[str, Candidate] = {}
    for cand in candidates:
        key = normalize_name(cand.name)
        if not key:
            continue
        existing = folded.get(key)
        if existing is None:
            folded[key] = Candidate(
                name=cand.name,
                definition=cand.definition,
                exclusions=cand.exclusions,
                criteria=list(cand.criteria),
                question_ids=list(cand.question_ids),
            )
            continue
        for cid in cand.question_ids:
            if cid not in existing.question_ids:
                existing.question_ids.append(cid)
        for text in cand.criteria:
            if text not in existing.criteria:
                existing.criteria.append(text)
        if not existing.definition and cand.definition:
            existing.definition = cand.definition
    return list(folded.values())


# ---------------------------------------------------------------------------
# ③ 人审
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    """人对一条候选的决定。"""

    index: int
    action: str          # approve / reject / merge_into
    name: str = ""       # approve 时可改名
    merge_into: int = 0  # merge_into 时的目标下标


@dataclass
class ApplyResult:
    created_points: int = 0
    rejected: int = 0
    merged: int = 0
    skipped: list[str] = field(default_factory=list)


def apply_review(
    session: Session,
    *,
    candidates: list[Candidate],
    decisions: list[Decision],
    domain_name: str,
) -> ApplyResult:
    """把人的决定落库：通过 → 建知识点 + 考察点；否决 → 不建；合并 → 并到目标。

    **域是新建的**（MVP）：人审时给一个领域名，管道建域并把这批知识点挂进去。
    真正的"领域有哪些"也该由人定（决策 26：前两层人审），但 MVP 里一次装配就是
    一个域，够用且不假装有更多结构。

    `skipped` 记录**被机器规则挡下**的候选（判据① 的机器可判部分）—— 不静默丢弃。
    """
    result = ApplyResult()
    by_index: dict[int, int] = {}   # 候选下标 → 落库后的知识点 id
    approved: list[tuple[int, Candidate]] = []

    # 第一遍：合并先解析到"最终要建的那条"（合并不建自己的节点）
    resolved: dict[int, int] = {}
    for decision in decisions:
        if decision.action == "merge_into":
            resolved[decision.index] = decision.merge_into
    for index in range(len(candidates)):
        target = resolved.get(index)
        seen = set()
        while target is not None and target in resolved and target not in seen:
            seen.add(target)
            target = resolved.get(target)
        if target is not None:
            resolved[index] = target

    for decision in decisions:
        if decision.action == "reject":
            result.rejected += 1
            continue
        if decision.action == "merge_into":
            result.merged += 1
            continue
        if decision.action != "approve":
            result.skipped.append(f"候选 {decision.index}：未知动作 {decision.action!r}")
            continue
        if decision.index < 0 or decision.index >= len(candidates):
            result.skipped.append(f"候选 {decision.index}：下标越界")
            continue
        cand = candidates[decision.index]
        reason = cand.unusable_reason
        if reason:
            result.skipped.append(f"候选「{cand.name}」被机器规则挡下：{reason}")
            continue
        approved.append((decision.index, _renamed(cand, decision.name)))

    if not approved:
        return result

    domain = _get_or_create_domain(session, domain_name)
    for index, cand in approved:
        point = _commit_point(session, domain_id=domain.id, cand=cand)
        by_index[index] = point.id
        result.created_points += 1
        # 建完就把这条候选**自己的题**挂上去。
        # （第一版漏了这一步：只有"被合并掉的"候选才挂题，于是**通过的那些反而没挂** ——
        #   测试当场抓到。挂载是这一步的一半职责，不是只在合并时才发生。）
        _attach_questions(session, point_id=point.id, question_ids=cand.question_ids)

    # 合并：把被合并的候选的**题目**并到目标知识点上（考察点不并 ——
    # 归并哪几条考察点属于人审第 3 步的判断，这里只保证题目不丢）
    for index, target in resolved.items():
        if target not in by_index:
            continue
        cand = candidates[index]
        if not cand.question_ids:
            continue
        _attach_questions(session, point_id=by_index[target], question_ids=cand.question_ids)

    return result


def _renamed(cand: Candidate, name: str) -> Candidate:
    if not name.strip() or name.strip() == cand.name:
        return cand
    return Candidate(
        name=name.strip(),
        definition=cand.definition,
        exclusions=cand.exclusions,
        criteria=list(cand.criteria),
        question_ids=list(cand.question_ids),
    )


def _get_or_create_domain(session: Session, name: str) -> Domain:
    domain = session.execute(select(Domain).where(Domain.name == name)).scalars().first()
    if domain is None:
        domain = Domain(name=name)
        session.add(domain)
        session.flush()
    return domain


def _commit_point(session: Session, *, domain_id: int, cand: Candidate) -> KnowledgePoint:
    """建知识点 + 它的考察点。**幂等**：同名同域已存在就复用、不重复建考察点。"""
    point = session.execute(
        select(KnowledgePoint).where(
            KnowledgePoint.domain_id == domain_id, KnowledgePoint.name == cand.name
        )
    ).scalars().first()
    if point is None:
        point = KnowledgePoint(
            domain_id=domain_id,
            name=cand.name,
            status="confirmed",     # ← 人审通过才是 confirmed（决策 26）
            origin="proposed",
            exclusions=cand.exclusions or None,
        )
        session.add(point)
        session.flush()

    existing = {
        c.text for c in session.execute(
            select(Criterion).where(Criterion.point_id == point.id)
        ).scalars()
    }
    seq = len(existing)
    for text in cand.criteria:
        if text in existing:
            continue
        seq += 1
        session.add(Criterion(point_id=point.id, seq=seq, text=text, shared=0))
    session.flush()
    return point


def _attach_questions(session: Session, *, point_id: int, question_ids: list[int]) -> int:
    """把题挂到这个知识点上（只填**空的** `primary_point_id`）。

    ⚠️ 不覆盖已有挂载：挂载自修复（`offline` 的另一职责）才是改挂载的地方，
    而上线前的一次性装配不该顺手把别人已确认的挂载改掉。
    """
    updated = 0
    for qid in question_ids:
        question = session.get(Question, qid)
        if question is None or question.primary_point_id is not None:
            continue
        question.primary_point_id = point_id
        updated += 1
    session.flush()
    return updated


# ---------------------------------------------------------------------------
# ④ 挂载（独立于提候选：它按**已确认的**知识点工作）
# ---------------------------------------------------------------------------
@dataclass
class MountResult:
    mounted: int = 0
    left_for_review: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def mount_questions(session: Session, *, questions: list[Question], llm, batch_size: int = 20) -> MountResult:
    """把题挂到**已确认的**知识点上。

    挂不上的题**不自动新建知识点**（决策 46）—— 它们进"待定池"（这里用返回值表达，
    全量期会落库）。这条纪律的理由：自动新建等于让 LLM 悄悄改知识地图，
    而骨架的判定权在人手上。

    分批是**有意留的接口**：批大小正是 `docs/知识层构建管道.md` 里第一个该试的参数。
    """
    result = MountResult()
    points = session.execute(
        select(KnowledgePoint).where(KnowledgePoint.status == "confirmed")
    ).scalars().all()
    if not points:
        return result

    catalog = "\n".join(f"{p.id}. {p.name}" for p in points)
    for start in range(0, len(questions), batch_size):
        batch = questions[start : start + batch_size]
        lines = []
        for q in batch:
            lines.append(f"[题 {q.id}] {q.stem}")
        try:
            data, _ = llm.chat_json(
                [
                    {
                        "role": "user",
                        "content": prompts.load("offline/mount_questions.md").render(
                            catalog=catalog, questions="\n".join(lines)
                        ),
                    }
                ]
            )
        except LLMError as e:
            logger.warning("挂载失败（这一批进待定池）：%s", e)
            result.left_for_review += len(batch)
            continue

        known = {p.id for p in points}
        assignments = (data if isinstance(data, dict) else {}).get("assignments") or []
        decided: set[int] = set()
        for item in assignments:
            if not isinstance(item, dict):
                continue
            try:
                qid = int(item.get("question_id"))
            except (TypeError, ValueError):
                continue
            point_id = item.get("point_id")
            question = next((q for q in batch if q.id == qid), None)
            if question is None or question.primary_point_id is not None:
                continue
            decided.add(qid)
            if point_id is None or int(point_id) not in known:
                # 模型说"挂不上"或给了个不存在的知识点 → 待定池（不新建）
                result.left_for_review += 1
                result.details.append({"question_id": qid, "reason": "挂不上或目标不存在"})
                continue
            question.primary_point_id = int(point_id)
            result.mounted += 1
            result.details.append({"question_id": qid, "point_id": int(point_id)})
        # 模型漏答的题也进待定池 —— 不能因为"没提到"就当它挂好了
        for q in batch:
            if q.id not in decided and q.primary_point_id is None:
                result.left_for_review += 1
                result.details.append({"question_id": q.id, "reason": "模型没给结果"})
    session.flush()
    return result


def derive_edges(*args: object, **kwargs: object) -> list[object]:
    """⑤ 前置依赖边 —— **故意没实现**，返回空。

    决策 48 已经承认这一步是全链最弱一环："从知识点名字推边属于开放集关系推断 ——
    工具只有两个名字，没有内容依据，LLM 基本在猜"。它要求的是**只做两类有依据的边**
    （挂载时采集的「涉及概念」+ 领域内限深推断），并强制无环。

    而"挂载时采集涉及概念"需要一个**还没做的**采集步骤（`docs/知识层构建管道.md`
    第 5 步的附加产物），所以现在实现它只能是"按名字猜" —— 那正是决策 48 要避免的。

    **留空比装满猜测好**：一个错的前置边会把用户带到错的方向（"先补 X"），
    而缺一条边的代价只是少一条推荐。等有真实作答数据后，真正的可靠信号是
    "会 A 的人是否大概率会 B"（决策 48 末段）。
    """
    return []


# ---------------------------------------------------------------------------
# ⑥ 全量装配：分批提候选 + 嵌入粗筛聚类 + 逐簇 LLM 判断（决策 44/46）
# ---------------------------------------------------------------------------
# MVP 期只有 8 道种子题，一次调用就提完了（上面的 `propose_points`）。全量期有
# 三千道题，必须换工序 —— 而换工序的**判据**不是"代码更漂亮"，是两条硬约束：
#
#   ① **上下文长度**：三千道题的题干塞不进一次调用
#   ② **分批必然跨批重复**：每批都看不到别的批，同一个知识点会被提出来两次
#
# 所以决策 44 定的顺序是：分批提 → 嵌入粗筛 → LLM 逐簇判断归并。
# 三条原则贯穿这一节：
#
#   · **聚类只做"粗筛"**：它按用词相似度分组，**不决定谁和谁合并** ——
#     判断权在 LLM（逐簇）与人（人审）手里。
#   · **确定性**：同样的输入得到同样的簇（见 `cluster_by_score`）。随机性会让
#     "为什么这次聚出来不一样"无从解释，而这份结果要人审。
#   · **挂不上的留在待定池**（决策 46）：绝不自动新建知识点。

#: 一批多少道题去提候选。它同时受两个东西约束：上下文长度（越大越容易被截断）
#: 与"跨批重复"（越小重复越多）。40 是 `docs/知识层构建管道.md` 里给的起点，
#: 该按实测重标定（那份文档点明了它是第一个该试的参数）。
PROPOSE_BATCH = 40
#: 粗筛聚类的相似度阈值。**同样是待标定的**：太高 → 同一个知识点被拆成好几条候选
#: （人审时要合并很多次）；太低 → 不同的知识点被并到一起（人审时要拆开，更难）。
CLUSTER_THRESHOLD = 0.86


def batch_questions(questions: list[Question], *, size: int = PROPOSE_BATCH) -> list[list[Question]]:
    """切批。**按 id 排序再切** —— 顺序稳定，重跑时批次划分一致（便于对比两次结果）。"""
    ordered = sorted(questions, key=lambda q: q.id)
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。任一向量为零（空文本）时返回 0 —— **不是报错**：

    空题干确实存在（导入的脏数据），它该被丢进"自己一簇"，而不是让整轮聚类炸掉。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def cluster_by_score(
    vectors: dict[str, list[float]], *, threshold: float = CLUSTER_THRESHOLD
) -> list[list[str]]:
    """按相似度分组（**leader 聚类**：每个还没归组的键开一个新簇，然后把剩下的
    与它相似度达标的都拉进来）。

    为什么不用 k-means / 层次聚类：

    · **确定性**：leader 聚类对同样的输入给出同样的结果（k-means 依赖随机初值）。
      这份结果要人审，"这次为什么和上次不一样"必须能回答。
    · **簇数不用先定**：知识点有多少个是**结果**，不是输入。
    · 它只做**粗筛**：宁可多分几簇（人审时合并），也不要把不同知识点并到一起
      （人审时拆开更难）。所以阈值取高。

    `vectors` 的键顺序决定簇的顺序 —— 调用方传进来的就是排好序的 ref_id。
    """
    unassigned = list(vectors.keys())
    clusters: list[list[str]] = []
    while unassigned:
        seed = unassigned.pop(0)
        group = [seed]
        rest: list[str] = []
        for ref in unassigned:
            if cosine(vectors[seed], vectors[ref]) >= threshold:
                group.append(ref)
            else:
                rest.append(ref)
        unassigned = rest
        clusters.append(group)
    return clusters


@dataclass
class BatchProposal:
    """全量装配第一阶段的产出。"""

    candidates: list[Candidate] = field(default_factory=list)
    batches: int = 0
    failed_batches: int = 0
    notes: list[str] = field(default_factory=list)


def propose_batched(
    session: Session, *, questions: list[Question], llm, batch_size: int = PROPOSE_BATCH
) -> BatchProposal:
    """① 分批提候选。**某一批失败不终止整轮** —— 记下来，继续跑（§3.1）。

    失败的那一批留在待定池里（它的题没被任何候选认领），下一轮再跑一次就会补上。
    """
    out = BatchProposal()
    for index, batch in enumerate(batch_questions(questions, size=batch_size), start=1):
        out.batches += 1
        result = propose_points(batch, llm=llm)
        if result.llm_failed:
            out.failed_batches += 1
            out.notes.append(f"第 {index} 批提候选失败：{result.note}")
            continue
        out.candidates.extend(result.candidates)
    out.candidates = fold_duplicates(out.candidates)
    return out


@dataclass
class ClusterJudgement:
    """逐簇判断的结果（决策 44 的第二阶段）。"""

    merged: int = 0
    kept: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def juddge_clusters(
    session: Session,
    *,
    clusters: list[list[Candidate]],
    llm,
) -> tuple[list[Candidate], ClusterJudgement]:
    """③ 逐簇让 LLM 判断归并：把一簇里的候选合成一条（或保持原样）。

    它是"分批必然跨批重复"的解法里**唯一需要判断**的一步：聚类只按用词分组，
    而"线程池"与"线程池参数"是不是同一个知识点，得读懂它们才能定。

    失败的那一簇**保持原样**（不合并）—— 保守方向：多留几条候选让人审时合并，
    比把两条不同的知识点合成一条（人审时要拆开）代价小。
    """
    out: list[Candidate] = []
    judgement = ClusterJudgement()
    for group in clusters:
        if len(group) == 1:
            out.extend(group)
            judgement.kept += 1
            continue
        merged = merge_cluster(group, llm=llm)
        if merged is None:
            judgement.failed += 1
            judgement.notes.append(
                f"归并失败（保持原样）：{' / '.join(c.name for c in group[:3])}…"
            )
            out.extend(group)
            continue
        out.append(merged)
        judgement.merged += 1
    return out, judgement


def merge_cluster(group: list[Candidate], *, llm) -> Candidate | None:
    """让模型把一簇候选合成一条。失败返回 `None`（调用方保持原样）。

    合并的是**候选**而不是题：题目的归属由候选的 `question_ids` 并集带过来，
    所以合并之后不会有题掉队（这一条有测试钉着）。
    """
    blocks = []
    for cand in group:
        blocks.append(
            f"- {cand.name}｜定义：{cand.definition or '（无）'}"
            f"｜考察点：{'；'.join(cand.criteria) or '（无）'}"
        )
    try:
        data, _ = llm.chat_json(
            [
                {
                    "role": "user",
                    "content": prompts.load("offline/merge_candidates.md").render(
                        candidates="\n".join(blocks), count=len(group)
                    ),
                }
            ]
        )
    except LLMError as e:
        logger.warning("归并一簇失败：%s", e)
        return None

    payload = data if isinstance(data, dict) else {}
    name = str(payload.get("name") or "").strip()
    if not name:
        logger.warning("归并结果没有名字 —— 按「保持原样」处理")
        return None
    question_ids: list[int] = []
    for cand in group:
        for qid in cand.question_ids:
            if qid not in question_ids:
                question_ids.append(qid)
    return Candidate(
        name=name,
        definition=str(payload.get("definition") or "").strip(),
        exclusions=str(payload.get("exclusions") or "").strip(),
        criteria=[str(c).strip() for c in (payload.get("criteria") or []) if str(c).strip()][
            :MAX_CRITERIA
        ],
        question_ids=question_ids,
    )


def cluster_candidates(
    session: Session,
    *,
    candidates: list[Candidate],
    embeddings,
) -> tuple[list[list[Candidate]], dict[str, int]]:
    """② 嵌入粗筛：把候选按"名字 + 定义"的向量分组。

    返回 `(簇, 报告)`。报告里的两个数（`embedded` / `reused`）是缓存的效果 ——
    重跑时应当几乎全是 `reused`，否则说明缓存没生效（那是一个会静默烧钱的 bug）。
    """
    from app.offline import embedding_store

    texts = {embedding_store.ref_for_text(_candidate_text(c)): _candidate_text(c) for c in candidates}
    by_ref = {embedding_store.ref_for_text(_candidate_text(c)): c for c in candidates}
    vectors, report = embedding_store.vectors_for(
        session, items=texts, kind=embedding_store.CANDIDATE, embeddings=embeddings
    )
    # 顺序稳定：按 ref 排序再聚类（`cluster_by_score` 的结果依赖键顺序）
    ordered = {ref: vectors[ref] for ref in sorted(vectors)}
    groups = cluster_by_score(ordered)
    return [[by_ref[ref] for ref in group] for group in groups], {
        "embedded": report.embedded,
        "reused": report.reused,
    }


def _candidate_text(cand: Candidate) -> str:
    return f"{cand.name}｜{cand.definition}"
