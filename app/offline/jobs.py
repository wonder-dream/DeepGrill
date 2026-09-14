"""离线任务的持久化队列 + 独立 worker（ADR-0006）。

## 为什么是库表 + worker 而不是 Redis

ADR-0006 的理由里，**唯一不可替代的是"投递与业务写入同一事务"**：

```
数据库任务表：  with db.transaction(): add(interview); add(Job(...))   # 要么都成
Redis 队列：    db.commit(); redis.enqueue(...)                        # 中间崩了就不一致
```

这正是 v1 那批"卡在中间态"的 bug（业务写成功但任务永不执行）的根治办法。

## 三个必须实现的机制（ADR-0006 点名）

| 机制 | 本文件里在哪 | 为什么必须有 |
|---|---|---|
| **原子认领** | `claim_one()` 的一条 `UPDATE ... WHERE status='pending'` | 不能"先查再改"，否则两个 worker 会抢到同一条 |
| **心跳 + 超时回退** | `heartbeat()` / `requeue_stale()` | worker 进程被 kill 后任务不能永远卡在 running |
| **任务幂等** | 由**任务函数**保证（`TASKS` 的约定） | 超时回退与手动重跑都会让同一任务跑两遍 |

## 回收者（AGENTS.md §3.2）

`jobs` 表里的完成记录按 TTL 清理（`purge_finished()`）—— 这是"任何新增状态都要有
回收者"在库表上的对应。**没有回收者的状态就是泄漏**，只是从内存搬到了磁盘。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.db.models import Job

logger = logging.getLogger(__name__)

#: 心跳间隔与超时阈值。ADR-0006 给的值：30s 心跳、5min 无心跳视为进程已死。
HEARTBEAT_INTERVAL = 30
STALE_AFTER = timedelta(minutes=5)

#: 已完成记录的保留期（回收者）。7 天足够排查"昨天那个任务跑了吗"。
FINISHED_TTL = timedelta(days=7)


def now_iso() -> str:
    """与库里所有时间列同一种格式（`datetime('now')` 那种 TEXT，UTC）。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class JobSpec:
    """一个任务类型：名字 + 执行函数 + 幂等性说明。

    `idempotent` 不是装饰：**非幂等的任务不该进队列**（ADR-0006 的表里写明了
    "发通知 / 扣额度"属于不安全那一类）。这里用字段显式标出来，让 review 时
    一眼能看出哪个任务有风险。
    """

    name: str
    run: Callable[[Session, dict[str, Any]], dict[str, Any] | None]
    idempotent: bool = True
    description: str = ""


#: 任务注册表。加任务在这里登记 —— worker 与命令行共用它（ADR-0006：同一份领域
#: 函数支持三种调用方式）。
TASKS: dict[str, JobSpec] = {}


def register(spec: JobSpec) -> JobSpec:
    TASKS[spec.name] = spec
    return spec


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------
def enqueue(session: Session, *, kind: str, payload: dict[str, Any] | None = None, max_attempts: int = 3) -> Job:
    """投递一个任务。

    ⚠️ **它不 commit** —— 这是刻意的：调用方要在**同一个事务**里写业务数据与这个
    Job（ADR-0006 的全部理由）。谁 commit 谁负责。
    """
    if kind not in TASKS:
        # 投一个没人认领的任务 = 一条永远不会被执行的记录。宁可当场报错。
        raise KeyError(f"没有注册的任务类型：{kind}（可用：{sorted(TASKS)}）")
    job = Job(kind=kind, payload=payload or {}, status="pending", max_attempts=max_attempts)
    session.add(job)
    session.flush()
    return job


# ---------------------------------------------------------------------------
# 认领（机制①：原子）
# ---------------------------------------------------------------------------
def claim_one(session: Session, *, worker_id: str) -> Job | None:
    """原子地抢一条 pending 任务。

    实现方式：先查出候选 id，再用**带条件的** `UPDATE` 去抢 —— 抢不到（`rowcount=0`）
    说明别人先抢走了，于是返回 None。

    ⚠️ 不能写成"查出来 → 改它的 status → commit"：两个 worker 同时查到同一条时，
    两个都会以为抢到了。这是 ADR-0006 点名的第一条机制。
    """
    candidate = session.execute(
        select(Job.id).where(Job.status == "pending").order_by(Job.id).limit(1)
    ).scalar_one_or_none()
    if candidate is None:
        return None

    result = session.execute(
        update(Job)
        .where(Job.id == candidate, Job.status == "pending")   # ← 条件里再确认一次
        .values(
            status="running",
            worker_id=worker_id,
            started_at=now_iso(),
            heartbeat_at=now_iso(),
            attempts=Job.attempts + 1,
        )
    )
    if result.rowcount != 1:
        # 被别人抢走了。**不是错误** —— 回头再抢下一条即可。
        session.rollback()
        return None
    session.flush()
    return session.get(Job, candidate)


