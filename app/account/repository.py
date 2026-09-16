"""账号领域的数据访问（ADR-0005：领域自己的表自己写）。

`users` / `user_tokens` / `invite_codes` / `quota_ledger` 只有这里碰 —— 别的领域
要"当前用户"就走 `service`，不自己去查 users 表。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy import update as sqlite_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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


def _refresh_quota_row(session: Session, user_id: int, day: str) -> None:
    """把当天那一行重新读一遍 —— **顺带刷新身份映射里的那个对象**。

    ⚠️ 下面两条语句的累加是在 **SQL 里**算的，而 ORM 的身份映射不会因此刷新已经
    加载过的对象：同一个会话里"先读过额度 → 扣点 → 再读"会拿到旧值（实测读到
    `units_used=6` 而库里是 12），而 `units_used_today()` 正是这么读的 ——
    于是页面说"还剩 26 点"、库里其实只剩 20（AGENTS.md §3.1 那类静默不一致）。
    `populate_existing=True` 是这道刷新唯一需要的开关。
    """
    session.get(QuotaLedger, (user_id, "day", day), populate_existing=True)


def add_usage(
    session: Session, user_id: int, *, day: str, units: int = 0, tokens: int = 0
) -> None:
    """累加当天的用量。**每日一行**，不做全量流水（决策 13 + Open-2）。

    ⚠️ 用 `ON CONFLICT … DO UPDATE SET x = x + :n`（**一条语句**）而不是
    "先查再改"：后者在同一用户并发两次时会把其中一次的累加**丢掉**
    （两个事务都在对方写库之前读到同一个旧值），而这一列是 token 账本
    ——「钱花在哪」不能少记（AGENTS.md §3.8）。主键 `(user_id, kind, day)`
    就是 ON CONFLICT 的目标，不需要额外索引。
    """
    stmt = (
        sqlite_insert(QuotaLedger)
        .values(
            user_id=user_id,
            kind="day",
            day=day,
            units_used=units,
            tokens_used=tokens,
            updated_at=now_iso(),
        )
        .on_conflict_do_update(
            index_elements=["user_id", "kind", "day"],
            set_={
                "units_used": QuotaLedger.units_used + units,
                "tokens_used": QuotaLedger.tokens_used + tokens,
                "updated_at": now_iso(),
            },
        )
    )
    session.execute(stmt)
    session.flush()
    _refresh_quota_row(session, user_id, day)


def try_spend_units(
    session: Session, user_id: int, *, day: str, units: int, daily_limit: int
) -> bool:
    """**原子地**扣额度点：够就扣并返回 True，不够就一点不动并返回 False。

    为什么必须是**一条语句**：原来的实现是「先 `quota_state()` 读余额 → 再
    `add_usage()` 累加」——两步之间没有锁。实测：6 路并发 `POST /interview/start`
    造出 6 场面试（36 点）而账本只记了 **12 点**，日上限被静默突破；另一路表现是
    撞 `PRIMARY KEY` 变成 500。判断"够不够"与"扣掉"必须在同一个语句里，
    否则并发下两者一定对不上。

    `WHERE units_used + :units <= :daily_limit` 是**条件更新**：条件不成立时
    这条 UPSERT 一行都不写，`rowcount` 为 0 —— 那就是"不够"。

    调用方保证 `0 <= units <= daily_limit`（`units` 是常量 `COST[mode]`，
    最大 6，而日上限是 32）：**插入**那条路没有条件可挂（新行的余额本来就是 0），
    所以一次要的比日上限还多这件事只能由服务层的常量校验挡。
    """
    stmt = (
        sqlite_insert(QuotaLedger)
        .values(
            user_id=user_id,
            kind="day",
            day=day,
            units_used=units,
            tokens_used=0,
            updated_at=now_iso(),
        )
        .on_conflict_do_update(
            index_elements=["user_id", "kind", "day"],
            set_={"units_used": QuotaLedger.units_used + units, "updated_at": now_iso()},
            where=(QuotaLedger.units_used + units) <= daily_limit,
        )
    )
    result = session.execute(stmt)
    session.flush()
    ok = bool(result.rowcount)
    _refresh_quota_row(session, user_id, day)
    return ok


def refund_units(
    session: Session, user_id: int, *, day: str, units: int
) -> int:
    """把**已经记账**的额度点退回去（决策 98）。返回实际退回的点数（0 = 一点没退）。

    与 `try_spend_units` 同一个形状：**一条语句 + 条件更新**，只是条件反了过来。
    这不是对称洁癖 —— "先读余额再减"在并发下与扣点那条路会互相覆盖（决策 90 的
    同一课，那次是日上限被静默突破）。

    `WHERE units_used >= :units` 是必要的守卫：`units_used` 是"今天已经花了多少"，
    退成负数会让 `remaining` 超过日上限，页面上就会出现 `35/32` 这种数字。
    条件不成立时一行都不写、返回 0（那一天没有可退的账）。

    只更新已存在的那一行（不 upsert）：返还针对的是**当初那次扣点**，那天的行一定在；
    行不在就说明这一天压根没扣过 —— 不该凭空造出一行 `-3`。
    """
    if units <= 0:
        return 0
    stmt = (
        sqlite_update(QuotaLedger)
        .where(
            QuotaLedger.user_id == user_id,
            QuotaLedger.kind == "day",
            QuotaLedger.day == day,
            QuotaLedger.units_used >= units,
        )
        .values(units_used=QuotaLedger.units_used - units, updated_at=now_iso())
    )
    result = session.execute(stmt)
    session.flush()
    _refresh_quota_row(session, user_id, day)
    return units if result.rowcount else 0


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
