# M14 Web 路由模块

> 路径：`app/web/routes.py` ｜ 规模：~250 行 ｜ 依赖：FastAPI + 静态页
> 更新日期：2026-08-07（实现偏差记录）

## 1. 职责

FastAPI 路由层：今日题目、答题（单轮/追问轮）、判分结果轮询、历史记录、会话恢复（D18）、手动触发流水线、静态页挂载。只做请求编排与校验，业务逻辑全部委托下层模块。

## 2. 接口（REST）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 静态页（SPA 骨架） |
| GET | `/api/bank` | 题库分页浏览（id 倒序，20/页可配；type/category 筛选；q 关键词搜题干+标签（LIKE 子串，2026-08-09）；含 done 标志；非法参数 400） |
| GET | `/api/today` | 今日题目列表（含待做红点）；`?date=YYYY-MM-DD` 返回被选为当日题目的题（不限 status，日历单选回看，2026-08-09） |
| GET | `/api/questions/{id}` | 题目详情 |
| GET | `/api/questions/{id}/history` | 历史详情：该题全部会话（倒序）+ 问答记录（transcript 含层级）+ 判分（2026-08-08，前端尝试按钮切换） |
| DELETE | `/api/sessions/{id}` | 删单次作答（级联删 attempts/judgments；判分中 409；题目保留） |
| DELETE | `/api/questions/{id}/history` | 删该题全部作答记录（题目保留） |
| POST | `/api/sessions` | 开新会话（body: question_id, kind） |
| POST | `/api/sessions/{id}/answer` | 提交回答（body: answer）；追问链返回下一问，单轮返回"判分中" |
| GET | `/api/sessions/{id}` | 轮询：会话状态 + 判分结果（判分中/完成/失败） |
| GET | `/api/sessions/{id}/resume` | 恢复未完成会话（D18） |
| GET | `/api/history` | 历史记录（按被选为今日题目的日期分组；未作答也展示，可选按题型筛选；前端支持状态/日期/分数/题型/标签筛选，2026-08-08） |
| GET | `/api/review?tag=` | 薄弱点复习（Phase 2 v1）：按词表 tag 检索同类题，未做优先；tag 须在共享词表内否则 400 |
| GET | `/api/review/paper?tag=` | 复习卷 v2：LLM 生成针对性复习讲义（markdown→HTML）+ 推荐练习题目；标签级内存缓存（2026-08-09） |
| GET | `/api/review/tags` | 全部词表标签 + 薄弱点计数（weak_tags 聚合，有计数在前；复习入口页用，2026-08-08） |
| GET | `/api/tags` | 标签分类结构（6 大类，前端筛选联动用，2026-08-08） |
| POST | `/api/upload` | 用户上传题目（JSON {filename, content 或 content_base64, type?}）：direct（Q:/列表行直入，后台补标签+难度+校验改写）/ facejing（面经后台生成）/ resume（后台解析候选，type 缺省自动识别）；pdf/docx/doc 二进制走 base64 + 解析轮询（2026-08-09 上传页三方式 + 多格式） |
| GET | `/api/upload/status/{token}` | 二进制上传解析轮询：parsing/done/failed；resume 完成带 candidates_token（2026-08-09） |
| GET | `/api/upload/candidates/{token}` | 简历解析轮询：running/done/failed + 候选题列表（题干可编辑，2026-08-09） |
| POST | `/api/upload/confirm` | 简历候选确认：编辑后题 → 校验 → 库内去重 → 入库（2026-08-09） |
| POST | `/api/daily/run` | 手动触发流水线，返回 DailyReport |

## 3. 关键决策