def heartbeat(session: Session, job_id: int) -> None:
    """worker 每 30 秒叫一次（机制②）。"""
    session.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=now_iso()))
    session.flush()


def requeue_stale(session: Session, *, now: str | None = None) -> int:
    """把心跳超时的 running 任务放回 pending（机制②）。

    v1 已有这个思路的雏形（给"判分中"的会话设 600 秒存活上限），只是当时只用在
    判分上、没推广到后台任务。**没有它，worker 被 kill 一次就永久卡住一条任务**。

    超过 `max_attempts` 的转 `failed` 并写清原因 —— 不静默丢弃。
    """
    reference = now or now_iso()
    cutoff = (
        datetime.strptime(reference, "%Y-%m-%d %H:%M:%S") - STALE_AFTER
    ).strftime("%Y-%m-%d %H:%M:%S")

    stale = session.execute(
        select(Job).where(
            Job.status == "running",
            func.coalesce(Job.heartbeat_at, Job.started_at) < cutoff,
        )
    ).scalars().all()

    requeued = 0
    for job in stale:
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            job.error = f"心跳超时 {int(STALE_AFTER.total_seconds())} 秒且已达重试上限（{job.max_attempts}）"
            job.finished_at = reference
            continue
        job.status = "pending"
        job.worker_id = None
        job.error = "心跳超时，已放回队列重跑"
        requeued += 1
    session.flush()
    return requeued


def finish(session: Session, job: Job, *, result: dict[str, Any] | None = None, error: str = "") -> None:
    """标记任务结束。**成功与失败都要落库**（失败不静默）。"""
    job.status = "failed" if error else "done"
    job.error = error or None
    job.message = (result or {}).get("message") if isinstance(result, dict) else None
    job.progress = 1.0 if not error else job.progress
    job.finished_at = now_iso()
    session.flush()


def purge_finished(session: Session, *, older_than: timedelta = FINISHED_TTL) -> int:
    """回收者：删掉保留期之外的终态记录（AGENTS.md §3.2）。"""
    cutoff = (datetime.now(UTC) - older_than).strftime("%Y-%m-%d %H:%M:%S")
    result = session.execute(
        delete(Job).where(Job.status.in_(("done", "failed")), Job.finished_at < cutoff)
    )
    session.flush()
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def run_one(session: Session, *, worker_id: str) -> bool:
    """抢一条并跑掉它。返回"有没有干活"。**不吞异常**：失败写进 job 再返回。"""
    job = claim_one(session, worker_id=worker_id)
    if job is None:
        return True  # 抢到了但被别人先拿走 —— 让调用方继续尝试
    spec = TASKS.get(job.kind)
    if spec is None:
        finish(session, job, error=f"没有注册的任务类型：{job.kind}")
        return True

    try:
        result = spec.run(session, job.payload or {})
    except Exception as e:  # noqa: BLE001
        # 任务函数抛异常是**预期内**的可能（外部服务挂了等）。失败必须落库：
        # 只有把 error 写进 job，"这条任务为什么没跑成"才是可查的（§3.1）。
        logger.exception("任务 %s 执行失败", job.id)
        finish(session, job, error=f"{type(e).__name__}: {e}")
        return True

    finish(session, job, result=result or {})
    return True


def run_forever(*, session_factory, worker_id: str, poll_seconds: float = 2.0, max_jobs: int | None = None) -> int:
    """worker 主循环。`max_jobs` 给测试用（跑够就退出，而不是真的一直跑）。"""
    handled = 0
    while True:
        with session_factory() as session:
            requeue_stale(session)
            did_work = run_one(session, worker_id=worker_id)
            session.commit()
        if did_work:
            handled += 1
            if max_jobs is not None and handled >= max_jobs:
                return handled
            continue
        if max_jobs is not None:
            return handled
        time.sleep(poll_seconds)
