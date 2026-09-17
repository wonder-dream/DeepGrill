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

import hashlib
import re
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(("html", "xml"), default=True),
    trim_blocks=True,
    lstrip_blocks=True,
    # ⚠️ **显式打开**，不靠默认值：改模板必须立刻生效。默认值（`auto_reload = not autoescape`）
    # 是 `False` —— 与这里的 `autoescape=True` 合起来正好把热更新**关掉**，而那是一个
    # 不会报错的失效形状：改了模板、刷新页面什么都没变（实测：改了 `base.html` 的
    # `?v=` 之后强刷也不生效，排查了一圈才发现"新模板根本没被读"）。
    # 生产上它是每个请求一次 mtime 检查的代价，换"改模板不用重启"，值。
    auto_reload=True,
)

#: `/static/<文件>?v=<内容哈希前 12 位>` 里的**旧版号**：`/static/xxx?v=3` / `?v=30` 这种。
#: 它靠人手加一，而"改了文件忘了加"不会报错 —— 浏览器照旧拿旧的那份。
_HAND_WRITTEN_ASSET_URL = re.compile(r"/static/([A-Za-z0-9_.-]+)\?v=[0-9A-Za-z.]+")


def asset(filename: str) -> str:
    """静态资源的 URL，**版号由内容算出来**（`?v=<sha256 前 12 位>`）。

    为什么不让模板手写 `?v=31`：静态资源带 `Cache-Control: public, max-age=3600`
    （`app/main.py::STATIC_MAX_AGE`），文件名里又没有内容指纹，于是"改了文件而忘了
    加版号"就等于**改动在浏览器里静默不生效**（实测踩过两次：一次是 `app.css` 的
    按钮居中、一次是 `app.css` 整个改版）。人手维护的计数器正好是最容易忘的那类东西，
    而这件事完全可以由机器做对。

    ⚠️ **刻意不缓存这个哈希**：第一版加了 `lru_cache`，结果造出一个自己咬自己的循环 ——
    进程里那份旧模板报出旧版号，旧版号对应浏览器缓存里那份旧 CSS，"文件已经改了"
    这件事就永远传不出去（实测：服务端一直下发旧版号，而磁盘上的内容早就是新的）。
    28KB 的 sha256 是微秒级，每个页面渲染多算一次，换"URL 永远等于磁盘上这一份"，值。

    它返回的已经是**完整 URL**（含 `/static/` 与版号），所以模板里直接引用这个值就行；
    `render()` 末尾还会把任何漏下来的手写 `/static/x?v=N` 重新按内容算一遍 —— 两道
    保险都不需要谁记得加一。
    """
    path = STATIC_DIR / filename
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"/static/{filename}?v={digest}"


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
        # `asset` 也和上面两个同理：静态资源 URL 是**每个页面**都要的，而"每个模板
        # 自己记得算版号"正是会漏的那类事（漏了不报错，只是浏览器拿旧文件）。
        "asset": asset,
        **(context or {}),
    }
    html = _env.get_template(name).render(request=request, **ctx)
    # 兜底：万一有手写的 `?v=3` 漏下来（比如新写的模板抄了老写法），这里按文件内容
    # 重算一遍。**先读**再写，所以 `?v=` 里的东西永远是内容哈希 —— 不依赖谁记得加一。
    if "?v=" in html:
        html = _HAND_WRITTEN_ASSET_URL.sub(lambda m: asset(m.group(1)), html)
    return HTMLResponse(html, status_code=status_code)
