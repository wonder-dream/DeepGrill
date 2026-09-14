"""晋升（私有题 → 公共题库）与它的**自动门禁**（决策 5、9）。

基线的两条原话：

> 私有题集**用户隔离**；用户可主动**晋升**单题到公共题库（经内容门禁）

> 门禁：**自动门禁进池 + 事后治理**（人工不再前置逐题审）

## 门禁为什么必须是"自动 + 可解释"

它每天都在跑（每次用户点晋升），而**人不在回路里**。所以两条约束：

· **判据必须是确定性的** —— 一个需要模型打分的门禁会让"同一道题今天过、明天不过"，
  而用户无从申诉。这里全是可复现的检查（长度、去重、有没有考察点、题型白名单）。
· **失败必须说清是哪一条**（§3.1）。返回的是**每条检查的结果**，不是一句"没过"。
  用户据此能改（把题干写长一点、别提交重复的题），而不是反复试。

## 过了门禁之后去哪：**公共待定池**，不是"立刻可用"

晋升**不清除**题的知识点归属是不够的 —— 私有题挂在一个**per-user 的 draft 锚点**上
（`profile_pipeline._private_anchor`，那是"让私有题立刻可判分"的权宜之计）。把这道题
直接标成公共、却留着那个锚点，会让公共题库里出现"挂在某个人的私有草稿点上"的题；
而那个锚点会随用户注销被清理，题就悬空了。

所以晋升把题送进**待定池**（`visibility='public'` + `primary_point_id=NULL`），
由知识层管道把它挂到人审过的知识点上（决策 46：**绝不允许自动新建知识点**）。
代价说清楚：**挂上去之前它没有考察点**，判分只能按题干自己判断 —— 页面上写着这件事。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.bank import repository
from app.db.models import KnowledgePoint, Question
from app.errors import Forbidden, InvalidInput

#: 题干的长度区间。下限是"太短的东西不构成一道面试题"（实测生成过 6 个字的题），
#: 上限与 v1 的字段约定一致（`attempts.answer_text` 那类长文本另有上限）。
STEM_MIN = 10
STEM_MAX = 500

#: 可以进公共题库的题型（决策 20：公共题库只保留 knowledge / design 两类）。
PUBLIC_KINDS = ("knowledge", "design")


@dataclass(frozen=True)
class GateCheck:
    """一条门禁检查的结果。`why` 是**给用户看的人话**，不是给日志看的。"""

    name: str
    ok: bool
    why: str


@dataclass
class GateResult:
    checks: list[GateCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        """失败原因的一句话（页面直接显示它）。"""
        if self.passed:
            return "门禁全部通过"
        return "；".join(c.why for c in self.failures)


def normalize_stem(stem: str) -> str:
    """比对题干时的归一化：去掉空白与常见标点 —— 只差一个句号的题就是同一道题。

    公开的（无下划线）：事后治理的重复检测也要用它。两处各写一套"什么算同一道题"
    就是两套会漂的规则 —— 而门禁与检测对同一道题给出不同结论，是最难解释的一种不一致。
    """
    drop = set(" \t\r\n，。？?！!、；;：:（）()【】[]“”\"'·-—_")
    return "".join(ch for ch in stem.strip().lower() if ch not in drop)


def _duplicate_of(session: Session, question: Question) -> Question | None:
    """在**公共池**里找一道题干相同的题（归一化后）。

    走仓储的 `public_visible_questions`，不自己 `select(Question)` ——
    题目查询只有那一条通道（AGENTS.md §3.5，`app/bank/test_repository.py` 有结构断言）。
    """
    normalized = normalize_stem(question.stem)
    if not normalized:
        return None
    for other in repository.public_visible_questions(session, exclude_id=question.id):
        if normalize_stem(other.stem) == normalized:
            return other
    return None


def _criteria_count(session: Session, question: Question) -> int:
    return len(repository.criteria_of_question(session, question))


def _point(session: Session, question: Question) -> KnowledgePoint | None:
    if question.primary_point_id is None:
        return None
    return session.get(KnowledgePoint, question.primary_point_id)


def run_gate(session: Session, *, question: Question, user_id: int) -> GateResult:
    """跑一遍内容门禁。**只读**，不改任何东西（页面可以随时预演）。"""
    result = GateResult()
    stem = question.stem or ""

    result.checks.append(
        GateCheck(
            "owned",
            question.owner_user_id == user_id,
            "只能晋升你自己的题",
        )
    )
    length = len(stem.strip())
    result.checks.append(
        GateCheck(
            "stem_length",
            STEM_MIN <= length <= STEM_MAX,
            f"题干长度要在 {STEM_MIN}-{STEM_MAX} 字之间（现在是 {length}）",
        )
    )
    result.checks.append(
        GateCheck(
            "kind",
            question.kind in PUBLIC_KINDS,
            f"公共题库只收 {' / '.join(PUBLIC_KINDS)} 两类（这道是 {question.kind}）",
        )
    )
    criteria_count = _criteria_count(session, question)
    result.checks.append(
        GateCheck(
            "criteria",
            criteria_count > 0,
            "这道题还没有考察点定义 —— 没有它，公共题库里的题无法被客观评判",
        )
    )
    duplicate = _duplicate_of(session, question)
    result.checks.append(
        GateCheck(
            "not_duplicate",
            duplicate is None,
            f"公共题库里已经有同一道题了（#{duplicate.id if duplicate else '?'}）",
        )
    )
    return result


def promote(session: Session, *, question: Question, user_id: int) -> GateResult:
    """晋升一道自己的私有题。返回门禁结果（`passed=False` 时**什么都没改**）。

    过闸之后这道题会：
    · `owner_user_id = None`、`visibility = 'public'` —— 它进入公共池
    · `primary_point_id = None` —— 挂载交给知识层管道（决策 46 的待定池）
    · `origin = 'promoted'` —— 来源三类之一（决策 4）

    ⚠️ 它**不扣额度点**：晋升是一次纯数据库写入，没有任何 LLM 调用。
    """
    if question.owner_user_id != user_id:
        # 越权与"不存在"在这里分开：这道题对自己可见（否则调用方拿不到它），
        # 所以"不是你的"是一个明确的拒绝，不需要伪装成 404。
        raise Forbidden("只能晋升你自己的题")
    if question.visibility not in ("private", "pending"):
        raise InvalidInput(f"这道题已经在公共池里了（visibility={question.visibility}）")

    result = run_gate(session, question=question, user_id=user_id)
    if not result.passed:
        # **什么都不改**：题留在私有题集里，用户可以改完再试。
        return result

    question.owner_user_id = None
    question.visibility = "public"
    question.primary_point_id = None  # 进待定池（决策 46）
    question.origin = "promoted"
    session.flush()
    return result


def pending_pool_size(session: Session) -> int:
    """待定池有多大（公共题里没挂知识点的）。

    它走仓储的**计数**入口（`unmounted_public_count`），不自己 `select(Question)`：
    那条规则靠"每次都觉得这次是例外"是守不住的（结构断言就在 `test_repository.py`）。
    也不能用 `len(unmounted_public())` —— 库里几千道题时会全拉进内存（§3.6）。
    """
    return repository.unmounted_public_count(session)
