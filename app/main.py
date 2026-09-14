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
from app.web import (
    admin_page,
    admin_users_page,
    auth_page,
    bank_page,
    feedback_page,
    home_page,
    interview_page,
    me_page,
    observability_page,
    report_page,
)
from app.web.templating import WEB_DIR, render


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
    app.include_router(observability_page.router)

    _install_error_handlers(app)

    return app


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
