# M6 牛客采集模块（仅本地个人学习）

> 路径：`app/crawler/nowcoder.py` ｜ 依赖：httpx
> 更新日期：2026-08-08（改版适配）；2026-08-16（合规边界与默认关闭）

## 0. 合规边界（先读）

- 本模块**仅供本地个人学习/研究**使用，**不进入公开服务、生产或多人部署**。
- 默认不注册：`config.yaml` 中 `sources.nowcoder_enabled: false`，每日流水线默认不会创建该源。
- 使用个人登录 Cookie 访问牛客非公开列表接口，存在违反平台条款、账号风控/封禁等风险；使用者自行评估。
- 若项目对外提供服务，应保持本模块关闭，改用有明确授权的数据源或用户主动提交内容。
- 对简历/面试展示的定位：公开服务使用「授权白名单源 + 用户提交 + 手动导入」；牛客仅保留在本地个人学习场景。

## 1. 职责

本地个人面经学习场景下，拉取牛客面经列表（分页，正文随列表返回）→ 清洗 → 入库；处理 cookie 鉴权、限速、重试与登录态失效。

## 2. 接口

```python
def collect(config: NowcoderConfig) -> list[Source]
    """入口：本地个人增量采集，返回入库的 Source 列表"""

class NowcoderAPI:
    """HTTP 传输层：cookie、随机间隔、重试"""
    def __init__(self, cookie: str, interval: float, retries: int): ...
    def get_list(page: int) -> list[dict]        # POST job-experience API → 条目

def parse_list_response(payload: dict) -> list[dict]  # 纯函数：API JSON → 条目
def detect_session_expired(resp) -> bool         # 登录态失效识别
```

## 3. 关键决策

- **解析与 HTTP 分离**：解析函数纯函数，用 fixture JSON 单测；接口变化只改解析层，不动传输层
- **仅显式开启时注册**：`default_sources()` 仅在 `config.sources.nowcoder_enabled=True` 时创建该源，默认关闭
- **限速**：请求间随机间隔，基准 `config.request_interval` ±30%
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
| fail | success=false 抛 NowcoderError；非 JSON 对象抛错；结构变化抛错 |

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

- 本模块只服务本地个人场景；生产/公开部署必须保持 `sources.nowcoder_enabled=false`
- 牛客接口可能变化：列表接口、解析函数、详情 URL 常量模块单点修改
- 测试 fixture 为脱敏样本（`nowcoder_list.json` 等）
- `detect_session_expired` 优先看 HTTP 状态码（401），其次 JSON envelope code（999/998），再其次响应体登录提示文本
- 本地适配接口变化时，以浏览器开发者工具观察到的页面网络请求样本为准，并同步更新 fixture；不在公开文档中展开接口发现/逆向过程

## 8. 变更记录（仅本地维护参考）

**2026-08-08 牛客页面改版 → 本地适配**

现象：
- 老 `https://www.nowcoder.com/discuss?type=2&page=1` 及 `/discuss` 301 到首页
- 新版面经入口为 SPA，列表由 XHR 加载；列表响应自带正文全文，无需详情请求

适配后的数据流：
- 列表：`POST https://gw-c.nowcoder.com/api/sparta/job-experience/experience/job/list`
  - body：`{companyList, jobId, level, order, page, isNewJob}`；`order` 3=最新 / 1=综合；`jobId=-1, level=1` 为全部岗位
  - 响应：`data.records[]`，面经条目 `momentData`（id/title/content，content 为纯文本全文）；`contentType=250` 的 AI 总结卡片无 momentData，解析层跳过
- 详情 URL：`https://www.nowcoder.com/discuss/{momentData.id}`（入库 URL 用，不再抓取）
- 登录态失效：JSON envelope `{"success": false, "code": 999}`（USER_NOT_LOGIN）或 998（异地登录），由 `detect_session_expired` 识别 → 安静停用本日源
- 接口需登录 cookie（匿名/过期返回 HTML 兜底页，`resp.json()` 失败 → `NowcoderError`）

**2026-08-16 合规调整**
- 牛客源默认关闭（`sources.nowcoder_enabled=false`），公开/生产流水线不再注册
- 本文档补充合规边界，删除对外部前端代码分析的细节记录
