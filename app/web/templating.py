"""模板环境（web 专属 —— ADR-0010：模板是"形状"，所以住 web/ 而不是基础设施）。

Jinja2 的自动转义默认关闭，而 ADR-0004 明确要求"必须引入模板引擎与它的 XSS
纪律"（v1 在复习卷那块用 bleach 白名单消毒）。这里的做法是三条：

① `autoescape=True` —— 把转义变成**默认**，而不是每处手写 `| e`；
② 不注册任何"标为安全"的过滤器（`|safe` 只许在没有用户内容的字面量上用）；
③ 模板里不许拼 HTML 字符串 —— 想复用就写 `{% block %}` / `{% include %}`。

一个 `Environment` 是进程级共享的（模板编译有缓存），所以它是**无状态**的：
不往里放用户数据、不往里放请求上下文（那些走 `render(request, name, ctx)`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(("html", "xml"), default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """渲染一个整页。`request` 传进模板是为了取 URL 与服务端状态（ADR-0004）。

    **当前用户在这里统一注入**，不让每个页面各自传：导航栏是每个页面都有的
    （base.html），而"每个调用点都记得传 user"正是会漏的那类事 —— 漏了的页面
    导航栏会静默显示成"未登录"。取不到（没接账号域、或匿名）就是 None。

    默认值来自 `request.state.user`（由 `deps.get_current_user` 挂上）。第一版
    只写了 `{"user": None}` 并靠页面自己传，于是**首页漏传了** —— 已登录用户看到
    的仍是"登录"链接，而且页面上没有任何东西会报错。页面显式传的 `user` 仍然优先
    （有些页面拿到的是"必须是 owner"的那个对象，与 `request.state` 里那个是同一个）。

    `rail` 同理走 `request.state.rail`（决策 96）：侧边栏的「继续面试」与状态栏的
    今日额度点**每个页面都渲染**，所以它也是"漏一个页面就静默少一块"的那类东西。
    它同样是快照（见 `deps.RailState`），不是 ORM 对象 —— 错误页读它时会话早已关闭。

    `status_code` 必须能透传：错误页配 200 是**静默的错**（缓存、爬虫、前端分支
    全都会判断错），而它不会有人肉眼发现。实测踩过两次 —— 领域层的 `NotFound`
    与 `Forbidden` 都曾渲染出 200 的错误页。
    """
    ctx: dict[str, Any] = {
        "user": getattr(request.state, "user", None),
        "rail": getattr(request.state, "rail", None),
        **(context or {}),
    }
    return HTMLResponse(
        _env.get_template(name).render(request=request, **ctx), status_code=status_code
    )
