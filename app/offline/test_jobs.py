"""离线队列与 worker 的测试（ADR-0006 的三个机制 + 回收者）。

这四条每一条都对应一次真实事故或 ADR 点名的机制，所以断言点都选在**库里的状态**
上，而不是返回值：

· **原子认领** —— 两个 worker 不能抢到同一条（v1 是"先查再改"）
· **心跳超时回退** —— worker 被 kill 后任务不能永远卡在 running
· **幂等** —— 重跑是常态，所以任务函数要写成"跑两遍结果一样"
· **回收者** —— 没有回收者的状态就是泄漏（AGENTS.md §3.2），只是搬到了磁盘
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import Job, TaskLog
from app.offline import jobs
from migrations._runner import migrate


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "jobs.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        yield s


@pytest.fixture(autouse=True)
def clean_registry():
    """每个测试用干净的注册表 —— 否则测试之间会互相看见对方注册的任务。"""
    saved = dict(jobs.TASKS)
    jobs.TASKS.clear()
    yield
    jobs.TASKS.clear()
    jobs.TASKS.update(saved)


def _register(name: str = "demo", fn=None, *, idempotent: bool = True):
    calls: list[dict] = []

    def default_run(session, payload):
        calls.append(payload)
        return {"message": f"跑了 {payload}"}

    return jobs.register(
        jobs.JobSpec(name=name, run=fn or default_run, idempotent=idempotent)
    ), calls


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------
def test_enqueue_refuses_unregistered_kind(session: Session) -> None:
    """投一个没人认领的任务 = 一条永远不执行的记录。宁可当场报错。"""
    with pytest.raises(KeyError):
        jobs.enqueue(session, kind="没注册过")


def test_enqueue_does_not_commit(session: Session, tmp_dir: Path) -> None:
    """**ADR-0006 的全部理由**：投递要与业务写入在同一事务里 —— 所以它不能自己 commit。

    ⚠️ 断言必须查**另一个连接**：`session.get()` 读的是当前会话的 identity map，
    回滚之后那份内存对象还在，于是"看着像没回滚"。实测踩过 —— 第一版就是这么
    写错的（它会永远通过，因为对象还在内存里）。
    """
    import sqlite3

    _register()
    jobs.enqueue(session, kind="demo", payload={"a": 1})
    session.rollback()

    path = session.get_bind().url.database
    conn = sqlite3.connect(str(path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "投递自己 commit 了，事务性就没了"


# ---------------------------------------------------------------------------
# 机制① 原子认领
# ---------------------------------------------------------------------------
def test_claim_is_atomic(session: Session) -> None:
    """两个 worker 抢同一条：只有一个拿到。"""
    _register()
    jobs.enqueue(session, kind="demo")
    session.commit()

    first = jobs.claim_one(session, worker_id="w1")
    assert first is not None
    # w2 再来抢 —— 必须抢不到（第一条已经是 running 了，队列里没有别的）
    second = jobs.claim_one(session, worker_id="w2")
    assert second is None
    assert first.worker_id == "w1"


def test_claim_returns_none_when_queue_is_empty(session: Session) -> None:
    assert jobs.claim_one(session, worker_id="w") is None


def test_claim_increments_attempts(session: Session) -> None:
    """每次认领都要计数 —— 否则 `max_attempts` 永远不会生效。"""
    _register()
    jobs.enqueue(session, kind="demo")
    session.commit()
    job = jobs.claim_one(session, worker_id="w")
    assert job is not None
    assert job.attempts == 1


# ---------------------------------------------------------------------------
# 机制② 心跳与超时回退
# ---------------------------------------------------------------------------
def test_stale_running_job_is_requeued(session: Session) -> None:
    """**worker 被 kill 后任务不能永远卡在 running** —— v1 的 `_process_ugc_submission`
    就是这么永久卡在 processing 的。"""
    _register()
    jobs.enqueue(session, kind="demo")
    session.commit()
    job = jobs.claim_one(session, worker_id="w")
    assert job is not None

    old = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    job.heartbeat_at = old
    session.commit()

    assert jobs.requeue_stale(session) == 1
    session.commit()
    assert session.get(Job, job.id).status == "pending"
    assert session.get(Job, job.id).worker_id is None


def test_fresh_heartbeat_is_not_requeued(session: Session) -> None:
    """心跳还新就不该被动 —— 否则正常的慢任务会被反复重跑。"""
    _register()
    jobs.enqueue(session, kind="demo")
    session.commit()
    jobs.claim_one(session, worker_id="w")
    assert jobs.requeue_stale(session) == 0


def test_stale_job_over_max_attempts_becomes_failed(session: Session) -> None:
    """超时且已重试到上限 → `failed` 并写清原因，**不静默丢弃**。"""
    _register()
    jobs.enqueue(session, kind="demo", max_attempts=1)
    session.commit()
    job = jobs.claim_one(session, worker_id="w")
    old = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    job.heartbeat_at = old
    session.commit()

    jobs.requeue_stale(session)
    session.commit()
    dead = session.get(Job, job.id)
    assert dead.status == "failed"
    assert "心跳超时" in dead.error
    assert dead.finished_at is not None


# ---------------------------------------------------------------------------
# 执行与幂等
# ---------------------------------------------------------------------------
def test_run_one_executes_and_finishes(session: Session) -> None:
    _, calls = _register()
    jobs.enqueue(session, kind="demo", payload={"x": 1})
    session.commit()

    assert jobs.run_one(session, worker_id="w") is True
    session.commit()
    assert calls == [{"x": 1}]
    job = session.execute(select(Job)).scalars().one()
    assert job.status == "done"
    assert job.finished_at is not None


def test_task_failure_is_recorded_not_swallowed(session: Session) -> None:
    """任务抛异常 → 写进 `job.error`。**失败不静默**（AGENTS.md §3.1）。"""

    def boom(session, payload):
        raise RuntimeError("外部服务挂了")

    _register(fn=boom)
    jobs.enqueue(session, kind="demo")
    session.commit()
    jobs.run_one(session, worker_id="w")
    session.commit()

    job = session.execute(select(Job)).scalars().one()
    assert job.status == "failed"
    assert "外部服务挂了" in job.error


def test_rerun_is_safe_when_the_task_is_idempotent(session: Session) -> None:
    """**机制③**：超时回退会重跑，所以任务函数要写成"跑两遍结果一样"。

    这里用一个幂等的任务示范：它把结果写进一个 dict（同 payload → 同结果），
    连跑两次之后状态与第一次相同 —— 这正是"重跑安全"的定义。
    """
    state: dict[str, int] = {}

    def idempotent(session, payload):
        state[payload["key"]] = payload["value"]  # 覆盖写，不是累加
        return {"message": "ok"}

    _register(fn=idempotent)
    jobs.enqueue(session, kind="demo", payload={"key": "k", "value": 1})
    jobs.enqueue(session, kind="demo", payload={"key": "k", "value": 1})
    session.commit()
    jobs.run_one(session, worker_id="w")
    session.commit()
    jobs.run_one(session, worker_id="w")
    session.commit()

    assert state == {"k": 1}, "跑两遍结果必须一样（覆盖写，不是累加）"
    assert len(session.execute(select(Job)).scalars().all()) == 2


def test_run_one_reports_no_work_for_an_empty_queue(session: Session) -> None:
    """空队列 → **False**（"有没有可干的活"要真的回答）。

    返回 True 的后果实测过三次：`python -m app.cli worker --once`（帮助文本写着
    "跑空队列就退出"）永远不退出，常驻 worker 在空队列上 `did_work=True → continue`、
    **不 sleep** → 空转。
    """
    assert jobs.claim_one(session, worker_id="w") is None
    assert jobs.run_one(session, worker_id="w") is False


def test_run_one_refreshes_its_heartbeat_while_the_task_runs(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务跑着的时候，心跳要真的在刷（机制②的另一半）。

    没有它，`requeue_stale()` 分不出"健康但要跑 6 分钟的任务"与"worker 已经死了"：
    前者被放回队列、被第二个 worker 再跑一遍（重复付费 + 双份 `task_logs`）。
    实测 `heartbeat()` 全库零调用者。间隔在这里调小到 0.01 秒，测试是瞬时的。
    """
    monkeypatch.setattr(jobs, "HEARTBEAT_INTERVAL", 0.01)
    beats: list[int] = []
    real = jobs.heartbeat

    def spy(s: Session, job_id: int) -> None:
        beats.append(job_id)
        real(s, job_id)

    monkeypatch.setattr(jobs, "heartbeat", spy)

    def slow_task(s: Session, payload: dict) -> dict:
        time.sleep(0.2)  # 比心跳间隔长得多 ⇒ 期间必然刷过几次
        return {"message": "done"}

    _register(fn=slow_task)
    jobs.enqueue(session, kind="demo")
    session.commit()

    jobs.run_one(session, worker_id="w")
    session.commit()

    assert beats, "任务运行期间必须刷新心跳（heartbeat() 以前没有任何调用者）"