- **LLM 调用放后台任务**（`BackgroundTasks`/线程池）：追问生成与判分可能 10-20s+，不阻塞请求；前端轮询 `/api/sessions/{id}`
- **全局异常 handler**：`AppError` → 对应 HTTP 状态码 + 统一错误 JSON；未知异常 → 500 + 日志
- **会话恢复**（D18）：`resume` 重建追问链上下文，轮数从 attempts 接续（调 M11 `resume`）
- **判分失败可重试**：结果接口返回 `status="failed"`，前端显示"判分失败，点击重试"
- **输入校验**：空答案/单字答案 400 拒绝；非法 ID 404
- **并发控制**：同一会话同时作答 → 409（状态机拒绝）

## 4. 错误隔离

- 依赖注入：`get_session`（DB）+ LLM 客户端 + 流水线执行器均可替换——测试用内存 SQLite + FakeLLM
- 路由层不捕获业务细节异常，统一交全局 handler
- 后台任务异常：记日志 + 会话置 failed 状态，前端可感知

## 5. 测试计划（`tests/test_routes.py`，TestClient + FakeLLM + 内存 SQLite）

| 类别 | 用例 |
|---|---|
| happy | 完整答题流（开会话→作答→轮询判分完成）；今日列表；历史列表 |
| edge | 未完成会话恢复续答；空题目列表返回空数组；追问链多轮往返 |
| fail | 无效 ID 404；空答案 400；判分失败返回 failed 可重试；并发作答同会话 409；手动触发流水线返回报告（成功/失败） |

## 6. 技术选型

**FastAPI + uvicorn + 原生 JS 静态页**（不做重前端框架）

- 优：自动 OpenAPI 文档、pydantic 校验、TestClient 测试成熟、async 支持后台任务
- 缺：相对 Flask 多 async 心智；重前端框架工作量（已用静态页规避）
- 理由：D4 已定 FastAPI；判分/追问放后台任务 + 轮询是复杂度最低的异步方案（比 WebSocket 简单且可靠）

## 7. 实现提示

- 后台任务封装 `run_answer_job(session_id, answer)`：调 M11 → 写 attempts → finish 时调 M10 判分 → 更新会话状态
- 轮询响应结构：`{status: active|judging|done|failed, message?, judgment?}`
- 静态页目录 `app/web/static/` 挂载 `StaticFiles`；左侧侧边栏导航（可收起/展开，localStorage 记忆），页面：今日题目（含题型/分类筛选与统计抬头）/答题（追问链对话时间线+层级徽标）/结果（四维评分+参考标记）/历史（状态/日期/分数/题型/分类筛选+详情复盘+删除）/薄弱点复习（入口标签页+列表筛选）/详情（多次作答切换）
- 通知：静态页在每日流水线触发后轮询 `/api/today`，检测到新题弹 Notification API（config 可关）

## 8. 实现偏差记录

- **`POST /api/daily/run` 返回 202 语义**（`{status: "running"}` + 后台任务），非文档的"返回 DailyReport"——真实流水线耗时分钟级，同步返回会挂起请求；前端轮询 /api/today 感知结果
- **判分失败重试路径**：finished+failed 会话再次作答 → 从 attempts 重建 transcript 直接重判（不走 resume→next_round，后者对 finished 会话抛 ChainStateError，首个测试即抓到）
- **路由测试用文件 SQLite**：FastAPI 线程池中内存库每线程独立连接会丢表（"no such table"）
- `create_app(config, llm_factory, daily_runner, enable_scheduler)`：llm/runner/调度均可注入；`main.py` 启动时 mkdir data/ + init_db + start_scheduler
- history 为三表内连接：无 Judgment 行的会话（判分失败且未重试）不出现，可接受（MVP）
- 后台任务异常只记日志、会话保持 active 可重试（模型无 failed 会话状态，与 M10 failed 判分状态区分）

### 9. 追加修复记录（2026-08-08）

- **「立即更新」长轮询**：原实现轮询一次（3s）必然看不到首次同步结果（真实 LLM 生成 36 题需 10-20 分钟，一次性成本）。改为持续轮询 /api/today 最长 20 分钟（按钮显示已运行分钟），有题自动刷新 + 浏览器通知
