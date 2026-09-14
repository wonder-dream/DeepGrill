"""把一次请求里花的 token 记进额度账本（决策 14）。

**为什么它单独成一个模块**：它要被不止一个页面用（面试页、题库的讲解生成），而
"抄一份记账代码到第二个页面"正是最容易漏记的那种做法 —— v1 的实测教训是一次请求
里连调三次模型，而记账只记了最后一次（"最近一次"只看得见最后一次）。

所以这里只有两个函数：`snapshot(llm)` 在请求开始时取一次、`record(...)` 在请求结束
时记差值。**两个调用点都必须成对出现**，中间那一段无论调了几次模型都会被算进去。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.account import service as account


def snapshot(llm) -> dict[str, int]:
    """请求开始时客户端的累计用量。"""
    return dict(getattr(llm, "usage_total", {}) or {})


def record(session: Session, user_id: int, llm, before: dict[str, int]) -> int:
    """把这个请求里**全部** LLM 调用消耗的 token 记进账本，返回记了多少。

    取差值而不是读"最近一次"：一次请求可能调用多次（判定 + 判分 + 总结），
    而"最近一次"只会记到最后一次 —— 实测就是这么漏掉两次的。

    没有 `usage_total` 的客户端（测试替身）返回 0，**不报错**：替身本来就没有真实
    用量，而记账不该因为替身而炸。
    """
    after = getattr(llm, "usage_total", None)
    if not isinstance(after, dict):
        return 0
    total = sum(
        after.get(k, 0) - before.get(k, 0) for k in ("prompt_tokens", "completion_tokens")
    )
    if total < 0:  # 客户端被换过（不该发生）——不记负账
        return 0
    account.record_tokens(session, user_id, total)
    return total
