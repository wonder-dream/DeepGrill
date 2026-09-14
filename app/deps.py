"""把配置、引擎与会话依赖装配到一起（组装用，不含业务）。

**为什么依赖是模块级函数而不是在 `create_app` 里现造**：FastAPI 的 `Depends`
需要在**路由函数定义时**就能拿到一个可调用对象，而路由模块在 import 时就绑定好了。
所以这里用懒单例（`lru_cache`）：同一库路径只开一个连接池。测试用
`app.dependency_overrides` 换掉它 —— 比"每个测试改环境变量再重新 import"干净，
也不会让 app 与测试互相污染。
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.account import service as account_service
from app.config import Settings
from app.db import create_db_engine, create_session_factory, make_session_dependency
from app.db.models import User
from app.errors import Forbidden
from app.llm import LLMCallError, LLMClient


@lru_cache(maxsize=8)
def _engine_for(path_str: str) -> Engine:
    return create_db_engine(Path(path_str))


def get_settings() -> Settings:
    return Settings()


def get_engine(settings: Settings = Depends(get_settings)) -> Engine:
    return _engine_for(str(settings.resolved_database_path()))


def get_session(engine: Engine = Depends(get_engine)) -> Iterator[Session]:
    """每请求一个会话。事务边界在 `make_session_dependency` 里（成功提交、失败回滚）。"""
    factory = create_session_factory(engine)
    yield from make_session_dependency(factory)()


#: 会话 cookie 的名字。它是"令牌明文"的唯一住处（库里只存 sha256）。
SESSION_COOKIE = "dg_session"


def get_current_user(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    """当前登录用户，或 None（匿名）。

    **它不抛异常** —— 匿名是合法状态（产品面向公众，决策 6：匿名能看全部公共题）。
    "必须登录"由需要它的页面自己决定（用 `require_user`），而不是由这一层替所有
    页面决定。

    为什么住在这里而不是 `account/service.py`：它要读 **cookie**（HTTP 那一层的事），
    而领域层不知道 cookie 是什么。领域只提供 `service.user_from_token(session, 明文)`
    —— 依赖注入这一层负责把 cookie 里的东西递进去。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return account_service.user_from_token(session, token)


def require_user(user: User | None = Depends(get_current_user)) -> User:
    """需要登录的页面用它。未登录 → 403（由 `AppError` 带状态码，见 `app/errors.py`）。"""
    if user is None:
        raise Forbidden("请先登录")
    return user


def get_llm(settings: Settings = Depends(get_settings)) -> object:
    """LLM 客户端（每请求一个）。**没有 key 时返回一个"明确失败"的替身**。

    这样建这个客户端本身不会让应用起不来（本机开发常常没有 key），而一旦真的
    要用它就会拿到一条清楚的错误 —— 而不是悄悄用假数据糊过去
    （AGENTS.md §3.1：降级可以，静默不行）。
    """
    if not settings.llm_api_key:
        return _MissingKeyLLM()
    return LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.model_interviewer,
    )


class _MissingKeyLLM:
    """没配 key 时的替身：**任何调用都抛 LLMCallError**。

    它让"判定失败"走既有的降级路径（会话不中断、这一轮记为未涉及、页面提示），
    所以没配 key 的机器上整条链仍然能跑通并被人工看到问题。
    """

    def chat(self, messages, **kwargs):
        raise LLMCallError("没有配置 DEEPGRILL_LLM_API_KEY —— 无法调用模型")

    def chat_json(self, messages, **kwargs):
        raise LLMCallError("没有配置 DEEPGRILL_LLM_API_KEY —— 无法调用模型")

    def close(self) -> None:
        pass
