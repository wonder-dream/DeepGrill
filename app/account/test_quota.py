"""额度账本的并发语义（决策 90）。

这个文件只钉一件事：**"够不够"与"扣掉"是一条语句**。

为什么值得单独一个文件：原来的实现分两步（先 `quota_state()` 读余额、再
`add_usage()` 累加），两步之间没有锁。实测 6 路并发 `POST /interview/start`
造出 6 场面试（36 点）而账本只记了 **12 点** —— 日上限被静默突破，另一路表现是
撞 `quota_ledger` 的主键变成 500。这类 bug 只有在真并发下才现形，所以这里的
测试用真线程 + 真连接（每个线程自己的引擎/会话），不用替身。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.account import repository, service
from app.db import create_db_engine, create_session_factory
from app.db.models import QuotaLedger, User
from app.errors import QuotaExhausted
from migrations._runner import migrate

ME = 7


@pytest.fixture
def db(tmp_dir: Path) -> Iterator[Path]:
    path = tmp_dir / "quota.db"
    migrate(path)
    engine = create_db_engine(path)
    with create_session_factory(engine)() as s:
        s.add(User(id=ME, email="me@local", username="me", password_hash="h", role="user"))
        s.commit()
    engine.dispose()
    yield path


def _day() -> str:
    return service.today()


def test_the_quota_day_follows_the_configured_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    """**回归测试**：额度按天重置的"天"按配置的时区算，不是按 UTC。

    以前写的是 `datetime.now(UTC)` —— 服务器在美东/UTC 时，中文用户看到的额度重置
    时刻是**北京时间早上 8 点**。默认改成 +8；边界要能用测试钉住（否则"改没改对"
    只能等到某天早上有人来问）。
    """
    from datetime import UTC
    from datetime import datetime as real_datetime

    class FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            # UTC 17:30 == 北京时间次日 01:30
            return real_datetime(2026, 9, 16, 17, 30, tzinfo=UTC)

    monkeypatch.setattr(service, "datetime", FixedDatetime)
    try:
        service.set_quota_utc_offset(8)
        assert service.today() == "2026-09-17", "北京时间已经跨天了"
        service.set_quota_utc_offset(0)
        assert service.today() == "2026-09-16", "UTC 还是 16 号"
        service.set_quota_utc_offset(-5)
        assert service.today() == "2026-09-16"
    finally:
        service.set_quota_utc_offset(8)   # 模块级状态，必须复位


def test_a_bogus_offset_is_refused_at_startup() -> None:
    """非法偏移要**当场报错**（启动期），而不是在某个请求里算出奇怪的日期。"""
    from app.errors import InvalidInput

    with pytest.raises(InvalidInput):
        service.set_quota_utc_offset(99)
    service.set_quota_utc_offset(8)


def test_spend_units_is_exact_under_concurrency(db: Path) -> None:
    """并发扣点：一笔都不能丢（丢的就是"日上限被突破"）。

    判据是**账本里的总数**，不是"函数没抛异常" —— 旧实现两边都不抛异常，
    只是把几笔累加悄悄吃掉了（实测 6 路 → 只记下 12 点）。

    路数取 `DAILY_UNITS // COST["interview"]`（= 5，从常量算，不写死）：再多一路
    就该被日上限挡下，那是下一条测试的事 —— 混在一起就分不清"丢更新"与"正常拒绝"。
    """
    engine = create_db_engine(db)
    cost = service.COST["interview"]
    workers = service.DAILY_UNITS // cost
    barrier = threading.Barrier(workers)

    def spend(_i: int) -> int:
        with create_session_factory(engine)() as s:
            barrier.wait(timeout=10)  # 一起冲，把窗口放到最大
            charged = service.spend_units(s, ME, "interview")
            s.commit()
            return charged

    with ThreadPoolExecutor(max_workers=workers) as ex:
        costs = list(ex.map(spend, range(workers)))

    assert costs == [cost] * workers, "并发下有请求没扣成"
    with create_session_factory(engine)() as s:
        assert service.units_used_today(s, ME) == workers * cost
    engine.dispose()


def test_the_daily_limit_holds_under_concurrency(db: Path) -> None:
    """余额只剩一场模拟面试时，6 路并发只能有**一场**开得起来（其余 `QuotaExhausted`）。

    这是"日上限"这条产品承诺的唯一可直接测的形式：并发下账本不许越过它。
    """
    engine = create_db_engine(db)
    cost = service.COST["interview"]
    with create_session_factory(engine)() as s:
        repository.add_usage(s, ME, day=_day(), units=service.DAILY_UNITS - cost)
        s.commit()

    workers = 6
    barrier = threading.Barrier(workers)

    def spend(_i: int) -> str:
        with create_session_factory(engine)() as s:
            barrier.wait(timeout=10)
            try:
                service.spend_units(s, ME, "interview")
                s.commit()
                return "ok"
            except QuotaExhausted:
                s.rollback()
                return "exhausted"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        outcomes = list(ex.map(spend, range(workers)))

    assert outcomes.count("ok") == 1, f"并发下开起来了 {outcomes.count('ok')} 场：{outcomes}"
    with create_session_factory(engine)() as s:
        used = service.units_used_today(s, ME)
    assert used == service.DAILY_UNITS, f"账本越过了日上限：{used}"
    engine.dispose()


def test_a_refused_spend_leaves_the_row_untouched(db: Path) -> None:
    """不够时**一点不动**（不是"先扣了再退"）—— 条件更新的 `WHERE` 就是这条。"""
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        repository.add_usage(s, ME, day=_day(), units=service.DAILY_UNITS)
        assert repository.try_spend_units(
            s, ME, day=_day(), units=1, daily_limit=service.DAILY_UNITS
        ) is False
        row = repository.quota_row(s, ME, _day())
        assert row is not None and row.units_used == service.DAILY_UNITS
        assert row.tokens_used == 0
    engine.dispose()


def test_reads_after_a_sql_side_increment_are_not_stale(db: Path) -> None:
    """扣点之后同一个会话里读到的必须是新值。

    ⚠️ 这条是**实测撞出来的**：累加发生在 SQL 里（`ON CONFLICT … SET x = x + n`），
    而 ORM 的身份映射不会因此刷新已加载的对象 —— "先读过额度 → 扣点 → 再读"
    会拿到旧值（读到 6 而库里是 12），页面于是显示"还剩 26 点"而实际只剩 20。
    """
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        service.spend_units(s, ME, "interview")
        first = service.units_used_today(s, ME)  # 进身份映射
        service.spend_units(s, ME, "interview")
        assert service.remaining_units(s, ME) == service.DAILY_UNITS - 2 * first
        assert s.query(QuotaLedger).one().units_used == 2 * first
    engine.dispose()
