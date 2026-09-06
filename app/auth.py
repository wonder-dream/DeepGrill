"""A2 认证：scrypt 密码哈希 + DB token（零新依赖）。

- 密码：hashlib.scrypt（标准库），存储格式 scrypt$salt_hex$digest_hex
- Token：随机 32 字节 hex，DB 存 sha256(token)（可撤销、重启不掉线）
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select

from .db import commit, get_session
from .models import User, UserToken

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

TOKEN_TTL_DAYS = 30  # token 有效期（天），过期惰性删除

MAX_USERS = int(os.environ.get("MAX_USERS", "20"))  # 注册名额（不含 owner）

PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,}$")


def validate_password(password: str) -> str | None:
    if not PASSWORD_RE.match(password):
        return "密码需至少 8 位，且包含字母和数字"
    return None


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
        )
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def create_token(user_id: int) -> str:
    token = secrets.token_hex(32)
    with get_session() as session:
        session.add(
            UserToken(
                user_id=user_id,
                token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                expires_at=datetime.now() + timedelta(days=TOKEN_TTL_DAYS),
            )
        )
        commit(session)
    return token


def user_from_token(token: str) -> User | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with get_session() as session:
        row = session.scalars(
            select(UserToken).where(UserToken.token_hash == token_hash)
        ).first()
        if row is None:
            return None
        if row.expires_at is not None and row.expires_at < datetime.now():
            session.delete(row)  # 惰性清理：过期 token 删行
            commit(session)
            return None
        return session.get(User, row.user_id)


def revoke_token(token: str) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with get_session() as session:
        row = session.scalars(
            select(UserToken).where(UserToken.token_hash == token_hash)
        ).first()
        if row is not None:
            session.delete(row)
            commit(session)


def user_count() -> int:
    """当前非 owner 用户数（注册名额判定）。"""
    with get_session() as session:
        return len(
            session.scalars(select(User).where(User.role == "user")).all()
        )
