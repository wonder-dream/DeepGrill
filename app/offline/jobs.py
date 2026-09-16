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
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.db.models import Job, TaskLog

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
    """worker 每 30 秒叫一次（机制②）。

    ⚠️ **调用者是 `run_one` 里的心跳线程**（见 `_heartbeat_while_running`）。
    它以前全库零调用者 —— 于是任何跑过 `STALE_AFTER` 的**健康**任务都会被
    `requeue_stale()` 放回队列、被第二个 worker 再跑一遍（同一份钱花两次，
    实测 probe22）。
    """
    session.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=now_iso()))
    session.flush()


@contextmanager
def _heartbeat_while_running(session: Session, job_id: int) -> Iterator[None]:
    """任务执行期间替它刷心跳。

    为什么要一个线程：`run_one` 是同步的，一个任务可能跑十几分钟（批量出题要调
    几十次模型），而"这个 worker 还活着吗"只能靠 `heartbeat_at` 说话。

    ⚠️ 它用**自己的会话**（`Session(bind=engine)`），不是任务那个：SQLAlchemy 的
    Session 不是线程安全的。这也意味着任务若长时间持有写锁，心跳会等锁
    （`busy_timeout` 之后放弃并记一条 warning）—— 那正是"事务不该跨在模型调用上"
    的另一面；本函数保证的是"心跳真的有发送者"，不是"一定有地方写"。

    ⚠️ 拿不到引擎（会话直接绑在一条 Connection 上）时不启动线程：宁可没有心跳，
    也不要在别人的连接上并发写。
    """
    engine = session.get_bind()
    if engine is None or not hasattr(engine, "connect"):
        logger.warning("任务 %s 拿不到引擎，这一轮不刷心跳", job_id)
        yield
        return

    stop = threading.Event()

    def beat() -> None:
        # 每轮重新读模块常量：测试把它调小到 0.01 秒，读死值就测不了
        while not stop.wait(HEARTBEAT_INTERVAL):
            try:
                with Session(bind=engine) as hb:
                    heartbeat(hb, job_id)
                    hb.commit()
            except Exception:  # noqa: BLE001
                # 心跳失败不该让任务失败：它只是"这一刻没写上"。记 warning，
                # 下一轮再试 —— 而任务本身的成败由它自己的结果决定。
                logger.warning("任务 %s 的心跳没刷上", job_id, exc_info=True)

    thread = threading.Thread(target=beat, name=f"heartbeat-{job_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5.0)


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
            # 走 finish 而不是就地改字段：**终态任务都要留下一行报告**
            # （否则"这条为什么没跑成"只有 jobs 里那一条，而它 7 天后就被回收了）。
            finish(
                session,
                job,
                error=f"心跳超时 {int(STALE_AFTER.total_seconds())} 秒且已达重试上限（{job.max_attempts}）",
                now=reference,
            )
            continue
        job.status = "pending"
        job.worker_id = None
        job.error = "心跳超时，已放回队列重跑"
        requeued += 1
    session.flush()
    return requeued


def _write_task_log(
    session: Session, job: Job, *, result: dict[str, Any] | None, error: str
) -> None:
    """把终态任务抄一份进 `task_logs`（"离线任务报告"）。

    **为什么两份都要有**：`jobs` 是队列，它的终态记录 7 天后被回收者删掉（§3.2 的
    TTL）；`task_logs` 是**给人看的报告**，它不回收 —— 否则"上个月那次批量生成
    到底跑出什么"会随着回收一起消失。

    失败也写（`status: failed` + `error`）—— AGENTS.md §3.1 要的"可查询的失败记录"
    落在这里。
    """
    body: dict[str, Any] = {"job_id": job.id, "status": "failed" if error else "done"}
    if error:
        body["error"] = error
    if isinstance(result, dict):
        body.update(result)
    session.add(TaskLog(kind=job.kind, payload=job.payload or {}, result=body))
    session.flush()


def finish(
    session: Session,
    job: Job,
    *,
    result: dict[str, Any] | None = None,
    error: str = "",
    now: str | None = None,
) -> None:
    """标记任务结束。**成功与失败都要落库**（失败不静默）。"""
    job.status = "failed" if error else "done"
    job.error = error or None
    job.message = (result or {}).get("message") if isinstance(result, dict) else None
    job.progress = 1.0 if not error else job.progress
    job.finished_at = now or now_iso()
    session.flush()
    _write_task_log(session, job, result=result, error=error)


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
    """抢一条并跑掉它。返回**有没有可干的活**。**不吞异常**：失败写进 job 再返回。

    返回值的语义是这一条的关键（实测撞过两次）：

    · **空队列 → False**。原来空队列也返回 True（"抢到了但被别人先拿走"与
      "队列里根本没有"共用一个 None），于是 `worker --once` 的
      `while True: if not run_one(...): break` **永远不 break**（三次实测被杀于
      12/15/25 秒），常驻 worker 也变成空转不 sleep。
    · 抢输了（有候选但没抢到）→ True：该立刻再抢下一条，而不是去睡。

    ⚠️ **认领之后立刻 commit**：认领是一次写（`BEGIN IMMEDIATE` 起事务），不提交的话
    这条连接会把**写锁**一直攥到任务跑完 —— 心跳线程（另一个连接）一个字节都写不进去，
    机制②就只剩个装饰。提交的代价只是"任务失败时 attempts 也已经加了 1"，而这恰恰
    更接近事实（那次尝试真的发生过）。
    """
    job = claim_one(session, worker_id=worker_id)
    if job is None:
        # 队列空 vs 抢输了：两者都是 None，含义不同。多查一次是有意的 ——
        # `--once` 的正确性与常驻 worker 的 sleep 都依赖这个区分。
        return (
            session.execute(select(Job.id).where(Job.status == "pending").limit(1)).first()
            is not None
        )
    session.commit()  # 放掉写锁（见 docstring）—— 心跳线程要靠它

    # ⚠️ 先把要用的字段读出来：任务失败之后这个对象可能**读都读不了**
    # （失败的 flush 会让会话进入"需要回滚"状态，连 `job.id` 都会抛
    # `PendingRollbackError` —— 实测就是这么炸的，第一版连日志那行都没走完）。
    job_id = job.id
    payload = job.payload or {}
    spec = TASKS.get(job.kind)
    if spec is None:
        finish(session, job, error=f"没有注册的任务类型：{job.kind}")
        return True

    try:
        with _heartbeat_while_running(session, job_id):
            result = spec.run(session, payload)
    except Exception as e:  # noqa: BLE001
        # 任务函数抛异常是**预期内**的可能（外部服务挂了等）。失败必须落库：
        # 只有把 error 写进 job，"这条任务为什么没跑成"才是可查的（§3.1）。
        #
        # ⚠️ **先 rollback 再碰任何 ORM 对象**：任务**在写库时**失败会把会话留在
        # "需要回滚"的状态，`finish()` 的 flush（甚至读一个属性）都会抛
        # `PendingRollbackError` 穿过这个 except 逃出去 —— worker 进程当场死掉，
        # job 卡在 running，连一行失败记录都没写（实测 probe22）。回滚顺带把这次
        # 任务写了一半的东西丢掉，那正是我们想要的。
        session.rollback()
        logger.exception("任务 %s 执行失败", job_id)
        job = session.get(Job, job_id)
        if job is None:  # 理论上到不了：认领的那一行是已提交的事实
            logger.error("任务 %s 的行在回滚后不见了，失败记录没写", job_id)
            return True
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
