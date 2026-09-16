"""简历 → 结构化档案 → 私有题集（决策 8）。

**它住在 `offline` 而不是 `profile`**：这一步要**写题目表**，而 `CONTEXT.md` 与
ADR-0005 都定了「写题目表的只有 `bank` 自己」。跨领域的动作放编排层 —— 这里正是
ADR-0005 说的那个"固定去处"。

## 隐私（决策 8 的两条硬规定）

① **不保存简历原文**：解析完就把原文丢掉，只留结构化档案。
   档案里有 `source_note`（用户自己写的备注），但那是他主动写的，不是我们从原文剪的。
② 生成的题**是私有的**（`owner_user_id` 非空），只对他可见 —— 隔离由
   `bank/repository.visible_to()` 保证（AGENTS.md §3.5）。

**为什么值得丢掉原文**：留着它等于把"这个人完整的简历"永久存进库里，而它的
唯一用途（解析一次）在解析完就结束了。删掉它是**减少泄露面**，不是省空间。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.db.models import CandidateProfile, Criterion, KnowledgePoint, Question
from app.errors import InvalidInput
from app.llm import LLMError, prompts

logger = logging.getLogger(__name__)

#: 一次生成几道私有题。**宁少勿滥**（prompt 里也写了）：泛泛的题对用户价值是负的。
DEFAULT_QUESTION_COUNT = 6

#: 简历原文的长度上限。它是**输入**不是存储，但一个超长文本会烧掉大量 token，
#: 且几乎必然是粘贴错了（贴了一整本书）。上限值属"可重标定"那一类。
MAX_RESUME_CHARS = 20000


@dataclass
class GeneratedQuestion:
    """一道待落库的私有题。"""

    stem: str
    kind: str
    difficulty: int
    source: str
    criteria: list[str] = field(default_factory=list)


@dataclass
class GenerationResult:
    profile_id: int
    questions_created: int
    questions: list[GeneratedQuestion] = field(default_factory=list)
    llm_failed: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_resume(text: str, *, llm) -> dict[str, Any]:
    """把简历原文解析成结构化档案。**不落库、不留原文。**

    失败时抛 `LLMError`（由调用方决定怎么给用户看）—— 这里不吞异常：
    "解析失败"与"解析成功但内容很少"必须能让用户区分（AGENTS.md §3.1）。
    """
    cleaned = (text or "").strip()
    if len(cleaned) < 50:
        # 太短的多半是误操作（贴了个标题）。明确拒绝，而不是生成一份空档案。
        raise InvalidInput("简历内容太短（至少 50 字）—— 请粘贴完整的简历正文")
    if len(cleaned) > MAX_RESUME_CHARS:
        raise InvalidInput(f"简历内容超过上限（{MAX_RESUME_CHARS} 字）—— 请只粘贴正文部分")

    data, _ = llm.chat_json(
        [{"role": "user", "content": prompts.load("offline/parse_resume.md").render(resume=cleaned)}]
    )
    if not isinstance(data, dict):
        raise LLMError("简历解析返回的形状不对（期望一个 JSON 对象）")

    # 规范化：缺字段补空，**不编造**。模型漏给的内容就是"简历里没说"。
    profile = {
        "headline": str(data.get("headline") or "").strip(),
        "years": str(data.get("years") or "未提及").strip(),
        "skills": [str(s).strip() for s in (data.get("skills") or []) if str(s).strip()],
        "projects": [],
    }
    for raw in data.get("projects") or []:
        if not isinstance(raw, dict):
            continue
        profile["projects"].append(
            {
                "name": str(raw.get("name") or "").strip(),
                "role": str(raw.get("role") or "").strip(),
                "tech": [str(t).strip() for t in (raw.get("tech") or []) if str(t).strip()],
                "highlights": [str(h).strip() for h in (raw.get("highlights") or []) if str(h).strip()],
                "probe_points": [
                    str(p).strip() for p in (raw.get("probe_points") or []) if str(p).strip()
                ],
            }
        )
    return profile


def save_profile(session: Session, *, user_id: int, structured: dict[str, Any], note: str = "") -> CandidateProfile:
    """落库结构化档案。**原文不进来**（参数里就没有它）。"""
    profile = CandidateProfile(user_id=user_id, structured=structured, source_note=note or None)
    session.add(profile)
    session.flush()
    return profile


def latest_profile(session: Session, user_id: int) -> CandidateProfile | None:
    from sqlalchemy import select

    return session.execute(
        select(CandidateProfile)
        .where(CandidateProfile.user_id == user_id)
        .order_by(CandidateProfile.id.desc())
    ).scalars().first()


# ---------------------------------------------------------------------------
# 出题
# ---------------------------------------------------------------------------
def generate_questions(profile: dict[str, Any], *, llm, count: int = DEFAULT_QUESTION_COUNT) -> list[GeneratedQuestion]:
    """由档案出私有题。返回**未落库**的题（落库在 `create_private_question_set`）。"""
    import json

    data, _ = llm.chat_json(
        [
            {
                "role": "user",
                "content": prompts.load("offline/generate_private_questions.md").render(
                    profile=json.dumps(profile, ensure_ascii=False, indent=2), count=count
                ),
            }
        ]
    )
    payload = data if isinstance(data, dict) else {}
    out: list[GeneratedQuestion] = []
    for raw in payload.get("questions") or []:
        if not isinstance(raw, dict):
            continue
        stem = str(raw.get("stem") or "").strip()
        if not stem:
            continue
        kind = str(raw.get("kind") or "design").strip()
        if kind not in ("design", "knowledge"):
            # 决策 20：公共题库只有 knowledge / design 两类，私有题也只出这两类
            # （项目深挖题的特征由 `source` 承载，不新增枚举值 —— 那会动 schema）
            kind = "design"
        try:
            difficulty = int(raw.get("difficulty") or 3)
        except (TypeError, ValueError):
            difficulty = 3
        criteria = [str(c).strip() for c in (raw.get("criteria") or []) if str(c).strip()]
        out.append(
            GeneratedQuestion(
                stem=stem,
                kind=kind,
                difficulty=min(5, max(1, difficulty)),
                source=str(raw.get("source") or "").strip(),
                criteria=criteria,
            )
        )
    return out


def _private_anchor(session: Session, user_id: int) -> KnowledgePoint:
    """给私有题找一个**判分锚点**：每位用户一个 `status='draft'` 的知识点。

    ## 为什么需要它（这是设计取舍，不是凑数）

    私有题来自简历，而知识层是按**公共题库**建的 —— "这道私有题该挂哪个知识点"
    没有可靠的自动答案。而 `criteria.point_id` 是 NOT NULL，**没有挂载点就没有
    考察点，没有考察点就无法判分**（判分会走"全部未涉及"那条降级路径）。

    所以只有两条路：

    · **猜一个公共知识点** → 题挂错、掌握度矩阵被污染，而错挂还看不出来
    · **给一个显式的私有锚点**（本选择）→ 不污染公共知识地图，且这道题立刻可判分

    选后者。它与决策 46 的纪律一致：**绝不允许自动新建公共知识点**（骨架的判定权
    在人手上）—— 但这不是骨架节点，它是 `status='draft'` 的私有容器，不进人审队列、
    不进掌握度矩阵的公共视图（矩阵只按考察点命中算，而这个节点的考察点只属于
    这一个人的私有题）。

    公共题**永远不会**挂到它上面：`primary_point_id` 由 `bank` 的挂载流程写，
    而那条流程只认已确认的知识点。
    """
    from sqlalchemy import select

    name = f"私有题集（用户 {user_id}）"
    # ⚠️ 认"是不是同一个用户"靠 **owner 列**（迁移 0007，决策 91），不是靠名字：
    # 名字是给人看的，而注销那条路要按 owner 找到它并整体删掉 —— 两处用同一个
    # 字符串约定，改一处就会静默漏删（留下简历派生的考察点）。名字仍然这么写，
    # 因为它是**用户看得见**的东西（页面上会显示这个知识点）。
    existing = session.execute(
        select(KnowledgePoint).where(KnowledgePoint.owner_user_id == user_id)
    ).scalars().first()
    if existing is not None:
        return existing

    # `domain_id` 必填：挂到一个专门的领域下（没有就建）。
    # 它同样是 draft 性质的容器，不参与知识地图的人审。
    from app.db.models import Domain

    domain = session.execute(
        select(Domain).where(Domain.name == "私有题集")
    ).scalars().first()
    if domain is None:
        domain = Domain(name="私有题集")
        session.add(domain)
        session.flush()

    point = KnowledgePoint(
        domain_id=domain.id,
        name=name,
        status="draft",      # ← 不是 confirmed：它没经过人审，也不该被当成公共骨架
        origin="manual",
        owner_user_id=user_id,   # ← 注销时按它整体删掉（决策 91）
    )
    session.add(point)
    session.flush()
    return point


def create_private_question_set(
    session: Session,
    *,
    user_id: int,
    resume_text: str,
    llm,
    count: int = DEFAULT_QUESTION_COUNT,
    note: str = "",
) -> GenerationResult:
    """一整条：解析 → 落档案 → 出题 → 落私有题集。

    失败的降级方式：**档案解析失败就整个失败**（没有档案就无从出题）；
    但**出题失败时档案仍然保留** —— 用户至少不必重贴一次简历。
    """
    structured = parse_resume(resume_text, llm=llm)
    profile = save_profile(session, user_id=user_id, structured=structured, note=note)
    result = GenerationResult(profile_id=profile.id, questions_created=0)

    try:
        questions = generate_questions(structured, llm=llm, count=count)
    except LLMError as e:
        logger.warning("由档案出题失败（档案已保留，用户不必重贴简历）：%s", e)
        result.llm_failed = True
        result.note = "档案已保存，但出题这次没成功 —— 可以稍后重试（不必重贴简历）"
        return result

    point = _private_anchor(session, user_id)
    for gq in questions:
        # 幂等：同一份档案重复生成不该出现两道一模一样的题
        if bank_repository.stem_exists(session, gq.stem):
            continue
        question = Question(
            kind=gq.kind,
            stem=gq.stem,
            difficulty=gq.difficulty,
            primary_point_id=point.id,
            origin="generated",
            owner_user_id=user_id,      # ← 非空即私有（决策 9 的隔离靠这一列）
            visibility="private",
            answer_tier="long_tail",    # 冷门题只给评分标准（决策 12）
        )
        session.add(question)
        session.flush()

        # 考察点挂在这个私有锚点下，**并且认到这道题上**（决策 93）：私有题集的锚点
        # 一个用户只有一个，不认题的话 8 道题会共享全部考察点 —— 每道题都被拿别人的
        # 考察点判分（实测）。判分那条路（`criteria_of_question`）优先读题级的。
        for seq, text in enumerate(gq.criteria, start=1):
            session.add(
                Criterion(point_id=point.id, seq=seq, text=text, shared=0, question_id=question.id)
            )
        result.questions.append(gq)
        result.questions_created += 1

    if result.questions_created == 0:
        result.note = "这次没有生成新题（可能都已存在，或模型没给出可用题目）"
    return result
