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


def render(request: Request, name: str, context: dict[str, Any] | None = None) -> HTMLResponse:
    """渲染一个整页。`request` 传进模板是为了取 URL 与服务端状态（ADR-0004）。"""
    template = _env.get_template(name)
    return HTMLResponse(template.render(request=request, **(context or {})))
