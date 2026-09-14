"""事后治理：质量信号的**检测**与**处置**（决策 5 + ADR-0002）。

决策 5 把内容治理定成"**自动门禁进池 + 事后治理**"，而 ADR-0002 有一句话：

> 矛盾记录是这套结构唯一白送的能力

## 检测（自动、可复现）

`flag_duplicate_questions()` 扫公共题：**同一个知识点下**如果两道题的题干归一化之后
完全相同，那就是一道题被收了两遍。它只标记、**不改题库** —— 自动合并/删除会让
"这道题为什么不见了"变成一件没人能解释的事。

⚠️ 这里故意**不用嵌入**：这版要抓的是"题干几乎一样"这种一眼可判的重复，
一个确定性的字符串比较就够了，而且它**可复现、可解释**（"与 #123 相似"）。
真正的语义级去重属于知识层构建管道（决策 44 的分批 + 嵌入聚类），不要在这里做半套。

## 处置（人来定）

· `resolve(flag, 'resolved')` —— 看过了，确实有问题（可能已经改过题）
· `dismiss(flag, 'dismissed')` —— 看过了，不是问题（误报）
· `hide_question(flag)` —— **先把题藏起来**（`visibility='hidden'`）并把这道题上
  所有未处理的标记一起结掉。这是治理里唯一"改题库"的动作，因为它是最保守的：
  藏起来只会让它不出现在浏览与抽题里，不会删数据。

## 谁在跑检测

注册成离线任务 `flag_duplicate_questions`（`jobs.TASKS`），由 worker 周期执行；
幂等（先清掉自己上一次的未处理记录再重建），所以重跑安全（ADR-0006 的机制③）。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.bank import repository
from app.bank.promotion import normalize_stem
from app.db.models import Question, QuestionFlag
from app.errors import InvalidInput, NotFound

#: 这一类标记由**本模块**负责重建（幂等的前提：一次扫描只清自己那个 kind 的未处理记录）。
#: 用的是 `conflict`（schema 的 CHECK 里已有的值：与同知识点其他题口径不一致）——
#: 同一知识点下出现两道一模一样的题，正是"口径不一致"最直白的一种。
FLAG_KIND = "conflict"

RESOLVED = "resolved"
DISMISSED = "dismissed"


def flag_duplicate_questions(session: Session, payload: dict | None = None) -> dict:
    """扫公共题，把"同知识点下题干相同"的**后一道**标出来。**幂等。**

    为什么留后一道（id 大的那个）而不是前一道：先入库的那道多半已经被引用（收藏、
    答题记录、面试计划），标新的那个代价最小。

    返回的 dict 直接进 `job.result`（`task_logs` 报告），所以带一句人话 `message`。
    """
    del payload
    deleted = (
        session.execute(
            delete(QuestionFlag).where(
                QuestionFlag.kind == FLAG_KIND, QuestionFlag.status == "open"
            )
        ).rowcount
        or 0
    )

    rows = repository.public_visible_questions(session)

    seen: dict[tuple[int | None, str], Question] = {}
    pairs: list[tuple[int, int]] = []
    for question in rows:
        key = (question.primary_point_id, normalize_stem(question.stem))
        if not key[1]:
            continue
        first = seen.get(key)
        if first is None:
            seen[key] = question
            continue
        session.add(
            QuestionFlag(
                question_id=question.id,
                kind=FLAG_KIND,
                detail=(
                    f"与 #{first.id} 的题干完全相同（归一化后）"
                    f"—— 同一个知识点下不该有两道一样的题"
                ),
                status="open",
            )
        )
        pairs.append((first.id, question.id))

    session.flush()
    return {
        "message": f"标记 {len(pairs)} 道重复题（清掉旧记录 {deleted} 条）",
        "flagged": len(pairs),
        "deleted_previous": deleted,
        "pairs": pairs,
    }


def open_flags(session: Session) -> list[QuestionFlag]:
    """还没处理的标记（治理页用）。最老的排前面 —— 先处理积压最久的。"""
    return list(
        session.execute(
            select(QuestionFlag)
            .where(QuestionFlag.status == "open")
            .order_by(QuestionFlag.id)
        )
        .scalars()
        .all()
    )


def _get(session: Session, flag_id: int) -> QuestionFlag:
    flag = session.get(QuestionFlag, flag_id)
    if flag is None:
        raise NotFound("这条质量标记不存在")
    return flag


def close(session: Session, *, flag_id: int, status: str = RESOLVED) -> QuestionFlag:
    """结掉一条标记。`status` 只能是 `resolved` / `dismissed`。

    **不接受 `open`**：那是"重新打开"，而它该由检测重新产出（跑一遍扫描），
    不是由人手工把一条结掉的记录改回去。后者会让"什么时候重新变脏的"无从追溯。
    """
    if status not in (RESOLVED, DISMISSED):
        raise InvalidInput(f"status 只能是 {RESOLVED} / {DISMISSED}（收到 {status}）")
    flag = _get(session, flag_id)
    flag.status = status
    session.flush()
    return flag


def hide_question(session: Session, *, flag_id: int) -> tuple[QuestionFlag, int]:
    """把这条标记指向的题**藏起来**，并把这道题上所有未处理的标记一起结掉。

    返回 `(触发的那条标记, 一起结掉了几条)`。

    藏 = `visibility='hidden'`：它不出现在任何浏览/抽题里，但数据还在（作者仍能看到
    自己的私有题；公共题则只对 owner 可见）。**不删题** —— 质量信号可能有误，
    而删除没有回头路。
    """
    flag = _get(session, flag_id)
    question = session.get(Question, flag.question_id)
    if question is None:
        raise NotFound("这条标记指向的题不见了")

    question.visibility = "hidden"
    siblings = session.execute(
        select(QuestionFlag).where(
            QuestionFlag.question_id == question.id, QuestionFlag.status == "open"
        )
    ).scalars().all()
    for sibling in siblings:
        sibling.status = RESOLVED
    session.flush()
    return flag, len(siblings)