def test_a_task_failing_on_a_db_write_is_recorded(session: Session) -> None:
    """任务**在写库时**失败：不能把异常抛出去，而且失败记录要写下来。

    实测：`finish()` 的 flush 撞上"会话需要回滚"的状态 → `PendingRollbackError`
    穿过 `run_one` 的 except 逃出去 → **worker 进程当场死掉**，job 卡在 running，
    §3.1 的失败记录一行都没写。
    """

    def boom(s, payload):
        # 写到一半失败：同一个主键插两次
        s.add(TaskLog(id=99, kind="probe_boom", payload={}, result={}))
        s.flush()
        s.add(TaskLog(id=99, kind="probe_boom", payload={}, result={}))
        s.flush()
        return {}

    _register(name="probe_boom", fn=boom)
    job = jobs.enqueue(session, kind="probe_boom")
    session.commit()

    jobs.run_one(session, worker_id="w")  # 不许抛
    session.commit()

    session.expire_all()
    row = session.get(Job, job.id)
    assert row is not None and row.status == "failed", "失败的任务要留下终态"
    logs = session.execute(select(TaskLog).where(TaskLog.kind == "probe_boom")).scalars().all()
    assert logs, "§3.1 要求失败也要有可查询的记录（task_logs）"


def test_cli_worker_once_exits_on_an_empty_queue(tmp_dir: Path) -> None:
    """`python -m app.cli worker --once`（帮助文本："跑空队列就退出"）**要真的退出**。

    实测三次被杀于 12s / 15s / 25s —— `while True: if not run_one(...): break`
    永远不 break，因为空队列上 `run_one()` 返回了 True。这里在原进程里跑它，
    超时就判失败（不用 sleep 猜时长，用事件等）。
    """
    import threading

    from app.cli import cmd_worker
    from app.config import Settings

    db = tmp_dir / "worker_once.db"
    migrate(db)
    done = threading.Event()

    def run() -> None:
        try:
            cmd_worker(Settings(database_path=db, llm_api_key=""), once=True)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert done.wait(timeout=30), "`worker --once` 在空队列上没有退出（它本该跑空就退出）"


