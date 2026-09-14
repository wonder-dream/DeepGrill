"""把配置、引擎与会话依赖装配到一起（组装用，不含业务）。

**为什么依赖是模块级函数而不是在 `create_app` 里现造**：FastAPI 的 `Depends`
需要在**路由函数定义时**就能拿到一个可调用对象，而路由模块在 import 时就绑定好了。
所以这里用懒单例（`lru_cache`）：同一库路径只开一个连接池。测试用
`app.dependency_overrides` 换掉它 —— 比"每个测试改环境变量再重新 import"干净，
也不会让 app 与测试互相污染。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.account import service as account_service
from app.config import Settings
from app.db import create_db_engine, create_session_factory, make_session_dependency
from app.db.models import User
from app.errors import Forbidden, TooManyRequests
from app.llm import LLMCallError, LLMClient
from app.llm.embeddings import (
    APIEmbeddings,
    Embeddings,
    FakeEmbeddings,
    NoProviderEmbeddings,
)
from app.llm.stt import APISTT, FakeSTT, NoProviderSTT, SpeechToText


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


@dataclass(frozen=True)
class CurrentUser:
    """导航栏与页面默认值要用的那几项 —— **一份在会话关闭后仍然读得到的快照**。

    为什么不能直接把 ORM 的 `User` 挂到 `request.state` 上：请求失败时依赖的
    `finally` 会先 `rollback()` + `close()`，而 **rollback 会让该会话里所有对象
    的已加载属性失效**（`expire_on_commit=False` 管不了 rollback）。异常处理器
    随后拿 `request.state.user` 渲染错误页 —— 那一刻读 `user.username` 就是
    `DetachedInstanceError`，错误页自己 500。实测正是这样炸的。

    所以这里是**值的拷贝**，不是"另一个用户模型"：它由 `get_current_user` 唯一
    产生，只服务模板的默认值；页面自己的业务判断仍然用依赖返回的那个 `User`。
    """

    id: int
    username: str
    role: str
    email: str


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

    ⚠️ 它**顺手把 `CurrentUser` 快照挂到 `request.state.user`**，让 `render()`
    有个安全的默认值。原来只靠"每个页面记得把 `user` 传进 context"——实测漏了一个
    （首页），于是首页主按钮对**已登录用户**也一直显示成未登录，而且不报错。
    FastAPI 对同一请求里的同一个依赖只解析一次，所以这不会多查一次库。
    """
    token = request.cookies.get(SESSION_COOKIE)
    user = account_service.user_from_token(session, token) if token else None
    request.state.user = (
        CurrentUser(id=user.id, username=user.username, role=user.role, email=user.email)
        if user is not None
        else None
    )
    return user


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


def get_stt(settings: Settings = Depends(get_settings)) -> SpeechToText:
    """语音转写（决策 32）。**没有供应商时返回一个"明确失败"的实现**。

    与 `get_llm` 同一个模式：应用照常起得来，而一旦真的要用它就会拿到一条清楚的
    错误 —— 面试页把它显示在页面上并让用户改用打字，而不是拿一段假转写去骗判分。
    """
    if settings.stt_provider == "api":
        # key / base_url 都允许回落到 LLM 那一组：同一家服务商时少配两个变量；
        # 模型名**必须**显式给 —— 转写模型与对话模型不通用，猜不出来
        return APISTT(
            api_key=settings.stt_api_key or settings.llm_api_key,
            base_url=settings.stt_base_url or settings.llm_base_url,
            model=settings.stt_model,
        )
    if settings.stt_provider == "fake":
        return FakeSTT()
    return NoProviderSTT()


def get_embeddings(settings: Settings = Depends(get_settings)) -> Embeddings:
    """嵌入（ADR-0008：**走 API，不跑本地模型**）。同样"没配就明确失败"。

    它不挂在 HTTP 请求上（聚类是离线管道的事），但装配管道需要一个统一的构造点 ——
    与 `get_llm` / `get_stt` 保持一致：接真实供应商时只改这一处。
    """
    if settings.embedding_provider == "api":
        return APIEmbeddings(
            api_key=settings.embedding_api_key or settings.llm_api_key,
            base_url=settings.embedding_base_url or settings.llm_base_url,
            model=settings.embedding_model,
        )
    if settings.embedding_provider == "fake":
        return FakeEmbeddings()
    return NoProviderEmbeddings()


def rate_limit_interviewer(
    request: Request, user: User | None = Depends(get_current_user)
) -> None:
    """按**用户**限面试官动作（决策 66 的成本型那一档）。

    它是依赖而不是中间件，因为用户身份只有到这里才知道（中间件里查库会踩 §3.3）。
    占位记录追加到 `request.scope["ratelimit"]`，由中间件在响应之后统一 settle ——
    这样"4xx 释放"只有一处实现。

    超限时抛 `TooManyRequests`（429），交给异常处理器渲染 —— 于是**匿名**
    （`user is None`）不受这一档限制：他们没有额度点，也就没有成本可言，而按 IP
    的那一层照旧管着他们。
    """
    limiters = getattr(request.app.state, "ratelimiters", None)
    if limiters is None or not limiters.enabled or user is None:
        return
    key = f"user:{user.id}"
    decision = limiters.llm.reserve(key)
    if not decision.allowed:
        raise TooManyRequests(
            f"面试官动作发得太频繁了，请等 {max(1, decision.retry_after)} 秒再试",
            retry_after=decision.retry_after,
        )
    request.scope.setdefault("ratelimit", []).append((limiters.llm, key))


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
