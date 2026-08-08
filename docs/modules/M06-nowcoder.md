# M6 牛客爬虫模块

> 路径：`app/crawler/nowcoder.py` ｜ 规模：~250 行 ｜ 依赖：httpx
> 更新日期：2026-08-08（改版适配，源已恢复）

## 1. 职责

拉取牛客面经列表（分页，正文随列表返回）→ 清洗 → 入库；处理 cookie 鉴权、限速、重试与登录态失效。

## 2. 接口

```python
def collect(config: NowcoderConfig) -> list[Source]
    """入口：本日增量采集，返回入库的 Source 列表"""

class NowcoderAPI:
    """HTTP 传输层：cookie、随机间隔、重试"""
    def __init__(self, cookie: str, interval: float, retries: int): ...
    def get_list(page: int) -> list[dict]        # POST job-experience API → 条目

def parse_list_response(payload: dict) -> list[dict]  # 纯函数：API JSON → 条目
def detect_session_expired(resp) -> bool         # 登录态失效识别
```

## 3. 关键决策

- **解析与 HTTP 分离**（抗改版核心）：解析函数纯函数，用 fixture JSON 单测；改版只改解析层，不动传输层
- **限速**：请求间随机间隔，基准 `config.request_interval` ±30%（防风控模式识别）
- **登录态失效**：识别 401 / JSON code 999/998 → 记日志 + 停用本日牛客源（不重试、不弹登录流程）
- **增量**：`source_hash` UNIQUE 天然去重，新条目才入库；按页遍历直到遇到已存在条目或页尾

## 4. 错误隔离

- `NowcoderError`（继承 CrawlerError）；单源失败由 M12 捕获跳过，不影响其他源
- HTTP 层：5xx/超时指数退避重试（配置次数），耗尽抛错
- cookie 失效不算"失败"：记日志后安静停用，任务报告显示 `fetched=0, reason=cookie_expired`

## 5. 测试计划（`tests/test_nowcoder.py`）

**解析层（fixture JSON 纯单测）**

| 类别 | 用例 |
|---|---|
| happy | 正常列表响应 → 条目（标题/URL/正文全文）；contentType=250 卡片跳过 |
| edge | 空列表；无 momentData 条目跳过；无标题无正文条目跳过 |
| fail | success=false 抛 NowcoderError；非 JSON 对象抛错；改版结构变化抛错 |

**HTTP 层（httpx.MockTransport）**

| 类别 | 用例 |
|---|---|
| happy | 顺序请求、间隔生效（mock 时钟断言） |
| edge | 429 响应 → 退避重试后成功 |
| fail | 502 重试耗尽抛错、连接超时、cookie 缺失 401 |

**集成**

- collect() 正常入库；重复源被 UNIQUE 拦截返回幂等；cookie 失效路径返回空 + reason

## 6. 技术选型

**httpx（sync Client）**

- 优：`MockTransport` 测试基建成熟（离网跑测试是硬要求）、超时/限速控制粒度细、与 openai SDK 共用 http 栈
- 缺：async 心智成本（本项目用 sync 模式规避）
- 理由：requests 无等价 MockTransport；sync 模式在 FastAPI 线程池中运行无碍

## 7. 实现提示

- 牛客接口可能变化：列表接口、解析函数、详情 URL 常量模块单点修改
- 测试 fixture 为真实 API JSON 脱敏样本（`nowcoder_list.json`），标注抓取日期
- `detect_session_expired` 优先看 HTTP 状态码（401），其次 JSON envelope code（999/998），再其次响应体登录提示文本

## 8. 外部状态记录（2026-08-08，当日修复）

**2026-08-08 牛客改版 → 当日定位并修复，源已恢复**：

改版现象（已确认）：
- 老列表 `https://www.nowcoder.com/discuss?type=2&page=1` 及 `/discuss` → **301 到首页 `/?target=main`**（带登录 cookie 仍 301）
- 新版面经入口 `https://www.nowcoder.com/interview/center`（导航「面试经验」）是 **SPA 壳**，列表由 XHR 加载；`/interview/experience` 路由已删（站内 404）
- 盲猜 API（`/api/discuss/search` 等）打到 **www 域名会返回 HTML 兜底页**——真实 API 域名是 `gw-c.nowcoder.com`（前端 axios baseURL，从 1.0.488 `page/interview/main.entry.js` 反编译确认）

修复后的数据流（新接口自带正文全文，**无需详情请求**）：
- 列表：`POST https://gw-c.nowcoder.com/api/sparta/job-experience/experience/job/list`
  - body：`{companyList, jobId, level, order, page, isNewJob}`；`order` 3=最新 / 1=综合（前端 tabMap）；`jobId=-1, level=1` 为全部岗位
  - 响应：`data.records[]`，面经条目 `momentData`（id/title/content，content 为纯文本全文）；`contentType=250` 的 AI 总结卡片无 momentData，解析层跳过
- 详情 URL：`https://www.nowcoder.com/discuss/{momentData.id}`（入库 URL 用，不再抓取）
- 登录态失效：JSON envelope `{"success": false, "code": 999}`（USER_NOT_LOGIN）或 998（异地登录），由 `detect_session_expired` 识别 → 安静停用本日源
- 接口需登录 cookie（匿名/过期返回 HTML 兜底页，`resp.json()` 失败 → `NowcoderError` 按"结构变更"报错）

相关改动：`parse_list_page(html)`/`parse_detail_page(html)` → `parse_list_response(payload)`；fixtures 换为真实 API JSON 脱敏样本（`nowcoder_list.json` 等，抓取日期 2026-08-08）。

**修复路径（解析与 HTTP 已分离，单点修改）**：
1. 浏览器 F12 → Network → 过滤 XHR → 打开 `/interview/experience` 翻页，抓取真实列表接口 URL 与响应 JSON 样本
2. 抓取详情页/详情接口样本
3. 更新模块常量（`NOWCODER_LIST_URL` 等）+ 解析函数（`parse_list_page`/`parse_detail_page`）+ fixtures
4. 无法抓取时保持现状即可：GitHub 源与手动导入不受影响
