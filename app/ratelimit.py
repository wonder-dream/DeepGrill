"""登录/注册速率限制（防爆破）：内存滑动窗口，按 IP，零依赖。

规则：同一 IP 在窗口内失败（响应 ≥400）≥ fail_limit 次 → 429；
任意成功响应清空该 IP 计数。内存态重启即失效（可接受，单进程部署）。
"""
import threading
import time

FAIL_LIMIT = 5
WINDOW_SECONDS = 60

_states: dict[str, list[float]] = {}
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


def reset() -> None:
    """清空全部计数（测试用）。"""
    with _lock:
        _states.clear()
