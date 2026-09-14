"""账号领域的数据访问（ADR-0005：领域自己的表自己写）。

`users` / `user_tokens` / `invite_codes` / `quota_ledger` 只有这里碰 —— 别的领域
要"当前用户"就走 `service`，不自己去查 users 表。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import InviteCode, QuotaLedger, User, UserToken

#: 令牌有效期（v1 是 30 天，继承）。
TOKEN_TTL = timedelta(days=30)


def now_iso() -> str:
    """库里时间列存的是 `datetime('now')` 那种 TEXT（UTC，'YYYY-MM-DD HH:MM:SS'）。

    统一从这里产出，避免"有的地方写 ISO8601 带 T、有的写 SQLite 格式"——
    那样字符串比较会静默给出错误结果（而我们所有时间比较都是字符串比较）。
    """
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def find_user_by_email(session: Session, email: str) -> User | None:
    return session.execute(select(User).where(User.email == email)).scalar_one_or_none()


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def create_user(session: Session, *, email: str, username: str, password_hash: str) -> User:
    user = User(email=email, username=username, password_hash=password_hash, role="user")
    session.add(user)
    session.flush()
    return user


# ---------------------------------------------------------------------------
# 会话令牌
# ---------------------------------------------------------------------------
def issue_token(session: Session, user_id: int, token_hash: str) -> UserToken:
    token = UserToken(
        token_hash=token_hash,
        user_id=user_id,
        expires_at=(datetime.now(UTC) + TOKEN_TTL).strftime("%Y-%m-%d %H:%M:%S"),
    )
    session.add(token)
    return token


def user_for_token(session: Session, token_hash: str) -> User | None:
    """按令牌取用户。**过期的当场删掉**（惰性清理，v1 的做法，继承）。

    惰性清理而不是定时任务：定时任务在单机 2C2G 上是"多一个要保活的东西"，
    而这里每次登录态的读取顺手就清了。
    """
    row = session.execute(
        select(UserToken).where(UserToken.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at <= now_iso():
        session.delete(row)
        return None
    return session.get(User, row.user_id)


def revoke_token(session: Session, token_hash: str) -> None:
    session.execute(delete(UserToken).where(UserToken.token_hash == token_hash))


# ---------------------------------------------------------------------------
# 邀请码（决策 6：注册 = 邀请码）
# ---------------------------------------------------------------------------
def find_invite(session: Session, code: str) -> InviteCode | None:
    return session.execute(
        select(InviteCode).where(InviteCode.code == code)
    ).scalar_one_or_none()


def create_invite(session: Session, code: str, *, expires_at: str | None = None) -> InviteCode:
    invite = InviteCode(code=code, expires_at=expires_at)
    session.add(invite)
    return invite


def mark_invite_used(session: Session, invite: InviteCode, user_id: int) -> None:
    invite.used_by = user_id
    invite.used_at = now_iso()


def invite_is_usable(invite: InviteCode) -> bool:
    """空 `used_by` 即未使用；空 `expires_at` 即永久有效（决策 6）。"""
    if invite.used_by is not None:
        return False
    return invite.expires_at is None or invite.expires_at > now_iso()


def invite_state(invite: InviteCode) -> str:
    """把一张码的状态算成一个词 —— **后台要按状态看**，而这不是一个列（可从三列推出）。

    不新增列的理由与决策 38 那条"存一个可从别处推出的字段只会多一个可能不一致的来源"
    同一个道理：状态是 `used_by` 与 `expires_at` 的函数。
    """
    if invite.used_by is not None:
        return "used"
    if invite.expires_at is not None and invite.expires_at <= now_iso():
        return "expired"
    return "unused"


def list_invites(session: Session) -> list[InviteCode]:
    """全部邀请码（新的在前）—— 后台的列表。"""
    return list(
        session.execute(select(InviteCode).order_by(InviteCode.code.desc())).scalars().all()
    )


def delete_invite(session: Session, code: str) -> bool:
    """作废一张**未使用**的码（决策 6：可作废回收）。

    用过的码不许删：它是**发放记录**（谁用了、什么时候用的）。
    删掉它会让"这个用户是怎么进来的"永久查不到。
    """
    invite = find_invite(session, code)
    if invite is None:
        return False
    if invite.used_by is not None:
        raise ValueError("这张码已经被使用 —— 它是发放记录，不能删")
    session.delete(invite)
    session.flush()
    return True


def set_password(session: Session, user: User, password_hash: str) -> None:
    """改口令（管理员重置 / 用户自改都走这里）。

    ⚠️ 它**不撤销已有令牌** —— 那是调用方的决定（重置口令时通常应该撤销，
    见 `revoke_all_tokens`）。分清"改口令"与"踢下线"是两件事。
    """
    user.password_hash = password_hash
    session.flush()


def revoke_all_tokens(session: Session, user_id: int) -> int:
    """撤销这个人的全部会话令牌。**管理员重置口令时必须调它** ——
    否则旧会话仍然有效，"口令丢了"这件事就没被真正解决。"""
    result = session.execute(delete(UserToken).where(UserToken.user_id == user_id))
    session.flush()
    return result.rowcount or 0


def list_users(session: Session) -> list[User]:
    """全部账号（后台用）。"""
    return list(session.execute(select(User).order_by(User.id)).scalars().all())


# ---------------------------------------------------------------------------
# 额度账本（决策 13：单一额度点）
# ---------------------------------------------------------------------------
def quota_row(session: Session, user_id: int, day: str, kind: str = "day") -> QuotaLedger | None:
    return session.execute(
        select(QuotaLedger).where(
            QuotaLedger.user_id == user_id,
            QuotaLedger.kind == kind,
            QuotaLedger.day == day,
        )
    ).scalar_one_or_none()


def add_usage(
    session: Session, user_id: int, *, day: str, units: int = 0, tokens: int = 0
) -> QuotaLedger:
    """累加当天的用量。**每日一行**，不做全量流水（决策 13 + Open-2）。"""
    row = quota_row(session, user_id, day)
    if row is None:
        row = QuotaLedger(user_id=user_id, kind="day", day=day, units_used=0, tokens_used=0)
        session.add(row)
    row.units_used += units
    row.tokens_used += tokens
    row.updated_at = now_iso()
    return row


def recent_usage(session: Session, *, days: int = 30) -> list[QuotaLedger]:
    """最近的额度记录（按天倒序）—— 后台的"钱花在哪"。

    `days` 是"取最近多少行"而不是"最近多少天"：这个表**每天每人一行**，
    所以行数与天数同量级，按行取更简单也够用。
    """
    return list(
        session.execute(
            select(QuotaLedger)
            .where(QuotaLedger.kind == "day")
            .order_by(QuotaLedger.day.desc(), QuotaLedger.user_id)
            .limit(days)
        )
        .scalars()
        .all()
    )
