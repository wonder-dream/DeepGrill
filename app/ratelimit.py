"""速率限制（防爆破/防脚本滥用）：内存滑动窗口，按 IP 或用户，零依赖。

- `check/fail/success`：登录/注册失败限速（响应 ≥400 计数，成功清零）
- `allow`：通用突发限流（写接口防滥用，放行计入计数，窗口内超限 429）
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
    now = time.time()
    with _lock:
        hits = [t for t in _burst.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            _burst[key] = hits
            return False
        hits.append(now)
        _burst[key] = hits
        return True


def reset() -> None:
    """清空全部计数（测试用）。"""
    with _lock:
        _states.clear()
        _burst.clear()
