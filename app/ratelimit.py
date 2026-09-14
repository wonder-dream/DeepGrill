"""进程内限流（决策 66）—— 继承 v1 的**滑动窗口 + reserve/settle**。

额度点管的是**成本**，这里管的是**滥用**：两者都会拒绝一个请求，但拒绝的理由与
处置完全不同 —— 前者说"今天的面试官额度用完了，明天再来"，后者说"你发得太快了，
等两秒"。把它们合成一个机制，前端就只能给出一句模糊的话。

## 继承的是**语义**，不是那个开关的值

`docs/v1行为规格.md` §9 把 v1 的限流标成「继承语义」，那句原话是：

> 内存滑动窗口 + **reserve/settle**：4xx 释放额度，只有真正执行的请求计数

这条规则对**成本型**限流成立（4xx 没真的花钱，不该记账），对**防滥用型**正好相反
（失败的尝试恰恰是要拦的东西：扫 404 的、撞密码的，全都返回 4xx）。所以三个限流器
各有各的取值，理由写在 `Limit.release_on_client_error` 上 —— 照抄一个开关的值到
三个用途上，会得到"限流器保护不了它该保护的东西"。

## 三个限流器

| 名字 | 键 | 管什么 | 4xx 释放 |
|---|---|---|---|
| `requests` | IP | 全部请求（防扫描 / 防单机灌流量） | 否 |
| `auth` | IP | 登录 / 注册（防撞密码） | 否 |
| `llm` | 用户 | 面试官动作（成本型，**这才是 v1 那条语义的落点**） | **是** |

## 回收者（AGENTS.md §3.2）

v1 的限流计数**永不淘汰**，是它自己点名的内存泄漏。所以这里有两个上限，缺一不可：

· **TTL**：窗口外的 key 定期清掉（每个窗口最多扫一次，不是每个请求都扫）
· **容量**：key 数超上限时丢最旧的（防"每次换一个 IP/key"把内存撑满）
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

#: 每个限流器最多记住多少个 key。超了就丢最旧的 —— 没有它，"换 key" 就是一条
#: 免费的内存增长路径（v1 的教训：进内存的东西必须有容量上限）。
DEFAULT_MAX_KEYS = 5000


@dataclass(frozen=True)
class Limit:
    """一个限流器的参数。"""

    requests: int
    per_seconds: float
    #: 客户端错误（4xx）时把占的那一格还回去。
    #:
    #: `True` = **成本型**（v1 的 reserve/settle 语义）：请求没真的执行，不该记账。
    #: `False` = **防滥用型**：失败的尝试正是要拦的东西，还回去等于没拦。
    release_on_client_error: bool = True
    name: str = ""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    #: 还要等几秒。0 表示不用等（放行时恒为 0）。
    retry_after: int = 0


@dataclass
class SlidingWindowLimiter:
    """按 key 的滑动窗口。**线程安全**（`def` 端点跑在线程池里，会并发）。

    窗口里存的是"每次放行的时间戳"，所以它是**真的滑动**：不是"每 60 秒清零一次"
    （那种固定窗口在边界上会放过两倍的量）。
    """

    limit: Limit
    max_keys: int = DEFAULT_MAX_KEYS
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: 下一次全量扫的时间（TTL 回收）。每个窗口最多扫一次。
    #: `None` = 还没定过 —— 第一次 reserve 只登记时间，不扫（那时也没什么可扫的）。
    _next_sweep: float | None = None
    #: 计数（观测页看得到 —— 回收者必须看得见，否则"有没有在回收"只能靠猜）
    reserved: int = 0
    denied: int = 0
    released: int = 0
    evicted: int = 0
    sweeps: int = 0

    # -- 主入口 ------------------------------------------------------------
    def reserve(self, key: str) -> Decision:
        """占一格。**先占再执行**（reserve）—— 这样并发请求不会都以为自己在配额内。"""
        now = self.clock()
        with self._lock:
            self._maybe_sweep(now)
            window = self._hits.setdefault(key, deque())
            cutoff = now - self.limit.per_seconds
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.limit.requests:
                self.denied += 1
                wait = window[0] + self.limit.per_seconds - now
                return Decision(
                    allowed=False, remaining=0, retry_after=max(1, math.ceil(wait))
                )
            window.append(now)
            self.reserved += 1
            # ⚠️ 容量上限必须**在插入时**守，不能只靠定期 sweep：定期扫的间隔是
            # 一个窗口，而一个窗口内塞进几万个新 key 早就把内存撑爆了。
            # "限流器自己把自己撑爆"是最讽刺的一种失败。
            self._enforce_capacity()
            return Decision(allowed=True, remaining=self.limit.requests - len(window))

    def release(self, key: str) -> None:
        """settle：这一次**没有真的执行**，把刚占的那一格还回去。

        还的是**最近那一格**（`pop()`）：reserve 与 release 在同一个请求里成对出现，
        而"最近一格"就是它自己那一格。全局按最早一格还（`popleft`）会在并发下
        还错别人的 —— 那会让窗口悄悄变小。
        """
        with self._lock:
            window = self._hits.get(key)
            if window:
                window.pop()
                self.released += 1

    # -- 回收者 ------------------------------------------------------------
    def _enforce_capacity(self) -> int:
        """超容量时丢"最久没出现过"的 key。返回丢了几个。**调用方已持锁。**

        一次多丢一点（至少 `max_keys // 10`），而不是每次只丢一个：后者会让**每一次**
        插入都做一次全排序，而那正好发生在"有人正在猛灌 key"的时候 —— 攻击者不仅
        撑内存，还能顺手把 CPU 占满。批量丢把这个成本摊薄。
        """
        overflow = len(self._hits) - self.max_keys
        if overflow <= 0:
            return 0
        batch = max(overflow, self.max_keys // 10)
        oldest = sorted(self._hits, key=lambda k: self._hits[k][-1] if self._hits[k] else 0.0)
        for k in oldest[:batch]:
            del self._hits[k]
        self.evicted += min(batch, len(oldest))
        return min(batch, len(oldest))

    def sweep(self, *, now: float | None = None) -> int:
        """清掉窗口外的 key；并守住容量上限。返回清掉几个。"""
        moment = self.clock() if now is None else now
        cutoff = moment - self.limit.per_seconds
        with self._lock:
            self.sweeps += 1
            stale = [
                k for k, window in self._hits.items()
                if not window or window[-1] <= cutoff
            ]
            for k in stale:
                del self._hits[k]
            self.evicted += len(stale)
            removed_by_capacity = self._enforce_capacity()
            self._next_sweep = moment + self.limit.per_seconds
            return len(stale) + removed_by_capacity

    def _maybe_sweep(self, now: float) -> None:
        """到点了才扫（不是每个请求都扫）。**调用方已持锁。**

        ⚠️ 不能转调 `sweep()`：它会再取一次锁，而 `threading.Lock` 不可重入 ——
        那会**死锁**（不是报错），而症状是"限流一上线就挂住"。
        """
        if self._next_sweep is None:
            # 第一次：只登记时间。此刻字典还是空的，扫也没东西可扫。
            self._next_sweep = now + self.limit.per_seconds
            return
        if now < self._next_sweep:
            return
        cutoff = now - self.limit.per_seconds
        stale = [k for k, w in self._hits.items() if not w or w[-1] <= cutoff]
        for k in stale:
            del self._hits[k]
        self.evicted += len(stale)
        self.sweeps += 1
        self._next_sweep = now + self.limit.per_seconds

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "keys": len(self._hits),
                "reserved": self.reserved,
                "denied": self.denied,
                "released": self.released,
                "evicted": self.evicted,
                "sweeps": self.sweeps,
                "max_keys": self.max_keys,
            }


@dataclass
class RateLimiters:
    """一个 app 的三个限流器。**挂在 `app.state` 上，不是模块全局**。

    模块全局会在同一进程里的多个 app 之间互相串（测试就是这么用的：一个测试里
    两个客户端指向同一个 app，另一些测试各建各的 app）。
    """

    requests: SlidingWindowLimiter
    auth: SlidingWindowLimiter
    llm: SlidingWindowLimiter
    enabled: bool = True

    @classmethod
    def from_settings(cls, settings) -> RateLimiters:
        return cls(
            requests=SlidingWindowLimiter(
                Limit(settings.ratelimit_requests, settings.ratelimit_window_seconds,
                      release_on_client_error=False, name="requests")
            ),
            auth=SlidingWindowLimiter(
                Limit(settings.ratelimit_auth_requests, settings.ratelimit_auth_window_seconds,
                      release_on_client_error=False, name="auth")
            ),
            llm=SlidingWindowLimiter(
                Limit(settings.ratelimit_llm_requests, settings.ratelimit_window_seconds,
                      release_on_client_error=True, name="llm")
            ),
            enabled=settings.ratelimit_enabled,
        )

    def all(self) -> list[SlidingWindowLimiter]:
        return [self.requests, self.auth, self.llm]


def client_ip(request, *, trust_proxy: bool) -> str:
    """请求来自哪个 IP。

    `trust_proxy` 是**必填的关键字参数**（没有默认值）：这是安全相关的取值，给个
    默认等于"某个调用点忘了传就静默采用某个策略" —— 而错的策略要么让限流形同虚设
    （信了客户端伪造的头），要么把所有用户算成一个人（代理后面全用源站看到的 IP）。
    两种都不会报错。

    ⚠️ **转发头只在显式信任代理时采用**（`DEEPGRILL_TRUST_PROXY_HEADERS=true`，
    线上在 Cloudflare 后面必须开）。不信任时：`X-Forwarded-For` 是客户端能自己写
    的，信了它等于把"按 IP 限流"变成"按客户端随便填的字符串限流"—— 攻击者填一个
    随机值就绕过了。

    信任时优先 `CF-Connecting-IP`（Cloudflare 会覆盖客户端自带的同名头），其次
    `X-Forwarded-For` 的第一段。
    """
    if trust_proxy:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def settle_on_response(reservations: list[tuple[SlidingWindowLimiter, str]], status: int) -> None:
    """按响应状态做 settle。**429 本身不释放** —— 否则限流器会把自己擦掉。

    （一个被限流的请求如果"因为返回 4xx 所以把占的格子还回去"，那么它永远限不住
    任何人：每次拒绝都把计数减回去，窗口里永远是 0。）
    """
    if not (400 <= status < 500) or status == 429:
        return
    for limiter, key in reservations:
        if limiter.limit.release_on_client_error:
            limiter.release(key)


def too_many(decision: Decision, retry_after: int | None = None) -> dict[str, object]:
    """429 的响应体。**告诉调用方还要等几秒** —— 只回一句"太快了"是没用的。"""
    wait = decision.retry_after if retry_after is None else retry_after
    return {"error": "请求太频繁，请稍后再试", "retry_after": wait}
