"""限流的测试（决策 66：继承 v1 的滑动窗口 + reserve/settle）。

这一笔的三件事各有各的失败方式，所以分开测：

· **滑动窗口**：固定窗口（每 60 秒清零）在边界上会放过两倍的量，而"看起来也是
  限流"。所以测试要**真的把时钟推过去**，断言它一格一格地滑。
· **reserve/settle**：`release` 还错格子（还成最早那一格）会让窗口悄悄变小 ——
  并发下表现为"限流偶尔失灵"。所以有一条专门测"还的是最近那一格"。
· **回收者**：v1 的限流计数永不淘汰是它点名的内存泄漏。所以 TTL 与容量上限
  各有一条测试，且断言"被清掉的 key 真的不在内存里了"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Domain, KnowledgePoint, Question, User
from app.deps import get_llm
from app.main import STATIC_MAX_AGE, create_app
from app.ratelimit import (
    Limit,
    RateLimiters,
    SlidingWindowLimiter,
    client_ip,
)
from app.security import hash_password
from migrations._runner import migrate
from tests.fakes import FakeLLM, round_reply

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


class FakeClock:
    """可推进的时钟 —— 滑动窗口的测试**必须能控制时间**，否则只能靠 sleep。"""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(requests: int = 3, per_seconds: float = 60, **kw) -> tuple[SlidingWindowLimiter, FakeClock]:
    clock = FakeClock()
    return (
        SlidingWindowLimiter(
            Limit(requests, per_seconds, name="test"), clock=clock, **kw
        ),
        clock,
    )


# ---------------------------------------------------------------------------
# 滑动窗口
# ---------------------------------------------------------------------------
def test_allows_up_to_the_limit_then_denies() -> None:
    limiter, _ = _limiter(requests=3)
    assert [limiter.reserve("k").allowed for _ in range(3)] == [True, True, True]
    denied = limiter.reserve("k")
    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.retry_after >= 1


def test_remaining_counts_down() -> None:
    limiter, _ = _limiter(requests=3)
    assert limiter.reserve("k").remaining == 2
    assert limiter.reserve("k").remaining == 1
    assert limiter.reserve("k").remaining == 0


def test_window_really_slides_not_resets() -> None:
    """**滑动**，不是"每 60 秒清零"。

    固定窗口的漏洞：在第 59.9 秒打满，第 60.1 秒又能打满 —— 两秒内两倍量。
    滑动窗口里，每一格都要等到**它自己**满 60 秒才出去。
    """
    limiter, clock = _limiter(requests=2, per_seconds=60)
    assert limiter.reserve("k").allowed is True   # t=1000
    clock.advance(30)
    assert limiter.reserve("k").allowed is True   # t=1030
    clock.advance(31)                             # t=1061：第一格（1000）过期，第二格（1030）还没
    assert limiter.reserve("k").allowed is True
    # 此刻窗口里是 [1030, 1061]，再来一个就该被拒
    assert limiter.reserve("k").allowed is False


def test_keys_are_independent() -> None:
    limiter, _ = _limiter(requests=1)
    assert limiter.reserve("a").allowed is True
    assert limiter.reserve("b").allowed is True, "另一个 key 不该被 a 用掉"
    assert limiter.reserve("a").allowed is False


def test_retry_after_points_at_the_oldest_slot() -> None:
    """`retry_after` 必须指向**最早那一格过期**的时间 —— 否则用户等的时间不准。"""
    limiter, clock = _limiter(requests=1, per_seconds=60)
    limiter.reserve("k")
    clock.advance(20)
    assert limiter.reserve("k").retry_after == 40


# ---------------------------------------------------------------------------
# reserve / settle
# ---------------------------------------------------------------------------
def test_release_gives_the_slot_back() -> None:
    limiter, _ = _limiter(requests=1)
    assert limiter.reserve("k").allowed is True
    assert limiter.reserve("k").allowed is False
    limiter.release("k")
    assert limiter.reserve("k").allowed is True, "4xx 释放之后应当还能再来一次"


def test_release_returns_the_newest_slot_not_the_oldest() -> None:
    """还的是**最近那一格**（reserve/release 成对）。

    还最早那一格会让窗口悄悄变小：本来到点才能进来的请求会提前进来 ——
    而症状是"限流偶尔失灵"，最难查的那一种。

    判别法：三格打满（t=1000/1010/1020）后还掉一格，再跳到 t=1061。
    · 还对了（去掉 1020）：窗口 = [1010]，t=1000 已过期 → 还剩**两格**可用
    · 还错了（去掉 1000）：窗口 = [1010, 1020] → 只剩**一格**可用
    所以"连着两次都能进"这条断言正好把两者分开。
    """
    limiter, clock = _limiter(requests=3, per_seconds=60)
    limiter.reserve("k")            # t=1000（最早那一格）
    clock.advance(10)
    limiter.reserve("k")            # t=1010
    clock.advance(10)
    limiter.reserve("k")            # t=1020（最近那一格，应该被还掉）
    limiter.release("k")

    clock.advance(41)               # t=1061 → cutoff=1001，t=1000 那一格过期
    assert limiter.reserve("k").allowed is True
    assert limiter.reserve("k").allowed is True, (
        "还错了格子 —— 窗口比它该有的小，用户的配额被凭空吃掉一格"
    )


def test_release_on_an_unknown_key_is_harmless() -> None:
    limiter, _ = _limiter()
    limiter.release("从来没有过")  # 不该抛


# ---------------------------------------------------------------------------
# 回收者（AGENTS.md §3.2）
# ---------------------------------------------------------------------------
def test_expired_keys_are_swept_automatically() -> None:
    """**TTL 回收在正常流量里就会发生**（不用等人手动调 sweep）—— 一个窗口之后
    下一个请求顺手把过期的 key 清掉。"""
    limiter, clock = _limiter(requests=1, per_seconds=60)
    limiter.reserve("old")
    clock.advance(61)
    limiter.reserve("new")
    assert "old" not in limiter._hits, "窗口外的 key 必须真的从内存里消失"
    assert "new" in limiter._hits
    assert limiter.sweeps == 1


def test_explicit_sweep_also_reaps() -> None:
    limiter, clock = _limiter(requests=1, per_seconds=60)
    limiter.reserve("old")
    clock.advance(61)
    assert limiter.sweep() == 1
    assert limiter._hits == {}


def test_sweep_is_throttled_not_per_request() -> None:
    """回收**不能每个请求都全量扫** —— 那是把 O(keys) 放进热路径。"""
    limiter, clock = _limiter(requests=99, per_seconds=60)
    for i in range(5):
        limiter.reserve(f"k{i}")
    assert limiter.sweeps == 0, "第一次 reserve 只是登记下一次扫的时间"
    clock.advance(61)
    limiter.reserve("k9")
    assert limiter.sweeps == 1
    clock.advance(1)
    limiter.reserve("k10")
    assert limiter.sweeps == 1, "一个窗口内最多扫一次"


def test_capacity_cap_evicts_the_oldest_keys() -> None:
    """**容量上限**：key 数超了就丢最旧的 —— 否则"每次换一个 key"能撑满内存。"""
    limiter, clock = _limiter(requests=5, per_seconds=600, max_keys=3)
    for i in range(3):
        limiter.reserve(f"k{i}")
        clock.advance(1)
    limiter.reserve("k3")   # 第 4 个 key → 超容量
    assert len(limiter._hits) <= 3
    assert "k0" not in limiter._hits, "丢的必须是最旧的"
    assert "k3" in limiter._hits


def test_stats_expose_the_reaper() -> None:
    """观测页要看得见回收者 —— "有没有在回收"不能只能靠猜。"""
    limiter, clock = _limiter(requests=1, per_seconds=60)
    limiter.reserve("k")
    limiter.reserve("k")            # 被拒
    limiter.release("k")
    clock.advance(61)
    limiter.sweep()
    stats = limiter.stats()
    assert stats["reserved"] == 1 and stats["denied"] == 1 and stats["released"] == 1
    assert stats["evicted"] >= 1 and stats["keys"] == 0


# ---------------------------------------------------------------------------
# 429 本身不释放（否则限流器会把自己擦掉）
# ---------------------------------------------------------------------------
def test_settle_does_not_release_on_429() -> None:
    from app.ratelimit import settle_on_response

    limiter, _ = _limiter(requests=1)
    limiter.reserve("k")
    settle_on_response([(limiter, "k")], 429)
    assert limiter.reserve("k").allowed is False, "被拒的请求把格子还回去 = 永远限不住"


def test_settle_releases_on_other_4xx_only_for_cost_type() -> None:
    from app.ratelimit import settle_on_response

    cost, _ = _limiter(requests=1)          # release_on_client_error 默认 True
    abuse, _ = _limiter(requests=1)
    abuse.limit = Limit(1, 60, release_on_client_error=False, name="abuse")
    for limiter in (cost, abuse):
        limiter.reserve("k")

    settle_on_response([(cost, "k"), (abuse, "k")], 400)
    assert cost.reserve("k").allowed is True, "成本型：4xx 该把额度还回来（v1 的语义）"
    assert abuse.reserve("k").allowed is False, "防滥用型：失败的尝试正是要拦的东西"


def test_settle_does_nothing_on_success() -> None:
    from app.ratelimit import settle_on_response

    limiter, _ = _limiter(requests=1)
    limiter.reserve("k")
    settle_on_response([(limiter, "k")], 200)
    assert limiter.reserve("k").allowed is False


# ---------------------------------------------------------------------------
# 客户端 IP
# ---------------------------------------------------------------------------
class _Req:
    def __init__(self, headers: dict[str, str], host: str = "10.0.0.9") -> None:
        self.headers = headers
        self.client = type("C", (), {"host": host})()


def test_proxy_headers_are_ignored_by_default() -> None:
    """**默认不信转发头** —— 它是客户端能自己写的，信了就等于把按 IP 限流变成
    "按客户端随便填的字符串限流"。"""
    request = _Req({"x-forwarded-for": "1.2.3.4", "cf-connecting-ip": "5.6.7.8"})
    assert client_ip(request, trust_proxy=False) == "10.0.0.9"


def test_proxy_headers_are_used_when_trusted() -> None:
    request = _Req({"x-forwarded-for": "1.2.3.4, 10.0.0.1", "cf-connecting-ip": "5.6.7.8"})
    assert client_ip(request, trust_proxy=True) == "5.6.7.8", "优先 Cloudflare 那个"
    assert client_ip(_Req({"x-forwarded-for": "1.2.3.4, 10.0.0.1"}), trust_proxy=True) == "1.2.3.4"


# ---------------------------------------------------------------------------
# 接进应用之后
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "rl.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="r@local", username="r", password_hash=_HASH, role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile", difficulty=3,
                       primary_point_id=1, origin="seed", visibility="public"))
        s.commit()
    return path


def _app(db: Path, **overrides):
    settings = Settings(database_path=db, **overrides)
    application = create_app(settings)
    application.state.test_db = db
    return application


def test_healthz_is_exempt(db: Path) -> None:
    """探针不该被限流 —— 它要能一直回答"进程还活着吗"。"""
    app = _app(db, ratelimit_requests=1)
    with TestClient(app) as c:
        for _ in range(5):
            assert c.get("/healthz").status_code == 200


def test_static_files_are_exempt(db: Path) -> None:
    """一个页面会拉好几个静态文件 —— 限流会把页面本身弄坏。"""
    app = _app(db, ratelimit_requests=1)
    with TestClient(app) as c:
        assert c.get("/static/app.css").status_code == 200
        assert c.get("/static/app.css").status_code == 200


# ---------------------------------------------------------------------------
# 静态资源的两条硬化（ADR-0008：2C2G + 境内直连美东 RTT 200-300ms）
# ---------------------------------------------------------------------------
def test_static_files_are_cacheable(db: Path) -> None:
    """静态资源要能缓存 —— 否则每个页面都跨一次太平洋去取 CSS（ADR-0008）。"""
    app = _app(db)
    with TestClient(app) as c:
        response = c.get("/static/app.css")
        assert response.headers["cache-control"] == f"public, max-age={STATIC_MAX_AGE}"


def test_html_pages_are_not_cached(db: Path) -> None:
    """**页面本身不能跟着缓存**：它带登录态与实时数据，缓存住会串号/发旧数据。"""
    app = _app(db)
    with TestClient(app) as c:
        assert "cache-control" not in c.get("/").headers


def test_large_responses_are_gzipped(db: Path) -> None:
    """源站自己压缩（ADR-0008 的"静态资源强缓存 + 压缩"）。

    源站自己压的意义是**可移植**：Cloudflare 不可达时没有人替我们压。
    """
    app = _app(db)
    with TestClient(app) as c:
        response = c.get("/", headers={"accept-encoding": "gzip"})
        assert response.headers.get("content-encoding") == "gzip"


def test_tiny_responses_are_left_alone(db: Path) -> None:
    """压缩有阈值：小响应压了反而更大（GZip 头就有 20 字节）。"""
    app = _app(db)
    with TestClient(app) as c:
        response = c.get("/healthz", headers={"accept-encoding": "gzip"})
        assert response.headers.get("content-encoding") != "gzip"


def test_ip_limit_returns_429_with_retry_after(db: Path) -> None:
    app = _app(db, ratelimit_requests=2, ratelimit_window_seconds=60)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert c.get("/").status_code == 200
        r = c.get("/")
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1


def test_browser_gets_a_readable_429_page(db: Path) -> None:
    """HTML 路由给一页能读的东西（ADR-0004：大部分路由回 HTML），并写明等多久。"""
    app = _app(db, ratelimit_requests=1)
    with TestClient(app) as c:
        c.get("/", headers={"accept": "text/html"})
        r = c.get("/", headers={"accept": "text/html"})
        assert r.status_code == 429
        assert "请求太频繁" in r.text
        assert "秒" in r.text


def test_api_callers_get_json(db: Path) -> None:
    app = _app(db, ratelimit_requests=1)
    with TestClient(app) as c:
        c.post("/interview/1/voice", headers={"accept": "application/json"})
        r = c.get("/", headers={"accept": "application/json"})
        assert r.status_code == 429
        assert r.json()["retry_after"] >= 1


def test_scanning_4xx_still_counts(db: Path) -> None:
    """**扫 404 也要计数** —— 那一档是防滥用型，还回去等于没拦。

    （v1 的"4xx 释放额度"是**成本型**的规则，不是所有限流的规则。见 `app/ratelimit.py`。）
    """
    app = _app(db, ratelimit_requests=2)
    with TestClient(app) as c:
        assert c.get("/nope").status_code == 404
        assert c.get("/nope").status_code == 404
        assert c.get("/nope").status_code == 429


def test_auth_limit_is_tighter_than_the_general_one(db: Path) -> None:
    """登录那一档更严：撞密码是这里唯一的现实攻击面。"""
    app = _app(db, ratelimit_requests=50, ratelimit_auth_requests=2)
    with TestClient(app) as c:
        for _ in range(2):
            c.post("/login", data={"email": "x@local", "password": "bad"})
        r = c.post("/login", data={"email": "x@local", "password": "bad"})
        assert r.status_code == 429


def test_per_user_limit_blocks_the_interviewer_actions(db: Path) -> None:
    """按**用户**那一档：连开太多场面试官动作就会被拦（成本型）。"""
    app = _app(db, ratelimit_requests=100, ratelimit_llm_requests=1)
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(
        round_reply(hits=[(1, "命中")], prose="继续")
    )
    with TestClient(app) as c:
        c.post("/login", data={"email": "r@local", "password": PASSWORD})
        assert c.post("/interview/start", data={"mode": "drill", "question_id": "1"},
                      follow_redirects=False).status_code == 302
        r = c.post("/interview/start", data={"mode": "drill", "question_id": "1"})
        assert r.status_code == 429
        assert "太频繁" in r.text


def test_resume_submission_is_on_the_cost_limit(db: Path) -> None:
    """`/me/resume` 必须挂在按用户的成本档上 —— 它是全项目唯一漏掉的那个 LLM 端点。

    它一次请求跑**两次**模型调用（解析简历 + 出题），实测 25 连发 0 个 429：
    页面上的"生成"按钮是唯一能被反复点着烧钱的入口。
    """
    app = _app(db, ratelimit_requests=100, ratelimit_llm_requests=1)
    # 队列空的替身 ⇒ 模型失败 ⇒ 路由回 502（5xx 不释放预留格子，限流计数留得住）
    app.dependency_overrides[get_llm] = lambda: FakeLLM()
    resume = {"resume_text": "张三，后端三年，做过订单系统。" * 10}
    with TestClient(app) as c:
        c.post("/login", data={"email": "r@local", "password": PASSWORD})
        assert c.post("/me/resume", data=resume).status_code == 502, (
            "第一次要真的走到模型那一步，否则这条测试没测到限流"
        )
        assert c.post("/me/resume", data=resume).status_code == 429


def test_auth_limit_survives_rotating_forwarded_headers(db: Path) -> None:
    """**回归测试**（bug 33）：轮换转发头也绕不开按账号那一档（决策 92）。

    实测：`trust_proxy_headers=true` 时客户端自己写 `CF-Connecting-IP` 就能每次换一个
    "IP" —— 于是按 IP 的两档全部失效（15/15 错口令登录通过、0 个 429），而这件事在
    产品上就是"撞密码没有上限"。修法是**再加一道与 IP 无关的键**：账号。

    这里刻意让每个请求都带一个不同的转发头：按 IP 那一档会全部放行（所以它拦不住），
    能拦住的只可能是按账号那一档。
    """
    app = _app(db, ratelimit_requests=1000, ratelimit_auth_requests=2,
               ratelimit_auth_window_seconds=60, trust_proxy_headers=True)
    with TestClient(app) as c:
        for i in range(2):
            r = c.post("/login", data={"email": "r@local", "password": "wrong-pw"},
                       headers={"CF-Connecting-IP": f"203.0.113.{i}"})
            assert r.status_code == 200, "前两次是普通的「口令不对」，不该被限流"
        r = c.post("/login", data={"email": "r@local", "password": "wrong-pw"},
                   headers={"CF-Connecting-IP": "203.0.113.99"})
        assert r.status_code == 429, "换一个转发头就又能撞了 —— 按账号那一档没生效"


def test_disabled_leaves_everything_alone(db: Path) -> None:
    app = _app(db, ratelimit_enabled=False, ratelimit_requests=1)
    with TestClient(app) as c:
        for _ in range(5):
            assert c.get("/").status_code == 200


def test_limiters_live_on_app_state_not_module_globals(db: Path) -> None:
    """两个 app 各有各的限流器 —— 模块全局会让它们互相限流（测试里就是这么用的）。"""
    first = _app(db, ratelimit_requests=1)
    second = _app(db, ratelimit_requests=1)
    assert isinstance(first.state.ratelimiters, RateLimiters)
    assert first.state.ratelimiters is not second.state.ratelimiters
