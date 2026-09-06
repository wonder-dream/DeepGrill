"""M13 调度：APScheduler 每日定时触发 M12 流水线；启动/关闭/手动触发。

- 单次运行异常不外泄（M12 已消化 + 本层兜底），调度器不因任务失败停止
- 运行时互斥锁（非阻塞 try-acquire）：重叠触发被拒绝，不阻塞调用方
- 手动触发与定时触发走同一路径（_run_locked），复用互斥锁
"""
import logging
import threading
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig
from .errors import ConfigError

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_job: Callable[[], None] | None = None
_lock = threading.Lock()


def start_scheduler(config: AppConfig, job: Callable[[], None], *, trigger=None) -> None:
    """注册每日任务并启动；重复调用幂等（不重复注册）。trigger 供测试注入。"""
    global _scheduler, _job
    if _scheduler is not None:
        return
    _job = job
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _run_locked,
        trigger or parse_cron(config.daily.schedule),
        id="daily",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("daily scheduler started at %s", config.daily.schedule)


def shutdown() -> None:
    """停止调度器并复位（可重新 start）。"""
    global _scheduler, _job
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _job = None


def trigger_now() -> None:
    """手动触发一次调度任务（测试/调试入口）；任务运行中则直接返回。

    注意：Web「立即更新」实际走 BackgroundTasks 直跑 daily_runner，不经过本函数。
    """
    _run_locked()


def parse_cron(expr: str) -> CronTrigger:
    """解析 "HH:MM" 表达式；非法抛 ConfigError（启动阶段 fail fast）。"""
    parts = expr.strip().split(":")
    if len(parts) != 2:
        raise ConfigError(f"invalid schedule expression: {expr!r}, expected HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as e:
        raise ConfigError(f"invalid schedule expression: {expr!r}") from e
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"invalid schedule expression: {expr!r}")
    return CronTrigger(hour=hour, minute=minute)


def _run_locked() -> None:
    if _job is None:
        return
    if not _lock.acquire(blocking=False):
        logger.info("daily job already running; overlap rejected")
        return
    try:
        _job()
    except Exception:
        logger.exception("daily job failed")
    finally:
        _lock.release()
