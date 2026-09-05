# 方案设计：登录态从 localStorage 迁到 HttpOnly Cookie

> 状态：已落盘并执行（代码/测试/文档已同步）
> 关联：前端 localStorage 存原始 token 在 XSS 面前有泄露风险；本方案通过 HttpOnly Cookie 将原始 token 移出 JS 可读范围。

## 1. 背景与目标

### 现状
- 使用不透明随机 token；
- 服务端只存 `SHA-256(token)`，token 可撤销、30 天过期；
- 前端把原始 token 存在 `localStorage`，每次请求手动加 `Authorization: Bearer ...`；
- 已通过后端 HTML 白名单消毒、输入校验等方式降低 XSS 面；
- 已知残留风险：一旦发生任何 XSS，攻击者可读取 localStorage 中的长期 token，实现持久会话劫持。

### 目标
1. 让 **XSS 无法直接读取原始 token**；
2. 保留现有可撤销、过期机制，不改变 token 语义；
3. 不引入 JWT / access+refresh token 的复杂度；
4. 兼容现有测试、脚本和旧客户端的 Bearer 调用方式；
5. 控制 CSRF 风险。

### 非目标
- 不引入短期 access token + refresh token；
- 不重做认证协议；
- 不在本轮解决 HTTPS 本身，但 Cookie 设计预留 `Secure` 开关。

## 2. 方案选型

| 方案 | 能否消除 XSS 偷长期 token | 复杂度 | 结论 |
|---|---|---|---|
| 维持 localStorage | 否 | 低 | 不采用 |
| token 只放 JS 内存 | 否（当前页面可被偷） | 低 | 不采用 |
| **HttpOnly Cookie 存原 token** | **是** | 中 | **采用** |
| access+refresh token | 只缩小窗口，仍需 Cookie | 高 | 不采用 |
| Service Worker 持 token | 页面 JS 读不到，但可被指挥 | 高 | 不采用 |

**选择理由**
- 本项目前后端同源，Cookie 无跨域障碍；
- token 已是服务端可撤销、有过期时间，Cookie 只改变“携带方式”；
- 相比 access/refresh token，改动面小、语义不变；
- CSRF 可通过 `SameSite=Lax` + 自定义请求头控制。

## 3. 总体架构

改造后：

```
浏览器
  └─ 登录/注册成功
       └─ 服务端 Set-Cookie: session_token=<token>; HttpOnly; SameSite=Lax; Path=/; Max-Age=30d
  └─ 后续 API 请求
       └─ 浏览器自动携带 Cookie
  └─ 登出
       └─ 服务端撤销 UserToken + 删除 Cookie
```

页面 JS 不再：
- 读取 localStorage 中的 token；
- 维护 `state.token`；
- 手动添加 `Authorization: Bearer ...`。

服务端仍保留：
- `Authorization: Bearer ...` 解析路径（兼容测试/脚本/存量调用）。

## 4. 后端设计

### 4.1 Cookie 定义

| 项 | 值 | 说明 |
|---|---|---|
| Cookie 名 | `session_token` | 建议常量 `SESSION_COOKIE` |
| Value | 现有不透明 token | 不额外做一层封装 |
| HttpOnly | `true` | JS 不可读 |
| SameSite | `Lax` | 阻止跨站 POST 自动携带 |
| Secure | `os.environ.get("COOKIE_SECURE") == "1"` | 生产 HTTPS 开启 |
| Path | `/` | 全站 API 可用 |
| Max-Age | `TOKEN_TTL_DAYS * 24 * 3600` | 与现有 30 天对齐 |

### 4.2 文件改动：`app/web/routes.py`

#### 新增辅助函数

```python
SESSION_COOKIE = "session_token"

def _extract_token(authorization: str = "", session_token: str = "") -> str:
    return authorization.removeprefix("Bearer ").strip() or session_token.strip()

def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=TOKEN_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE") == "1",
        path="/",
    )
```

#### 修改认证依赖

```python
def require_user(
    authorization: str = Header(default=""),
    session_token: str = Cookie(default=""),
) -> User:
    token = _extract_token(authorization, session_token)
    user = user_from_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user
```

#### 修改登录/注册

登录、注册接口增加 `response: Response` 参数，成功创建 token 后调用 `_set_auth_cookie(response, token)`，响应体不再返回原始 token。

#### 修改登出

登出撤销 `_extract_token(authorization, session_token)` 对应的 token，并删除 `session_token` Cookie。

#### 导入接口

若导入后签发 `new_token`，后端同样调用 `_set_auth_cookie(response, new_token)`；前端不再消费 `resp.new_token`。

### 4.3 CSRF 防护

Cookie 会被浏览器自动携带，因此写操作需要防 CSRF。

设计选择：
- `SameSite=Lax` 已能挡住大部分跨站 POST；
- 再加一层“自定义头校验”作为纵深防御：非 `/api/auth/*` 的写操作若未带 `Authorization: Bearer`，必须带 `X-Requested-With: fetch`。

## 5. 前端设计

### 5.1 `app/web/static/app.js`

1. `state.token` 不再从 localStorage 初始化，置为 `null`；
2. `api()` 删除手动 `Authorization`，统一加 `X-Requested-With: fetch`；
3. 401 分支不再 `localStorage.removeItem("token")`，只做状态清理并跳登录；
4. 登录/注册成功后不保存 token，`state.user = body.user`；
5. 登出只调用接口并清理状态；
6. `initAuth()` 不再检查本地 token，直接请求 `/api/auth/me`；
7. 上传 XHR、导出 fetch 去掉 `Authorization`，补 `X-Requested-With`；
8. 导入不再写 localStorage。

### 5.2 `app/web/static/reviewer.js`

同样：
- `state.token` 置为 `null`；
- `api()` 去 Authorization、加 `X-Requested-With`；
- `init()` 不再检查本地 token，401 跳回主页。

## 6. 兼容与迁移

- 服务端 `require_user` 同时支持 Bearer Header 与 Cookie，测试/脚本不受影响；
- 部署后存量 localStorage token 不再被新前端使用，用户需重新登录一次（可接受）；
- 新增 `COOKIE_SECURE=1` 环境变量控制生产 Cookie `Secure`。

## 7. 测试设计

新增后端用例：
1. 登录/注册响应含 `Set-Cookie: session_token=...`，响应体不再含 token；
2. 不带 Authorization、只带 Cookie 的请求可通过鉴权；
3. 登出后 Cookie 被清除且 token 被撤销；
4. 写接口在 Cookie 认证且无自定义头时返回 403（CSRF）；
5. Cookie 带 `HttpOnly`、`SameSite=Lax`。

前端检查：
- `node --check app/web/static/app.js`
- `node --check app/web/static/reviewer.js`

## 8. 文档更新

- `docs/多用户部署方案.md`：登录态说明改为 HttpOnly Cookie；
- `.env.example`：增加 `COOKIE_SECURE=1`；
- `docs/服务器运维清单.md`：生产 HTTPS 开启 `COOKIE_SECURE`。

## 9. 风险与备注

| 风险 | 应对 |
|---|---|
| CSRF | `SameSite=Lax` + `X-Requested-With` 自定义头 |
| 存量用户需重新登录 | 可接受，小规模系统可提前告知 |
| 非 HTTPS 下 Cookie 明文传输 | 保留 Bearer/兼容模式，生产开启 `COOKIE_SECURE` |
| 测试大量依赖 Bearer | `require_user` 双通道兼容，不改测试主体 |
