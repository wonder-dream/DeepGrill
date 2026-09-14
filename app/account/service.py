"""账号领域的业务规则（ADR-0005：模块级函数，`db` 第一个参数）。

三件事：注册（要邀请码）、登录（发令牌）、以及**额度点的读与扣**。

额度这里有两条产品决定（决策 13），它们决定了函数形状：

· **单一额度点**：用户只理解一个数字（"今天还剩几场"），所以 `remaining_units()`
  返回的是余量而不是流水。
· **耗尽降级、不硬拒绝**：`spend_units()` 在余额不足时抛 `QuotaExhausted`，
  但**题库仍然可用** —— 那由题库领域自己保证（它根本不查额度）。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.account import repository
from app.db.models import InviteCode, QuotaLedger, User
from app.errors import Forbidden, InvalidInput, NotFound, QuotaExhausted
from app.security import hash_password, new_token, token_hash, verify_password

#: 每日额度点上限。MVP 先用一个能演示的值；真实系数要按成本重标定（§未决 9）。
DAILY_UNITS = 20

#: 各形态的扣减（决策 2 的额度点列）。
COST = {"interview": 6, "drill": 1, "browse": 0}


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 注册与登录
# ---------------------------------------------------------------------------
def register(
    session: Session, *, email: str, username: str, password: str, invite_code: str
) -> User:
    """邀请码注册（决策 6）。**不做邮箱验证码、不需要 SMTP。**

    邀请码在同一个事务里被标记已用 —— 所以"注册成功但码没被消耗"这种不一致
    不可能发生（这也是选"库表 + 事务"而不是"先查后用"的原因）。
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        raise InvalidInput("邮箱格式不对")
    if len(password) < 8:
        raise InvalidInput("口令至少 8 位")
    if repository.find_user_by_email(session, email) is not None:
        raise InvalidInput("这个邮箱已经注册过了")

    invite = repository.find_invite(session, invite_code.strip())
    if invite is None or not repository.invite_is_usable(invite):
        # 不区分"码不存在"与"码已用/过期"：那会把"这个码存在"泄露给猜码的人
        raise Forbidden("邀请码无效或已被使用")

    user = repository.create_user(
        session,
        email=email,
        username=username.strip() or email.split("@")[0],
        password_hash=hash_password(password),
    )
    repository.mark_invite_used(session, invite, user.id)
    return user


def login(session: Session, *, email: str, password: str) -> str:
    """校验口令并签发令牌，返回**明文令牌**（只该进 cookie）。

    失败一律同一句话、同一种异常：区分"邮箱不存在"与"口令不对"等于给出一个
    账号枚举接口。
    """
    user = repository.find_user_by_email(session, email.strip().lower())
    if user is None or not verify_password(password, user.password_hash):
        raise Forbidden("邮箱或口令不正确")
    token = new_token()
    repository.issue_token(session, user.id, token_hash(token))
    return token


def logout(session: Session, token: str) -> None:
    repository.revoke_token(session, token_hash(token))


def verify_login(session: Session, *, email: str, password: str) -> bool:
    """只校验口令、**不签发令牌**。

    注销（`/me/delete`）要重输口令才允许执行 —— 它不能复用 `login()`，因为那会
    顺手多签一个令牌，而这次请求根本不需要新会话。
    """
    user = repository.find_user_by_email(session, email.strip().lower())
    return user is not None and verify_password(password, user.password_hash)


def user_from_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    return repository.user_for_token(session, token_hash(token))


# ---------------------------------------------------------------------------
# 额度点
# ---------------------------------------------------------------------------
def units_used_today(session: Session, user_id: int) -> int:
    row = repository.quota_row(session, user_id, today())
    return row.units_used if row else 0


def remaining_units(session: Session, user_id: int) -> int:
    return max(0, DAILY_UNITS - units_used_today(session, user_id))


def spend_units(
    session: Session, user_id: int, mode: str, *, tokens: int = 0
) -> int:
    """按动作扣额度点，返回**本次扣了多少**。

    ⚠️ **它不负责"扣了之后能不能开始"** —— 那是编排层的事（面试域创建时扣、
    失败时不重复扣）。这里只做"记账 + 不够就拒绝"。
    """
    cost = COST.get(mode)
    if cost is None:
        raise InvalidInput(f"未知的形态：{mode}")
    if cost == 0:
        return 0
    if remaining_units(session, user_id) < cost:
        raise QuotaExhausted()
    repository.add_usage(session, user_id, day=today(), units=cost, tokens=tokens)
    return cost


def record_tokens(session: Session, user_id: int, tokens: int) -> None:
    """把真实 token 开销记进第二道安全网（决策 14）。**不扣额度点。**"""
    if tokens > 0:
        repository.add_usage(session, user_id, day=today(), tokens=tokens)


# ---------------------------------------------------------------------------
# 管理员操作（决策 6 / 7）
# ---------------------------------------------------------------------------
def new_invite(
    session: Session, *, created_by: int, days_valid: int | None = None
) -> InviteCode:
    """生成一张邀请码（决策 6：后台生成、可设有效期、可作废）。

    码本身用 `secrets` 生成而不是自增序号：猜码要付出与暴力破解同等的代价。
    **有效期是"天数"而不是时间戳** —— 后台界面给的是"7 天后过期"，
    让调用方自己算时间戳只会多一处算错的机会（时区、格式）。
    """
    if days_valid is not None and days_valid <= 0:
        raise InvalidInput("有效期必须是正数天（或留空表示永久有效）")

    expires_at = None
    if days_valid is not None:
        expires_at = (datetime.now(UTC) + timedelta(days=days_valid)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    for _ in range(5):  # 撞码概率极低，但撞了就重试，不抛给用户
        code = "DG-" + secrets.token_hex(6).upper()
        if repository.find_invite(session, code) is None:
            break
    else:
        raise InvalidInput("生成邀请码失败（连续撞码）—— 请重试")

    invite = repository.create_invite(session, code, expires_at=expires_at)
    invite.created_by = created_by
    session.flush()
    return invite


def revoke_invite(session: Session, code: str) -> None:
    """作废一张未使用的码。"""
    try:
        ok = repository.delete_invite(session, code)
    except ValueError as e:
        raise InvalidInput(str(e)) from e
    if not ok:
        raise NotFound("这张邀请码不存在")


def admin_reset_password(session: Session, *, user_id: int, new_password: str) -> int:
    """管理员重置口令（决策 7：忘密码 = 管理员后台重置，不做邮箱找回）。

    返回被撤销的令牌数 —— **重置口令必须同时踢下线**，否则旧会话还能用，
    "口令丢了"就没被真正解决。
    """
    if len(new_password) < 8:
        raise InvalidInput("口令至少 8 位")
    user = repository.get_user(session, user_id)
    if user is None:
        raise NotFound("这个账号不存在")
    repository.set_password(session, user, hash_password(new_password))
    return repository.revoke_all_tokens(session, user_id)


def usage_summary(session: Session, *, days: int = 30) -> list[QuotaLedger]:
    """最近的额度记录（后台看"钱花在哪"，决策 14）。"""
    return repository.recent_usage(session, days=days)
