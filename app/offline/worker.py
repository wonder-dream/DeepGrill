"""worker 进程入口：`python -m app.offline.worker`（ADR-0006 / ADR-0010）。

与 web **共用同一个镜像**，只是启动命令不同 —— 于是不存在"两边代码不一致"这类
问题：worker 调的是与路由同一批领域函数。

它做三件事，循环往复：
  ① `requeue_stale()` —— 把心跳超时的任务放回队列（机制②）
  ② `run_one()`       —— 原子抢一条并执行（机制①）
  ③ `purge_finished()`—— 回收终态记录（AGENTS.md §3.2 的回收者）

**任务函数的幂等由函数自己保证**（机制③）—— 这里只负责"可能跑两遍"这件事
不会让谁意外。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.offline import jobs

# 导入即注册任务（worker 与命令行共用同一批函数 —— ADR-0006）。
# 放在这里而不是 `jobs.py`：`jobs.py` 是队列机制本身，任务属于"用队列的人"。
from app.offline import tasks as _tasks  # noqa: F401  (side effect: register)

logger = logging.getLogger("deepgrill.worker")

#: 回收者的检查间隔（跑得比心跳慢得多 —— 它删的是几天前的记录）。
PURGE_EVERY = 100


def worker_id() -> str:
    """每台机器每个进程一个稳定标识：`<主机名>-<pid>`。

    为什么不用随机串：排查"这条任务被哪个 worker 抢走了"时，能靠它找到机器与进程。
    """
    return f"{os.uname().nodename if hasattr(os, 'uname') else 'win'}-{os.getpid()}"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s")
    settings = Settings()
    engine = create_db_engine(settings.resolved_database_path())
    factory = create_session_factory(engine)
    me = worker_id()

    stop = False

    def _handle(signum: int, _frame: object) -> None:
        nonlocal stop
        logger.info("收到信号 %s，跑完当前任务就退出", signum)
        stop = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            # 非主线程 / 平台不支持 —— 不是致命问题，但要说出来
            logger.warning("装不上信号处理器 %s（这个平台可能不支持）", sig)

    logger.info("worker %s 启动；已注册任务：%s", me, sorted(jobs.TASKS))
    rounds = 0
    while not stop:
        with factory() as session:
            requeued = jobs.requeue_stale(session)
            if requeued:
                logger.info("放回 %d 条心跳超时的任务", requeued)
            did_work = jobs.run_one(session, worker_id=me)
            rounds += 1
            if rounds % PURGE_EVERY == 0:
                purged = jobs.purge_finished(session)
                if purged:
                    logger.info("回收了 %d 条终态任务记录", purged)
            session.commit()
        if not did_work:
            time.sleep(2.0)
    logger.info("worker %s 退出", me)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
