"""邮箱验证码（注册验证）：内存态 + SMTP 发送（标准库 smtplib，零新依赖）。

- 6 位数字码，5 分钟有效；同邮箱 60s 限频；连续 5 次错误作废；用后即删
- 进程内存态：单 worker 部署下安全（与 _inflight 同思路）；重启即失效可重发
- 发送函数可注入（测试用 Fake，不触网）
"""
import logging
import re
import secrets
import smtplib
import threading
import time
from email.header import Header
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")

_CODE_TTL = 300  # 验证码有效期（秒）
_SEND_INTERVAL = 60  # 同邮箱发送间隔（秒）
_MAX_ATTEMPTS = 5  # 验证码最大错误次数（防爆破）

_codes: dict[str, dict] = {}
_codes_lock = threading.Lock()


def validate_email(email: str) -> str | None:
    """返回错误信息或 None（合法）。"""
    email = (email or "").strip()
    if len(email) > 64 or not EMAIL_RE.match(email):
        return "邮箱格式不正确"
    return None


def _new_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def _cleanup_locked(now: float) -> None:
    for key in [k for k, v in _codes.items() if now - v["ts"] > _CODE_TTL]:
        _codes.pop(key, None)


def can_send(email: str) -> bool:
    """发送限频：同邮箱 60s 内已发过则拒绝。"""
    with _codes_lock:
        _cleanup_locked(time.time())
        last = _codes.get(email, {}).get("last_sent", 0)
        return time.time() - last >= _SEND_INTERVAL


def issue_code(email: str, send_fn=None) -> None:
    """生成验证码并发送（send_fn(email, code) 可注入；默认 SMTP）。

    发送失败也占 60s 冷却（last_sent 先落库再发送）：否则 SMTP 报错时同一邮箱可被
    无限次触发发送尝试，限频形同虚设。抛异常时不留验证码（调用方提示发送失败）。
    """
    send_fn = send_fn or send_smtp_code
    now = time.time()
    code = _new_code()
    with _codes_lock:
        _cleanup_locked(now)
        # 先写 last_sent 占冷却窗口；code 留空，发送成功后再补
        _codes[email] = {"code": "", "ts": now, "last_sent": now, "attempts": 0}
    try:
        send_fn(email, code)
    except Exception:
        raise  # 冷却已占（last_sent 已写），验证码不保留
    with _codes_lock:
        entry = _codes.get(email)
        if entry is not None:
            entry["code"] = code
        else:  # 冷却窗口内被清理：补写一条完整记录
            _codes[email] = {"code": code, "ts": now, "last_sent": now, "attempts": 0}


def verify_code(email: str, code: str) -> bool:
    """校验验证码：正确且未过期则消费（用后即删）；错误累计 5 次作废。"""
    with _codes_lock:
        _cleanup_locked(time.time())
        entry = _codes.get(email)
        if entry is None:
            return False
        if entry["code"] != code:
            entry["attempts"] += 1
            if entry["attempts"] >= _MAX_ATTEMPTS:
                _codes.pop(email, None)
            return False
        _codes.pop(email, None)
        return True


def send_smtp_code(email: str, code: str) -> None:
    """通过 SMTP 发送验证码邮件（163/QQ 授权码）。

    配置：SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS（.env），未配置抛 RuntimeError（调用方转 503）。
    """
    import os

    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    if not host or not user or not password:
        raise RuntimeError("SMTP 未配置（SMTP_HOST/SMTP_USER/SMTP_PASS）")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = MIMEText(
        f"【DeepGrill】你的注册验证码是 {code}，5 分钟内有效。若非本人操作请忽略。",
        "plain",
        "utf-8",
    )
    msg["Subject"] = Header("DeepGrill 注册验证码", "utf-8")
    msg["From"] = user
    msg["To"] = email
    with smtplib.SMTP_SSL(host, port, timeout=15) as server:
        server.login(user, password)
        server.sendmail(user, [email], msg.as_string())
    logger.info("验证码邮件已发送至 %s", email)