def test_jobspec_declares_idempotence() -> None:
    """非幂等的任务必须**显式标出来**（ADR-0006 的表：发通知/扣额度属不安全那类）。

    这个字段的用途是让 review 时一眼看出哪个任务有风险 —— 所以它默认 True 但
    有真实含义，不是装饰。
    """
    spec = jobs.JobSpec(name="x", run=lambda s, p: None, idempotent=False)
    assert spec.idempotent is False


# ---------------------------------------------------------------------------
# 回收者
# ---------------------------------------------------------------------------
def test_purge_removes_only_old_finished_jobs(session: Session) -> None:
    """**回收者**（AGENTS.md §3.2）：终态记录按 TTL 清；pending/running 一条都不能动。"""
    _register()
    done = jobs.enqueue(session, kind="demo")
    pending = jobs.enqueue(session, kind="demo")
    running = jobs.enqueue(session, kind="demo")
    session.commit()
    job_done = session.get(Job, done.id)
    job_done.status = "done"
    job_running = session.get(Job, running.id)
    job_running.status = "running"
    old = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    job_done.finished_at = old
    job_running.heartbeat_at = "2999-01-01 00:00:00"
    session.commit()

    assert jobs.purge_finished(session) == 1
    session.commit()
    remaining = {j.id for j in session.execute(select(Job)).scalars()}
    assert done.id not in remaining
    assert pending.id in remaining and running.id in remaining


