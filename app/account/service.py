"""账号领域的业务规则（ADR-0005：模块级函数，`db` 第一个参数）。

三件事：注册（要邀请码）、登录（发令牌）、以及**额度点的读与扣**。

额度这里有两条产品决定（决策 13），它们决定了函数形状：

· **单一额度点**：用户只理解一个数字（"今天还剩几场"），所以 `remaining_units()`
  返回的是余量而不是流水。
· **耗尽降级、不硬拒绝**：`spend_units()` 在余额不足时抛 `QuotaExhausted`，
  但**题库仍然可用** —— 那由题库领域自己保证（它根本不查额度）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.account import repository
from app.db.models import User
from app.errors import Forbidden, InvalidInput, QuotaExhausted
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
