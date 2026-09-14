"""讲解的按需生成与缓存（决策 67）。

基线把「讲解 / 讲义」列为知识层的**第三类内容**，并写明它**按需生成 + 缓存**
（不预生成、不进审核队列）。这一笔实现它。

## 四件事在这里定下来

① **键 = 题目 id + 版本**，而版本是**算出来的**（`version_of`）：
   `提示词版本 : 题干哈希`。两个失效条件因此自动成立 ——
   改了 prompt（缓存不该复活）、改了题干（讲解不该被当成新题干的讲解）。
   比"再存一列题干、查询时比对"少一处会漏的条件。

② **必须在"这道题可见"之后才谈缓存**。`explain()` 收的是**已经过可见性检查的
   题目行**（`bank.service.detail()` 的产物），不自己查题 —— 题目查询只有
   `bank/repository.py` 那一条通道（AGENTS.md §3.5）。

③ **失败要说出来**（§3.1）：模型挂了就抛 `LLMError` 上去，由页面显示"讲解没生成
   出来"，而不是返回一段空讲解（那会被缓存下来，之后所有人看到的都是空的）。

④ **它是一个进库的状态，所以要有回收者**（§3.2）：`purge()` 按 TTL 与容量上限清，
   由离线任务 `purge_explanations` 执行。终端用户那次请求只负责读与写。

## 为什么不扣额度点

基线里的额度点只有三档（模拟面试 6 / 单题追问 1 / 题库刷题 0），而讲解属于**题库
那一边** —— 给它扣点会让"题库刷题 0 点"这条产品线自相矛盾。成本靠三道防线兜着：
**缓存**（同一道题只生成一次）、**限流**（决策 66 的按用户那一档）、
**token 账本**（决策 14：这次花了多少 token 记在案，能回答"钱花在哪"）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.db.models import Explanation, Question
from app.llm import LLMError, prompts

#: 提示词版本。**改了 `prompts/knowledge/explain_question.md` 就改它** ——
#: 这是"旧缓存不复活"的那个开关。（不改也不会错得离谱：内容变旧而已。但不改的话，
#: "改了 prompt 为什么讲解没变"会变成一个要读源码才能回答的问题。）
PROMPT_VERSION = "explain-1"

#: 回收者的两个上限（§3.2）。TTL 按"讲解的教学内容不会很快过时"给得宽一些；
#: 容量上限是兜底 —— 它保证这张表不会无限长（题目数 × 版本数才是它的自然上限）。
TTL_DAYS = 180
MAX_ROWS = 5000


@dataclass(frozen=True)
class ExplanationResult:
    """一次讲解的读取结果。

    `cached=True` 表示这次没调模型（命中了缓存）—— 页面可以据此显示"这是缓存"，
    而**测试与观测要能区分"真的生成了"与"读到了"**：把两者混起来，缓存失效就会
    静默变成"每次都在花钱"。
    """

    body: str
    version: str
    cached: bool


def _stem_hash(stem: str) -> str:
    return hashlib.sha1(stem.strip().encode("utf-8")).hexdigest()[:12]


def version_of(question: Question) -> str:
    """这道题此刻的讲解版本（缓存键的另一半）。"""
    return f"{PROMPT_VERSION}:{_stem_hash(question.stem)}"


def cached(session: Session, *, question: Question) -> ExplanationResult | None:
    """查缓存。**不改任何东西**（页面渲染路径会调它，那里的失败必须便宜且无副作用）。"""
    row = session.execute(
        select(Explanation).where(
            Explanation.question_id == question.id,
            Explanation.version == version_of(question),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return ExplanationResult(body=row.body, version=row.version, cached=True)


def explain(session: Session, *, question: Question, llm) -> ExplanationResult:
    """取讲解：命中缓存就直接返回，否则生成一次并落库。

    ⚠️ `question` 必须是**已经过可见性检查**的题目行。调用方走
    `bank.service.detail()`（它内部过 `visible_to()`）—— 本模块不自己查题。
    """
    hit = cached(session, question=question)
    if hit is not None:
        return hit

    criteria = bank_repository.criteria_of_question(session, question)
    body = _generate(question, criteria, llm)
    _store(session, question=question, body=body)
    return ExplanationResult(body=body, version=version_of(question), cached=False)


def _generate(question: Question, criteria: list, llm) -> str:
    """调一次模型。**失败抛 `LLMError`，不返回空串。**

    空串会顺着"写进缓存"那条路变成一个**可信的坏数据**：之后所有人打开这道题都看到
    空讲解，而缓存命中不会报错（ADR-0007 的 prompt 纪律同一条：读不到就抛，别返回空）。
    """
    prompt = prompts.load("knowledge/explain_question.md").render(
        stem=question.stem,
        kind=question.kind,
        criteria="\n".join(f"{c.id}. {c.text}" for c in criteria)
        or "（这道题还没有考察点定义 —— 按题干自己判断）",
        reference_answer=question.reference_answer or "",
    )
    # ⚠️ **不要给这里一个小的 max_tokens**：这个模型先推理，而推理吃同一份预算。
    # 第一版写 `max_tokens=2048`，结果每次都拿到空内容（推理用光了、正文没地方写），
    # 而症状看起来像"模型不肯说话"。用客户端默认的 8192。
    reply = llm.chat([{"role": "user", "content": prompt}])
    text = (reply.text or "").strip()
    if not text:
        raise LLMError("模型返回了空的讲解")
    return text


def _store(session: Session, *, question: Question, body: str) -> Explanation:
    """写缓存。**这道题的旧版本一并删掉**。

    留着旧版本没有用处：版本变化只发生在"prompt 改了"或"题干改了"这两种情况下，
    而那时旧讲解对新题干（或新契约）都没有意义。留着只会占着容量上限。
    """
    session.execute(
        delete(Explanation).where(Explanation.question_id == question.id)
    )
    row = Explanation(question_id=question.id, version=version_of(question), body=body)
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# 回收者（AGENTS.md §3.2）
# ---------------------------------------------------------------------------
@dataclass
class PurgeReport:
    by_age: int = 0
    by_capacity: int = 0

    @property
    def total(self) -> int:
        return self.by_age + self.by_capacity

    def __str__(self) -> str:
        return f"按时间清 {self.by_age} 条、按容量清 {self.by_capacity} 条"


def purge(
    session: Session, *, ttl_days: int = TTL_DAYS, max_rows: int = MAX_ROWS
) -> PurgeReport:
    """清掉过期与超量的讲解。**幂等**（跑两遍结果一样），所以能做成离线任务。"""
    report = PurgeReport()
    cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    report.by_age = (
        session.execute(
            delete(Explanation).where(Explanation.created_at < cutoff)
        ).rowcount
        or 0
    )

    total = int(session.execute(select(func.count()).select_from(Explanation)).scalar_one())
    overflow = total - max_rows
    if overflow > 0:
        # 留最新的那些：按 created_at 倒序取要保留的 id，其余删掉
        keep = session.execute(
            select(Explanation.id)
            .order_by(Explanation.created_at.desc(), Explanation.id.desc())
            .limit(max_rows)
        ).scalars().all()
        report.by_capacity = (
            session.execute(
                delete(Explanation).where(Explanation.id.not_in(list(keep)))
            ).rowcount
            or 0
        )
    session.flush()
    return report
