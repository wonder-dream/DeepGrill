"""哈希闸门（决策 88）：scrypt 每次 16 MiB，而线程池是 40。

这条测试钉住的是**同时进行的哈希数**，不是"哈希还能算" —— 后者本来就会过。
"""

from __future__ import annotations

import hashlib
import threading
import time

import pytest

from app import security


@pytest.fixture(autouse=True)
def restore_gate():
    """每个用例都把闸门恢复成默认值 4 —— 否则先跑的用例会改掉后跑的语义。"""
    security.set_hash_concurrency(4)
    yield
    security.set_hash_concurrency(4)


def _instrument(monkeypatch: pytest.MonkeyPatch, hold: float = 0.05) -> dict[str, int]:
    """把 `hashlib.scrypt` 换成一个会停一会儿的假实现，记录**同时在跑**的峰值。"""
    state = {"current": 0, "peak": 0}
    lock = threading.Lock()
    real = hashlib.scrypt

    def fake(password, *, salt, n, r, p, dklen):  # noqa: ANN001, ARG001
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        try:
            time.sleep(hold)
            return b"\x00" * dklen
        finally:
            with lock:
                state["current"] -= 1

    monkeypatch.setattr(hashlib, "scrypt", fake)
    assert real is not fake
    return state


def _stored() -> str:
    return "scrypt$" + "00" * 16 + "$" + "11" * 32


def test_verify_password_never_exceeds_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    security.set_hash_concurrency(2)
    state = _instrument(monkeypatch)
    threads = [
        threading.Thread(target=security.verify_password, args=("pw", _stored()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] <= 2, f"同时在跑的哈希数到了 {state['peak']}，闸门没生效"


def test_hash_password_never_exceeds_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    security.set_hash_concurrency(3)
    state = _instrument(monkeypatch)
    threads = [threading.Thread(target=security.hash_password, args=("pw",)) for _ in range(9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] <= 3, f"同时在跑的哈希数到了 {state['peak']}，闸门没生效"


def test_a_bad_format_hash_does_not_take_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """格式不对的哈希在上面就返回了 —— 它不该占一个槽（否则坏输入变成排队成本）。"""
    security.set_hash_concurrency(1)
    state = _instrument(monkeypatch)
    assert security.verify_password("pw", "不是哈希") is False
    assert security.verify_password("pw", "bcrypt$aa$bb") is False
    assert state["peak"] == 0


def test_set_hash_concurrency_ignores_non_positive_values() -> None:
    security.set_hash_concurrency(0)
    security.set_hash_concurrency(-3)
    # 仍然是 4 —— 一个"设成 0"就永久卡死所有登录的口子不该存在
    assert security._hash_gate._value == 4
    security.set_hash_concurrency(6)
    assert security._hash_gate._value == 6