def test_purge_keeps_recent_finished_jobs(session: Session) -> None:
    """刚跑完的不能删 —— 排查"昨天那个任务跑了吗"要用到它。"""
    _register()
    job = jobs.enqueue(session, kind="demo")
    session.commit()
    jobs.run_one(session, worker_id="w")
    session.commit()

    assert jobs.purge_finished(session) == 0
    assert session.get(Job, job.id) is not None


# ---------------------------------------------------------------------------
# 离线任务报告（task_logs）
# ---------------------------------------------------------------------------
def test_successful_job_leaves_a_report(session: Session) -> None:
    """`jobs` 是队列（终态 7 天后被回收），`task_logs` 是**不回收的报告**。

    只有队列记录的话，"上个月那次批量生成跑出什么"会随着回收一起消失 ——
    这正是 ADR-0006 与观测页要分开存两份的原因。
    """
    _register()
    jobs.enqueue(session, kind="demo", payload={"a": 1})
    session.commit()
    jobs.run_one(session, worker_id="w")
    session.commit()

    log = session.execute(select(TaskLog)).scalars().one()
    assert log.kind == "demo"
    assert log.payload == {"a": 1}
    assert log.result["status"] == "done"
    assert log.result["message"] == "跑了 {'a': 1}"


def test_failed_job_leaves_a_report(session: Session) -> None:
    """§3.1 要的"可查询的失败记录"落在 task_logs 上 —— 失败也必须留一行。"""

    def boom(session, payload):
        raise RuntimeError("外部服务挂了")

    _register(fn=boom)
    jobs.enqueue(session, kind="demo")
    session.commit()
    jobs.run_one(session, worker_id="w")
    session.commit()

    log = session.execute(select(TaskLog)).scalars().one()
    assert log.result["status"] == "failed"
    assert "外部服务挂了" in log.result["error"]


def test_timeout_over_limit_also_leaves_a_report(session: Session) -> None:
    """`requeue_stale` 那条"重试到上限"的路径**也必须**留报告。

    第一版它是就地改字段、绕过 `finish()` 的 —— 于是任务死了却没有报告，
    而这条路径恰恰是最需要解释的那一种（"它为什么没跑成"）。
    """
    _register()
    jobs.enqueue(session, kind="demo", max_attempts=1)
    session.commit()
    job = jobs.claim_one(session, worker_id="w")
    job.heartbeat_at = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    session.commit()

    jobs.requeue_stale(session)
    session.commit()

    log = session.execute(select(TaskLog)).scalars().one()
    assert log.result["status"] == "failed"
    assert "心跳超时" in log.result["error"]


def test_requeued_job_leaves_no_report_yet(session: Session) -> None:
    """回退到 pending 的任务**还没结束**，不该留报告 —— 否则一次超时会留下
    "失败"的记录，而它下一轮就跑成了。"""
    _register()
    jobs.enqueue(session, kind="demo")
    session.commit()
    job = jobs.claim_one(session, worker_id="w")
    job.heartbeat_at = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    session.commit()

    assert jobs.requeue_stale(session) == 1
    assert session.execute(select(TaskLog)).first() is None
