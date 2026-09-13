"""速率限制（防爆破/防脚本滥用）：内存滑动窗口，按 IP 或用户，零依赖。

- `check/fail/success`：登录/注册失败限速（响应 ≥400 计数，成功清零）
- `reserve/settle`：写接口额度（预占→按响应状态结算）。被业务层拒绝（4xx）的请求
  不计额度：否则批量非法请求会把额度吃光，合法请求反而被 429。
- `allow`：预占即计数（保留给无需按结果结算的调用方）
内存态重启即失效（可接受，单进程部署）。
"""
import threading
import time

FAIL_LIMIT = 5
WINDOW_SECONDS = 60

_states: dict[str, list[float]] = {}
_burst: dict[str, list[float]] = {}
_lock = threading.Lock()


def check(ip: str, fail_limit: int = FAIL_LIMIT, window_seconds: int = WINDOW_SECONDS) -> bool:
    """返回是否放行；放行不计数。"""
    now = time.time()
    with _lock:
        fails = [t for t in _states.get(ip, []) if now - t < window_seconds]
        if len(fails) >= fail_limit:
            return False
        _states[ip] = fails
        return True


def fail(ip: str) -> None:
    """记录一次失败请求。"""
    with _lock:
        _states.setdefault(ip, []).append(time.time())


def success(ip: str) -> None:
    """成功请求清零该 IP 计数。"""
    with _lock:
        _states.pop(ip, None)


def allow(key: str, limit: int, window_seconds: int = WINDOW_SECONDS) -> bool:
    """通用突发限流：滑动窗口内超 limit 次返回 False；放行计入计数。"""
    ok, _ = reserve(key, limit, window_seconds)
    return ok


def reserve(
    key: str, limit: int, window_seconds: int = WINDOW_SECONDS
) -> tuple[bool, list | None]:
    """预占一个额度：返回 (是否放行, 预占的时间戳列表快照)。

    快照交给 settle 结算：列表仍在则请求被计入；被移除则该次请求不占额度。
    """
    now = time.time()
    with _lock:
        hits = [t for t in _burst.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            _burst[key] = hits
            return False, None
        hits.append(now)
        _burst[key] = hits
        return True, hits


def settle(key: str, reservations: list | None, *, keep: bool) -> None:
    """结算：keep=False（业务层 4xx，请求未产生实际工作）时移除预占槽位。"""
    if keep or not reservations:
        return
    with _lock:
        hits = _burst.get(key)
        if hits is None:
            return
        for res in reservations:
            try:
                hits.remove(res)
            except ValueError:
                pass  # 已被窗口滚出或并发结算，忽略


def reset() -> None:
    """清空全部计数（测试用）。"""
    with _lock:
        _states.clear()
        _burst.clear()
