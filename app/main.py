"""组装根：建 app、挂路由、启动期只做"读配置 + 校验"（ADR-0010）。

**启动期什么都不做**是有意的（AGENTS.md §3.7 / ADR-0010）：迁移是显式的一步
（`python -m migrations.run`），prompt 是平级目录里的数据。启动路径碰不到它们 ——
这是"结构上够不着"，不是"记得别调用"。

`create_app(settings)` 收一个配置对象而不是读全局：测试因此能在不改环境变量的
前提下造出指向临时库的 app（见 `app/test_main.py`）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.db.startup import assert_database_is_ready
from app.deps import get_settings
from app.errors import AppError, NotFound
from app.ratelimit import (
    RateLimiters,
    client_ip,
    settle_on_response,
    too_many,
)
from app.web import (
    admin_page,
    admin_users_page,
    auth_page,
    bank_page,
    favorites_page,
    feedback_page,
    home_page,
    interview_page,
    me_page,
    observability_page,
    quality_page,
    report_page,
)
from app.web.templating import WEB_DIR, render

#: 限流**不覆盖**的路径。探针不该被限流（它要能一直回答"进程还活着吗"），
#: 静态资源也不该（一个页面会拉好几个，限流会把页面本身弄坏）。
RATELIMIT_EXEMPT_PREFIXES = ("/static/",)
RATELIMIT_EXEMPT_PATHS = ("/healthz",)
#: 登录 / 注册用更严的那一档（按 IP）—— 撞密码是这里唯一的现实攻击面。
AUTH_PATHS = ("/login", "/register")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    # 启动期只做"读配置 + 校验"（ADR-0010）—— **不建表、不迁移**（AGENTS.md §3.7），
    # 但**要拒绝带着占位口令的库**（决策 58）。这一条与"什么都不做"不冲突：
    # 它是检查，不是副作用。
    assert_database_is_ready(
        settings.resolved_database_path(), require_secure=settings.require_secure_db
    )

    app = FastAPI(
        title="DeepGrill",
        docs_url=None,      # 生产硬化：文档端点默认关闭（v1 的教训，行为规格 §9）
        redoc_url=None,
        openapi_url=None,
    )
    # 页面装配要用到的进程级状态。放 `app.state` 而不是模块全局：
    # 同一进程里可以存在多个 app（测试就是这么用的），模块全局会互相串。
    app.state.settings = settings

    # ⚠️ **必须覆盖这个依赖，不能只设 app.state**：`get_settings()` 会去读环境变量
    # 造一个新的 Settings，于是 `create_app(settings=…)` 传进来的配置在依赖链里
    # **静默失效**（实测：测试传临时库，页面读的是仓库里那个真库，而且不报错）。
    # 覆盖之后，所有 `Depends(get_settings)` 都拿到同一个对象。
    app.dependency_overrides[get_settings] = lambda: settings

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    app.include_router(home_page.router)
    app.include_router(bank_page.router)
    app.include_router(auth_page.router)
    app.include_router(me_page.router)
    app.include_router(interview_page.router)
    app.include_router(report_page.router)
    app.include_router(admin_page.router)
    app.include_router(admin_users_page.router)
    app.include_router(feedback_page.router)
    app.include_router(favorites_page.router)
    app.include_router(observability_page.router)
    app.include_router(quality_page.router)

    _install_rate_limit(app, settings)
    _install_error_handlers(app)

    return app


def _install_rate_limit(app: FastAPI, settings: Settings) -> None:
    """按 IP 的那一层限流（决策 66）。**按用户**的那一层在 `deps.rate_limit_interviewer`。

    ## 为什么是两个地方

    中间件跑在路由之前，那时**还不知道请求是谁**（要知道就得在这里查库 —— 而
    AGENTS.md §3.3 禁止在 async 上下文里做同步 DB 查询，中间件正是 async 上下文）。
    所以：

    · 中间件按 **IP** 限：它不需要知道身份，而且它要覆盖**每一个**请求
    · 依赖按 **用户** 限：它跑在依赖解析里（那里已经有会话与用户），只挂在
      **面试官动作**那几个端点上 —— 那才是花钱的地方

    两层都把"占位记录"追加到同一个 `request.scope["ratelimit"]`，由中间件在响应
    之后统一 settle（它能看到状态码）。
    """
    limiters = RateLimiters.from_settings(settings)
    # 挂在 app.state 上而不是模块全局：同一进程里有多个 app 时（测试就是这么用的），
    # 模块全局会让它们互相限流。
    app.state.ratelimiters = limiters

    @app.middleware("http")
    async def _ratelimit(request: Request, call_next):
        if not limiters.enabled or _ratelimit_exempt(request.url.path):
            return await call_next(request)

        reservations: list[tuple[object, str]] = []
        request.scope["ratelimit"] = reservations

        ip_key = f"ip:{client_ip(request, trust_proxy=settings.trust_proxy_headers)}"
        decision = limiters.requests.reserve(ip_key)
        if not decision.allowed:
            # 这一层不释放（防滥用型）—— 被拦的请求本身就该计数
            return _too_many_response(request, decision)
        reservations.append((limiters.requests, ip_key))

        if request.url.path in AUTH_PATHS:
            auth_key = f"ip-auth:{client_ip(request, trust_proxy=settings.trust_proxy_headers)}"
            auth_decision = limiters.auth.reserve(auth_key)
            if not auth_decision.allowed:
                return _too_many_response(request, auth_decision)
            reservations.append((limiters.auth, auth_key))

        response = await call_next(request)
        # settle：只有**成本型**限流器会因为 4xx 把格子还回去（v1 的 reserve/settle）。
        # 429 本身不释放，否则限流器会把自己擦掉（见 `settle_on_response`）。
        settle_on_response(reservations, response.status_code)
        return response


def _ratelimit_exempt(path: str) -> bool:
    return path in RATELIMIT_EXEMPT_PATHS or path.startswith(RATELIMIT_EXEMPT_PREFIXES)


def _too_many_response(request: Request, decision) -> object:
    """429。HTML 路由给一页能读的东西，其余给 JSON —— 两者都带上 `Retry-After`。"""
    wait = max(1, decision.retry_after)
    accepts_html = "text/html" in (request.headers.get("accept") or "")
    if accepts_html:
        response = render(
            request,
            "error.html",
            {"message": f"请求太频繁了，请等 {wait} 秒再试。"},
            status_code=429,
        )
    else:
        response = JSONResponse(too_many(decision), status_code=429)
    response.headers["Retry-After"] = str(wait)
    return response


def _install_error_handlers(app: FastAPI) -> None:
    """异常 → 用户可见的响应。**对外不回显内部细节**（v1 的刻意选择）。

    ⚠️ 这里不是"吞异常"：`AppError` 是**预期内**的失败，越出它的一律交给
    FastAPI 的默认 500（真实栈进日志）。AGENTS.md §3.1 要的是"降级必须同时
    产出用户可见状态与可查询记录" —— 前者在这里，后者在日志/`task_logs`。

    **状态码由异常自己带**（`AppError` 继承 `HTTPException`），处理器只渲染页面 ——
    第一版让处理器挑状态码，结果领域层的 `NotFound` 返回了 200，测试当场抓到。
    """

    @app.exception_handler(AppError)
    def _app_error(request: Request, exc: AppError) -> object:
        # 用公开的 render()，不碰 templating 的内部环境 —— 一旦有人把"错误页"
        # 做成需要自己拼 HTML 的东西，XSS 纪律（ADR-0004）就从这里破口。
        # ⚠️ `status_code` 必须跟着异常走：第一版漏了它，于是 `Forbidden`/`NotFound`
        # 都渲染出 **200 的错误页**（静默的错，没人会肉眼发现）。
        return render(
            request, "error.html", {"message": exc.message}, status_code=exc.status_code
        )

    @app.exception_handler(404)
    def _not_found(request: Request, exc: object) -> JSONResponse:
        del request, exc
        return JSONResponse({"error": "not found"}, status_code=404)


app = create_app()
