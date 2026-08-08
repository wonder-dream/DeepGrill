import threading
from datetime import datetime

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from app.config import (
    AppConfig,
    DailyConfig,
    LLMConfig,
    NotificationConfig,
    NowcoderConfig,
    SourcesConfig,
)
from app.errors import ConfigError
from app.scheduler import parse_cron, shutdown, start_scheduler, trigger_now


@pytest.fixture(autouse=True)
def clean_scheduler():
    yield
    shutdown()


def make_config(schedule="08:00"):
    return AppConfig(
        llm=LLMConfig(
            base_url="http://x", api_key_env="LLM_API_KEY",
            generate_model="g", judge_model="j",
        ),
        nowcoder=NowcoderConfig(cookie_env="NOWCODER_COOKIE", request_interval=0, retries=1),
        daily=DailyConfig(
            max_new_questions=36, knowledge_limit=3, design_limit=2,
            project_limit=1, chain_max_rounds=20, schedule=schedule,
        ),
        notification=NotificationConfig(enabled=False),
        sources=SourcesConfig(github_repos=[]),
    )


# --- parse_cron ---


def test_parse_cron_valid_expressions():
    def next_fire(expr, after):
        return parse_cron(expr).get_next_fire_time(None, after)

    fire = next_fire("08:00", datetime(2026, 8, 7, 0, 0))
    assert (fire.hour, fire.minute, fire.day) == (8, 0, 7)
    assert fire.tzinfo is not None  # 本地时区

    fire = next_fire("00:00", datetime(2026, 8, 7, 12, 0))
    assert (fire.hour, fire.minute, fire.day) == (0, 0, 8)  # 次日零点

    fire = next_fire("23:59", datetime(2026, 8, 7, 0, 0))
    assert (fire.hour, fire.minute) == (23, 59)


@pytest.mark.parametrize("expr", ["25:00", "8:60", "abc", "8", "", "08-00"])
def test_parse_cron_invalid_raises(expr):
    with pytest.raises(ConfigError):
        parse_cron(expr)


# --- 手动触发 ---


def test_trigger_now_runs_job():
    calls = []
    start_scheduler(make_config(), lambda: calls.append(1))
    trigger_now()
    assert calls == [1]
    trigger_now()
    assert calls == [1, 1]


def test_trigger_now_after_shutdown_is_noop():
    calls = []
    start_scheduler(make_config(), lambda: calls.append(1))
    shutdown()
    trigger_now()
    assert calls == []


# --- 定时触发 ---


def test_cron_trigger_fires_job():
    calls = []
    start_scheduler(
        make_config(), lambda: calls.append(1),
        trigger=IntervalTrigger(seconds=0.05),
    )
    import time

    time.sleep(0.35)
    assert len(calls) >= 1


def test_start_idempotent_single_registration():
    start_scheduler(make_config(), lambda: None)
    start_scheduler(make_config(), lambda: None)
    from app.scheduler import _scheduler

    assert len(_scheduler.get_jobs()) == 1


# --- fail ---


def test_overlap_rejected():
    from app.scheduler import _lock

    calls = []
    start_scheduler(make_config(), lambda: calls.append(1))
    assert _lock.acquire(blocking=False)  # 模拟任务运行中
    trigger_now()  # 被拒，立即返回
    assert calls == []
    _lock.release()


def test_job_exception_does_not_kill_scheduler():
    calls = []

    def job():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    start_scheduler(make_config(), job)
    trigger_now()  # 异常被 _run_locked 捕获
    trigger_now()  # 调度器仍存活
    assert len(calls) == 2
