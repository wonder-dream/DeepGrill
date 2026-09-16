"""口令与令牌的密码学（`docs/v1行为规格.md` §9 —— v1 这部分做对了，整套继承）。

它不是"领域"也不是"业务规则"，而是**纯函数集合**：给一个口令算哈希、给一个
随机源算令牌。没有数据库、没有配置 —— 所以它可被隔离测试，也可以被将来的
管理员重置口令脚本直接复用。

两条 v1 的实测结论（都写在这里而不是靠注释记着）：

① `scrypt` 参数 n=2^14, r=8, p=1；比对必须用 `hmac.compare_digest`
   （普通 `==` 会在第一个不同字节处短路，是时序侧信道）。
② **令牌只在库里存 sha256**，明文只出现在 cookie 里。所以库泄露不等于会话可被
   接管 —— 这条是"令牌表被拖库"与"账号被接管"之间唯一的墙。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

#: scrypt 参数（v1 标定值，继承）
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

#: 令牌字节数。32 字节 = 256 位随机（v1 值，继承）。
TOKEN_BYTES = 32

#: 进程内**同时在跑**的哈希数上限（决策 88）。
#:
#: scrypt 每次要 `128 * n * r` = **16 MiB**，而 anyio 的默认线程池是 40（每个同步
#: 端点都跑在那里）⇒ 最坏 ~640 MiB 峰值，同时两个核被哈希占满。实测 40 路并发
#: 错口令登录：RSS **+173 MB**；加 4 路闸门后 **+33 MB**。
#:
#: 它与限流器（决策 66）管的是两件事：这里管"同一时刻有几个哈希在跑"，
#: 那里管"单位时间允许多少次"。被伪造转发头绕开限流之后（`trust_proxy_headers=true`
#: 而前面没有代理），只剩这道闸门 —— 所以它不是优化，是兜底。
#:
#: 上限由装配根按配置设一次（`create_app` → `set_hash_concurrency`）；测试与脚本
#: 不调它就吃默认值 4，不需要知道配置的存在。
_hash_gate = threading.BoundedSemaphore(4)


def set_hash_concurrency(limit: int) -> None:
    """设置同时进行的哈希数上限（配置项 `DEEPGRILL_PASSWORD_HASH_CONCURRENCY`）。"""
    global _hash_gate
    if limit > 0:
        _hash_gate = threading.BoundedSemaphore(limit)


def hash_password(password: str) -> str:
    """`scrypt$<salt_hex>$<digest_hex>`（格式继承 v1，便于将来对照）。"""
    salt = secrets.token_bytes(16)
    with _hash_gate:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN,
        )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """比对口令。**任何格式异常都返回 False**，不抛异常。

    理由：这个函数的调用方是登录端点，而"哈希串坏了"与"口令不对"对用户是同一件
    事（都该得到"邮箱或密码不正确"）。分岔成异常会让"这个账号的哈希格式特殊"
    变成可观测的差异。
    """
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    # ⚠️ 闸门只包住**真正要算**的那一次调用：格式不对的输入在上面就返回了，
    # 让它占一个槽等于把"坏输入"变成"排队成本"（决策 88 的另一半理由）。
    with _hash_gate:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=len(expected) or _SCRYPT_DKLEN,
        )
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    """生成一个会话令牌（明文）。它只该出现在 cookie 里。"""
    return secrets.token_hex(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """库里存这个，不存明文。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
