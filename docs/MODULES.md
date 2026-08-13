# DeepGrill — 模块拆分设计

> 状态：全部模块实现完成（M1-M14 + Phase 2），335 测试全绿
> 更新日期：2026-08-07（收尾同步）
> 原则：独立可运行测试的小模块 / 每模块详细设计 / 单模块 ≤400 行纯逻辑 / 每模块详尽单测（happy+edge+fail）/ 错误隔离 / 简洁不过度防御 / 明确选型与理由

## 1. 总览

### 1.1 模块清单与依赖拓扑

```
M1 config ──────────────┐
M2 models/storage ──────┤
M3 llm_client ──────────┤
                        ▼
M4 clean ──► M8 generate ──► M9 dedup ──► M12 daily ──► M13 scheduler
M5 importer ─┘               ▲             │
M6 nowcoder ─────────────────┘             ▼
M7 github ───────────────────► M10 judge ─► M11 chain ──► M14 web/routes
```

| # | 模块 | 路径 | 预估规模 | 核心依赖 |
|---|---|---|---|---|
| M1 | 配置加载 | `app/config.py` | ~80 行 | pyyaml, pydantic, python-dotenv |
| M2 | 数据模型与存储 | `app/models.py` + `app/db.py` | ~180 行 | SQLModel |
| M3 | LLM 客户端 | `app/llm/llm_client.py` | ~180 行 | openai SDK |
| E1 | Embedding（Phase 2） | `app/embed.py` | ~80 行 | sentence-transformers（bge-m3, GPU） |
| E2 | 检索（Phase 2 §9.3） | `app/retrieval.py` | ~140 行 | E1 + numpy |
| M4 | 清洗 | `app/pipeline/clean.py` | ~150 行 | BeautifulSoup4 |
| M5 | 面经/简历导入 | `app/crawler/importer.py` | ~150 行 | 标准库 |
| M6 | 牛客爬虫 | `app/crawler/nowcoder.py` | ~250 行 | httpx |
| M7 | GitHub 面经源 | `app/crawler/github.py` | ~200 行 | subprocess git |
| M8 | 题目生成 | `app/pipeline/generate.py` | ~250 行 | M3, M2 |
| M9 | 去重 | `app/pipeline/dedup.py` | ~120 行 | E1 + numpy |
| M10 | 判分 | `app/judge/judge.py` | ~220 行 | M3 |
| M11 | 追问链 | `app/judge/chain.py` | ~250 行 | M3, M10 |
| M12 | 每日流水线 | `app/pipeline/daily.py` | ~200 行 | 全部上层 |
| M13 | 调度 | `app/scheduler.py` | ~80 行 | APScheduler |
| M14 | Web 路由 | `app/web/routes.py` | ~250 行 | FastAPI |

### 1.2 横切设计

**共享标签词表**（`app/tags.py`，Phase 2 前置，2026-08-08 落地）：单层主题词表 58 词；生成侧 `tags` 与判分侧 `weak_tags` 均 prompt 约束 + 代码过滤（方案见 docs/标签词表方案.md）；2026-08-08 扩充后端通用标签（缓存/并发/锁/IO/Python/GIL/asyncio/FastAPI）。

**异常层次**（`app/errors.py`，所有模块共享）：

```
AppError
 ├── ConfigError
 ├── StorageError
 ├── LLMError            (retryable)
 ├── LLMJsonError        (retryable)
 ├── CrawlerError
 │    ├── NowcoderError
 │    ├── GitHubError
 │    └── ImportError
 ├── GenerationError
 ├── JudgeError          (retryable)
 └── PipelineStepError(step)
```

- `retryable` 标记驱动重试策略（M3 统一指数退避重试 2 次）
- 模块间只通过返回值/异常契约交互；跨模块异常在 M12/Web 边界统一捕获记录，不向上传播崩溃
- 依赖注入总则：网络/LLM/时间全部注入，测试用 `tests/fakes.py` 的 Fake 实现替换

**测试基建**：
- `tests/fakes.py`：`FakeLLM`（预录响应队列）、`FakeSource`（假采集源）、内存 SQLite fixture
- `tests/fixtures/`：面经 HTML/文本样本、mock LLM 响应 JSON、mock 仓库目录
- 全项目 `pytest` 一键离线运行，不触网、不调真实 LLM

**实现顺序**（每模块含测试同步交付）：
M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8 → M9 → M10 → M11 → M12 → M13 → M14 → 收尾

---

## 2. 模块详细设计

### M1 配置加载 `app/config.py`

**职责**：加载 `config.yaml` + 环境变量（API key / cookie 只从 env 读），输出 pydantic 校验后的配置对象。

**接口**：
- `load_config(path: Path) -> AppConfig`
- `AppConfig`：`llm(base_url, api_key_env, generate_model, judge_model)`、`nowcoder(cookie_env, request_interval, retries)`、`daily(max_new_questions, knowledge_limit, design_limit, project_limit, chain_max_rounds, schedule)`、`notification(enabled)`、`sources(github_repos)`

**关键决策**：
- 密钥（API key/cookie）仅声明环境变量名；`load_config` 先加载 config.yaml 同目录 `.env`（python-dotenv，gitignore 排除），真实环境变量优先、`.env` 兜底；不落配置文件
- 缺失必填字段/类型错误直接抛 `ConfigError`，服务启动时一次性捕获；不静默默认值（除有明确安全默认的项，如 schedule=08:00）
- 非 UTF-8 编码（GBK/UTF-16 BOM）的 config.yaml 归为 `ConfigError`（提示用 UTF-8 保存）

**测试计划**：
- happy：完整 yaml + env 正常加载，字段映射正确
- edge：可选字段缺省取默认值；yaml 含多余字段忽略（pydantic 配置忽略）
- fail：文件不存在 / yaml 语法错误 / 类型错误（如 knowledge_limit 为字符串）/ 必填字段缺失 / api_key_env 对应的 env 未设置

**选型**：`pyyaml` + `pydantic`。
- 优：类型安全、错误信息清晰、与 SQLModel/FastAPI 同生态、简历加分
- 缺：两个第三方依赖
- 理由：比手写 dict 校验可靠；配置结构随项目增长（多源、多限流参数），pydantic 校验成本低收益高

---

### M2 数据模型与存储 `app/models.py` + `app/db.py`

**职责**：6 张表 SQLModel 定义（字段按 docs/DESIGN.md 第 4 章）+ SQLite 连接/会话/建表 + 通用 CRUD 辅助。

**接口**：
- `init_db(db_url)`、`get_session()`（依赖注入用，FastAPI 与流水线共用）
- 模型：`Source`、`Question`、`Session`、`Attempt`、`Judgment`、`TaskLog`
- 关键约束：`Source.source_hash UNIQUE`、`Question.type` 枚举 `knowledge/design/project`、`Session.status` 枚举 `active/finished`、`Judgment.scores` 为 JSON
- 通用辅助（均在 db.py，session 为第一参数）：`list_today_questions`、`pick_questions(limit, qtype=None)`、`latest_task_log`、`count_questions_created_since`、`commit`

**关键决策**：
- 枚举用 SQLModel str-Enum + DB 级 `CheckConstraint`（SQLModel 0.0.39 构造不校验枚举且 sa_column 的 Enum 类型被注解覆盖），非法值入库即抛 `StorageError`，ORM 与裸 SQL 均拦截
- 违反 UNIQUE（source_hash 重复）捕获为 `DuplicateSource` 标记（`app/errors.py`，携带 hash 值），由调用方决定跳过而非崩溃
- 通用查询辅助（按状态选题、最近任务日志）收敛在 db.py，避免各模块重复写 SQL

**测试计划**：
- happy：增删查改、级联删除（session→attempts）、JSON 字段往返、枚举入库
- edge：空表查询、批量写入、长文本字段、多个 active 会话共存
- fail：UNIQUE 冲突 → DuplicateSource（校验携带 hash）、非法枚举值（ORM/裸 SQL 两路径被 CHECK 拒绝）、数据库文件只读；字段超长在 SQLite 上无意义（不强制长度），以长文本 round-trip 覆盖

**选型**：`SQLModel`（构建于 SQLAlchemy + Pydantic）。
- 优：pydantic 集成（API 层直接复用模型）、类型提示、建表/迁移简单、简历项目观感好
- 缺：抽象层较厚、复杂查询调试不如原生 SQL 直观
- 理由：个人工具规模下 SQLAlchemy 能力富余，但类型安全与开发速度收益明确；原生 sqlite3 零依赖但手写映射代码反而更多

---

### M3 LLM 客户端 `app/llm/llm_client.py`

**职责**：统一 OpenAI 兼容调用（DeepSeek/Qwen/OpenAI 可换），封装重试/超时/JSON 输出解析。

**接口**：
- `LLMClient(model: str, base_url: str, api_key: str)`
- `complete(messages, json_schema: dict | None = None) -> str | dict`
  - 带 schema：请求 `response_format=json_object`，解析 JSON 返回 dict，失败抛 `LLMJsonError`
  - 不带 schema：返回文本
- `tests/fakes.py::FakeLLM`：预录响应队列，供全项目测试复用

**关键决策**：
- 指数退避重试 2 次（HTTP 5xx / 超时 / 连接错误），最终失败抛 `LLMError`
- 输出解析容错：剥离 markdown 代码块围栏后再 json.loads，避免模型包 JSON 常见失败
- 超时统一 60s（judge 用深思考模型时放宽由调用方指定）

**测试计划**：
- happy：正常 JSON 返回、纯文本返回、带围栏 JSON 解析成功
- edge：空内容、超长内容、schema 请求时模型返回文本（降级为尝试解析失败路径）
- fail：HTTP 5xx 重试 2 次后抛 LLMError、JSON 损坏抛 LLMJsonError、API key 无效（401）、连接超时、重试间隔退避序列断言

**选型**：`openai` SDK（官方 OpenAI 兼容端点通用）。
- 优：重试/超时/流式/错误类型成熟，一行换 provider（DeepSeek/通义/OpenAI 同协议）；自带结构化输出参数
- 缺：依赖较重（内含 httpx），版本升级可能引入 API 变动
- 理由：手写 HTTP + SSE 是造轮子；多 provider 可配置（D5）是硬需求，SDK 是最稳的兼容层

---

### M4 清洗 `app/pipeline/clean.py`

**职责**：原始文本 → 规范化：去 HTML 标签/广告/水印、去空白噪音、按"一面/二面"结构拆分、提取标题。

**接口**（纯函数，无 IO）：
- `clean_text(raw: str) -> str`
- `split_rounds(text: str) -> list[RoundText]`（`RoundText = {name, content}`）

**关键决策**：
- 纯函数设计：无文件/网络依赖，测试零成本
- 广告/水印过滤用关键词表（如"求私聊""vx""关注公众号"），表放模块常量便于维护
- 输入非 str（None/bytes）由调用方校验，本模块不防御

**测试计划**：
- happy：标准面经 HTML → 纯文本、广告行被滤除、三面结构正确拆分
- edge：空串、全空白、超长文、中英文混合标点、`<script>` 内容剥离
- fail：损坏 HTML 标签（未闭合）不抛异常、HTML 实体（&amp;）正确解码、嵌入式标签属性残留

**选型**：`BeautifulSoup4`（html.parser 后端）。
- 优：容错解析损坏 HTML、get_text 一行提取、依赖轻（lxml 不需要）
- 缺：多一个依赖；解析性能对超大文档一般（个人工具量级无感）
- 理由：牛客正文是嵌套复杂 HTML，正则不可靠；标准库 html.parser 手写遍历繁琐且容错差

---

### M5 面经/简历导入 `app/crawler/importer.py`

**职责**：`python -m app import <file> [--type resume|manual]`：读文件（md/txt），复用 M4 清洗，写 `Source(type=manual|resume)`；简历源额外触发 M8 的 project 题生成。

**接口**：
- `import_file(path: Path, source_type: str) -> Source`
- `collect() -> list[Source]`（与其他源同协议，供 M12 遍历；实现为扫描配置的导入目录）

**关键决策**：
- 重复导入幂等：内容哈希与库内比对，重复返回已有记录标记
- 编码处理：优先 utf-8，失败回退 gbk，再失败抛 `ImportError`
- 简历导入同时生成 project 题（复用 M8 `generate_project_questions`）

**测试计划**：
- happy：md/txt 正常导入、重复导入幂等返回已存在、简历导入触发 project 题生成（FakeLLM）
- edge：空文件、超大文件、BOM 头、gbk 编码中文
- fail：路径不存在、权限拒绝、非法 type 参数、编码双重失败

**选型**：纯标准库（pathlib + codecs）。
- 优：零依赖、行为可预测
- 缺：无
- 理由：无网络无 LLM 的最简模块，引入第三方无收益

---

### M6 牛客爬虫 `app/crawler/nowcoder.py`

**职责**：拉取面经列表（分页）→ 详情 → 清洗 → 入库；cookie 鉴权、限速、重试。

**接口**：
- `collect() -> list[Source]`
- 内部拆分：`NowcoderAPI`（HTTP 传输：cookie、间隔、重试）+ 纯解析函数（列表页/详情页 → 结构化数据），改版只改解析层
- `detect_session_expired(response) -> bool`

**关键决策**：
- 解析与 HTTP 分离：解析函数纯函数，用 fixture HTML 单测；HTTP 层用 `httpx.MockTransport` 测限速/重试
- 限速：请求间随机间隔（config.request_interval 基准 ±30%）
- 登录态失效（401/指定响应特征）→ 记录日志并停用本日牛客源（不重试、不提示重登）

**测试计划**：
- 解析层（fixture HTML）：happy（正常列表页→条目、详情页→正文标题）、edge（空列表页、字段缺失容错）、fail（验证码页、登录失效页、改版后结构变化抛 NowcoderError 但消息可读）
- HTTP 层（MockTransport）：happy（顺序请求、间隔生效断言）、edge（429 响应→退避重试成功）、fail（502 重试耗尽抛错、超时、cookie 缺失 401）
- 集成：collect() 单条入库成功、重复源被 UNIQUE 拦截返回幂等

**选型**：`httpx`（sync Client）。
- 优：`MockTransport` 测试基建成熟（离网跑测试是硬要求）、超时/限速控制粒度细、与 openai SDK 共用 http 栈
- 缺：async 心智成本（本项目用 sync 模式规避）
- 理由：requests 无 MockTransport 等价物；实现参考 `agent-interview-hub/scripts/collect_interviews.py` 的接口结构

---

### M7 GitHub 面经源 `app/crawler/github.py`

**职责**：按 config 仓库列表 `git pull`（首拉 clone），遍历 markdown 文件 → 提取标题/正文 → 清洗入库。

**接口**：
- `collect() -> list[Source]`
- `ensure_repos(repos: list[str]) -> list[Path]`（clone/pull 到本地缓存目录）

**关键决策**：
- 单仓库失败（网络/认证/冲突）跳过该仓库，不影响其他仓库与其他源
- 文件过滤：跳过 README/非 markdown/代码目录/二进制；超大文件（>1MB）跳过
- 仓库缓存目录在 .gitignore 外（数据目录），不污染项目仓库

**测试计划**：
- happy：临时目录 git init 造 fixture 仓库（真实 git 操作）→ clone 成功、增量 pull 新增文件入库、删除文件不再出
- edge：空仓库、无 markdown 仓库、多仓库并行、仓库含大文件被跳过
- fail：系统无 git 命令、仓库不存在、认证失败、网络错误（mock subprocess 返回值）

**选型**：`subprocess` 调 git（非 dulwich）。
- 优：git 能力完整（clone/pull/submodule）、零 Python 依赖、行为与用户 git 环境一致
- 缺：依赖系统安装 git（Windows 需 Git for Windows，个人开发机几乎必装）
- 理由：dulwich 纯 Python 但仅功能子集、维护成本高，为保真度选 subprocess

---

### M8 题目生成 `app/pipeline/generate.py`

**职责**：面经 → knowledge/design 题 + good/bad criteria + 标签/难度；简历 → project 深挖题；统一落 Question 表。

**接口**：
- `generate_from_source(source: Source, existing_questions: list[Question]) -> list[Question]`
- `generate_project_questions(resume_source: Source, limit: int) -> list[Question]`

**关键决策**：
- 题型分流：prompt 让 LLM 输出 `[{type: knowledge|design, stem, tags, difficulty, good_criteria, bad_criteria}]`，代码只做结构校验与落库
- 单篇面经失败仅记 task_logs 跳过，不中断批量；产出空列表合法（面经无有效问题）
- project 生成器按项目清单逐个生成，受 `daily.project_limit` 节流
- 输出结构校验：缺 criteria 字段补默认（good=["完整、准确、结构清晰"], bad=["答非所问"]），非法 type 丢弃该条

**测试计划**：
- happy：面经含 3 问 → 3 题含 criteria；简历 → project 题（FakeLLM 预录）
- edge：面经全是面试官寒暄无问题 → 空结果；题目文本超长截断；criteria 缺字段补默认；非法 type 条目被丢弃
- fail：LLM 输出非 JSON → GenerationError 且该面经跳过；LLM 异常重试后失败；输出与库内重复被 M9 拒收的衔接
- 一致性：good/bad criteria 与 stem 强绑定（同一条目内，不串位）

**选型**：prompt 模板字符串 + M3 的 json_schema 参数，无独立第三方。
- 优：生成逻辑核心在 prompt 设计，代码层保持薄；prompt 版本化常量便于回归
- 缺：prompt 质量波动是主要风险（由 fixture 回归测试锁定）
- 理由：题目生成不涉及复杂计算或框架，引入 LangChain 等反而稀释可控性

---

### M9 去重 `app/pipeline/dedup.py`

**职责**：新题 vs 库内旧题：归一化精确重复快速路径 → bge-m3 embedding 余弦阈值 → 返回去重结果。

**接口**：
- `dedup(new_questions: list[Question], existing_questions: list[Question], embedder) -> list[Question]`（保留不重复者；embedder 注入）
- `normalize_stem(stem: str) -> str`（供快速路径用，纯函数）

**关键决策**：
- 两级策略：归一化精确重复（哈希集合，零 embedding 成本）→ 余弦全量比对（numpy 矩阵，毫秒级）
- **降级**：embedding 不可用（EmbedError）→ 降级仅哈希精确去重——宁重复勿丢题（防误删新题）
- **阈值**：`SIM_THRESHOLD = 0.85`（真实模型标定：60 题最大相似度 0.797，零误杀，见 docs/语义去重方案.md §6）
- 向量持久化：Question.embedding BLOB；旧题 NULL 首次自动补算，新题随 daily 落库

**测试计划**：
- happy：归一化精确重复判重（零 encode）、预设向量语义判重/保留、阈值边界 0.84/0.86
- edge：空新旧列表、旧题向量复用不重算、NULL 自动补算
- fail：embedder 抛错 → 降级仅哈希、降级时语义近似放过、批内多条全部判重
- 幂等：同一批跑两次结果一致

**选型**：bge-m3（sentence-transformers，GPU）+ numpy 暴力余弦 + SQLite BLOB（E1）。
- 优：多语 SOTA、离线免费、GPU 209 题全量 20s、0 向量库依赖
- 缺：torch 依赖重（GPU 版需手动安装）、模型 2.2GB 一次性下载
- 理由：千级题量 numpy 暴力比对毫秒级；向量库（sqlite-vec/chroma）题量到万级再评估

---

### M10 判分 `app/judge/judge.py`

**职责**：四维评分 + 总分合成（D17）+ 评语/参考答案/薄弱点标签；prompt 按题型差异化；注入参考检索片段与深挖层级。

**接口**：
- `judge(question, transcript, model, llm, *, session_id=None, reference=None, max_level=None) -> Judgment`
- 常量 `PROMPT_VERSION = "judge_v5"`（判分 prompt 版本号，随 fixture 冻结）

**关键决策**：
- 输入 = 题目 + 该题 good/bad criteria + 完整对话记录 + 标签/难度（D7）
- 总分合成：`total = accuracy×0.3 + completeness×0.3 + clarity×0.2 + depth×0.2`（D17）
- 分数钳制 0-100；缺失维度补 0；判分失败重试 1 次 → `Judgment(status="failed")` 存库，Web 显示"判分失败可重试"，不影响其他题与后续会话
- 题型差异化 prompt：knowledge 重准确性、design 重完整性与深度、project 重真实性与反思深度
- `reference`（库内同类高分回答，§9.3）与 `max_level`（深挖层级 L1-L5，depth 按此校准）可选注入，空则降级不注入

**测试计划**：
- happy：完整 JSON 判分落库、各维度边界分 0/100、criteria 引用生效（prompt 内容断言含 criteria 文本）
- edge：弱标签列表、参考答案为空、追问链超长 transcript 截断、total 与四维加权一致、reference/max_level 注入断言
- fail：JSON 缺字段补默认、分数越界钳制、LLM 异常重试后降级落 failed 状态、模型输出与 schema 完全不符
- 回归：PROMPT_VERSION 变更需显式更新 fixture（防 prompt 漂移）

**选型**：prompt 模板版本化 + M3。
- 优：判分是核心质量点，版本化 + fixture 回归锁行为（防 prompt 漂移）；参考 interview-pilot-ai 的 criteria 注入（D7）
- 缺：判分质量上限受 judge_model 能力约束（可升级配置）
- 理由：不引入评估框架（如 DeepEval），个人工具规模下 prompt 版本化足够

---

### M11 追问链 `app/judge/chain.py`

**职责**：深挖式多轮面试对话状态机（Deep-Probe Chain）：五层深度阶梯逐层追问到"不会为止"、回答质量驱动策略、20 轮上限强制结束（D15）、层级数据落库供判分校准（D18 断点恢复保留）。

**接口**：
- `ChainSession(session_id, question)`：`next_round(answer) -> RoundResult(interviewer_message, finished)`、`finish(reference=None) -> Judgment`
- `resume(session_id, question) -> ChainSession`（从 attempts 表重建上下文，含 max_level 恢复）

**关键决策**：
- 深挖协议（V2）：五层阶梯 L1 概念 → L2 原理 → L3 权衡 → L4 边界/反例 → L5 横向联系，每轮追问 ≥ 上一层
- 回答质量判定：每轮 LLM 输出 `quality`（correct/partial/wrong/unsure）+ `level`（1-5）；correct 继续加深（L5 连续 2 轮 correct 才证明充分）；wrong/unsure **连续 2 次**判定探到底收尾；partial 同层挖缺口
- finish 仅两种：连续 2 次差评探到底 / L5 证明充分；"答对就结束"不存在；20 轮上限兜底
- 状态持久化：每轮落 attempts（round_no、is_followup、answer、feedback、level），恢复时全量重建（含 max_level）
- 单轮 LLM 失败：重试 → 仍失败则本轮降级为固定提示（"请继续"），不中断会话
- 空回答/单字回答：由 Web 层校验拒绝（400），状态机本身不防御
- 判分衔接：finish 后调用 M10（带 max_level + 可选 reference），失败落 failed 状态仍可重试

**测试计划**：
- happy：答得好 → 逐层深挖到 L5 连续 correct 才 finish（层级递增落库断言）；连续 2 次差评 → 探到底收尾；单次差评不误杀
- edge：恢复后轮数正确接续（含 max_level 恢复）；finish 与判分衔接（prompt 含追问深度 L{n}）；每轮反馈+层级写入 attempts
- fail：LLM 每轮失败 → 降级提示继续；判分失败仍落库 status=failed；finish 后 next_round 抛错（状态机非法调用）

**选型**：纯状态机 + attempts 持久化，无框架。
- 优：轮数/状态逻辑简单可控、零依赖、简历上可清晰讲解
- 缺：无现成对话管理（本场景不需要工具调用/并行分支）
- 理由：LangGraph 等图编排对"单链 20 轮"过度设计，代码量反而更大；差评计数由 prompt 看对话历史判断，代码不维护状态机

---

### M12 每日流水线 `app/pipeline/daily.py`

**职责**：编排采集→清洗→生成→去重→入库→选题→通知；36 题上限（D16）与待做配额（D8）计数；每阶段写 TaskLog。

**接口**：
- `run_daily(config) -> DailyReport`（`{sources: {源: {fetched, failed}}, new_questions, today_questions}`）

**关键决策**：
- 逐源、逐阶段 try/except：单源失败仅该源计数为 0；单阶段失败记录后停止后续阶段但已入库内容保留；任何异常不导致服务崩溃
- 计数控制：新入库**去重后**不重复问题累计 ≤36（D16）；从新题池按配额选当日待做（knowledge 3-5 / design 1-2 / project 0-1，D8）
- 配额选题规则：knowledge 优先，其次 design，最后 project；不足配额按实际可选数
- 通知：生成今日题目后返回报告，由 Web 层触发浏览器通知（模块自身不发通知）

**测试计划**：
- happy：全源正常闭环、各计数正确、当日题目选定
- edge：无新源（全增量零新题）、超 36 上限截断、配额不足取实际值、重复源幂等
- fail：某源全挂其余正常（计数正确）、生成阶段挂（已入库保留、报告含错误）、入库 UNIQUE 冲突幂等、阶段异常不向上传播

**选型**：纯编排代码，无框架。
- 优：流水线是核心资产，保持薄、行为可穷举测试
- 缺：无重试编排能力（单阶段重试由模块内自管，跨阶段重试靠手动触发）
- 理由：Airflow/Celery 对个人工具是过度设计；APScheduler 只负责"何时跑"，"怎么跑"由本模块负责

---

### M13 调度 `app/scheduler.py`

**职责**：APScheduler CronTrigger 每日触发 M12；提供启动/关闭/手动触发。

**接口**：
- `start_scheduler(config, job: Callable)`、`shutdown()`、`trigger_now()`
- `parse_cron(expr: str) -> CronTrigger`（供测试直接验证表达式）

**关键决策**：
- 单次运行异常由 M12 内部消化，调度器不因任务失败停止
- 手动触发 API 由 Web 层暴露（页面"立即更新"按钮）
- 任务互斥：运行时加内存锁，防止重叠执行（长任务跨过调度点）

**测试计划**：
- happy：注册后按 cron 触发（注入假 job，时间推进断言）、手动触发
- edge：cron 表达式边界（00:00/23:59）、时区本地时间
- fail：非法 cron 表达式配置抛 ConfigError、job 抛异常后调度器仍存活、重叠触发被锁拒绝

**选型**：`APScheduler`。
- 优：进程内、零系统配置、CronTrigger 表达式灵活、与 FastAPI 同进程生命周期
- 缺：多进程/多实例部署需额外分布式锁（个人单进程无影响）
- 理由：对比 Windows 计划任务（不可控、不便调试、难随服务启停）与 Celery（过重）

---

### M14 Web 路由 `app/web/routes.py`

**职责**：FastAPI 路由：今日题目、答题提交（单轮/追问轮）、判分结果、历史记录、继续会话（D18）、手动触发流水线、静态页挂载。

**接口**（REST）：
- `GET /`（静态页）、`GET /api/today`、`GET /api/questions/{id}`、`POST /api/sessions`（开新会话）、`POST /api/sessions/{id}/answer`、`GET /api/sessions/{id}`（轮询判分结果）、`GET /api/history`、`GET /api/sessions/{id}/resume`、`POST /api/daily/run`

**关键决策**：
- LLM 调用（追问生成/判分）放后台任务执行，前端轮询结果——避免请求挂起 20s+
- 全局异常 handler：AppError → 对应 HTTP 状态码 + 统一错误 JSON；未知异常 → 500 但记录日志
- 会话恢复：`resume` 重建追问链上下文，轮数从 attempts 接续（D18）
- 判分失败：结果接口返回 `status="failed"`，前端显示"判分失败，点击重试"
- 依赖注入：session/FakeLLM 均可替换，测试用内存 SQLite + FakeLLM

**测试计划**：
- happy：完整答题流（开会话→作答→判分展示）、今日列表、历史列表
- edge：未完成会话恢复续答、空题目列表返回空数组、并发作答同一会话（后到者 409）
- fail：无效 ID 404、空答案 400、判分失败返回 failed 状态可重试、LLM 后台任务超时返回错误态、手动触发流水线成功/失败报告

**选型**：`FastAPI + uvicorn` + 原生 JS 静态页。
- 优：自动 OpenAPI 文档、pydantic 校验、TestClient 测试成熟、async 支持后台任务
- 缺：相对 Flask 多 async 心智；重前端框架工作量（已规避：静态页）
- 理由：D4 已定 FastAPI；判分/追问放后台任务 + 轮询是复杂度最低的异步方案（比 WebSocket 简单且可靠）

---

## 3. 测试策略汇总

| 模块 | 测试文件 | mock 手段 |
|---|---|---|
| M1 | `test_config.py` | 临时 yaml + monkeypatch env |
| M2 | `test_models.py` | 内存 SQLite |
| M3 | `test_llm_client.py` | httpx MockTransport / 假 client |
| M4 | `test_clean.py` | fixture 文本 |
| M5 | `test_importer.py` | 临时文件 + FakeLLM |
| M6 | `test_nowcoder.py` | fixture HTML + MockTransport |
| M7 | `test_github.py` | 临时 fixture git 仓库 + mock subprocess |
| M8 | `test_generate.py` | FakeLLM 预录响应 |
| M9 | `test_dedup.py` | FakeEmbedder + 文件 SQLite |
| E2 | `test_retrieval.py` | FakeEmbedder + 文件 SQLite |
| M10 | `test_judge.py` | FakeLLM 预录响应 |
| M11 | `test_chain.py` | FakeLLM 状态机路径 |
| M12 | `test_daily.py` | FakeLLM + FakeSource + FakeEmbedder + 内存 SQLite |
| M13 | `test_scheduler.py` | 假 job + IntervalTrigger 注入 |
| M14 | `test_routes.py` | FastAPI TestClient + FakeLLM + 文件 SQLite（线程池安全） |

运行方式：`uv run pytest` 一键离线执行（无网络、无真实 LLM、无系统服务依赖，M7 除外需本机 git，测试内自动跳过若无 git）。当前 **335 用例全绿**。

## 4. 技术选型总表

| 依赖 | 用途 | 优 | 缺 | 理由 |
|---|---|---|---|---|
| FastAPI+uvicorn | Web | 自动文档/校验/TestClient | async 心智 | D4 已定 |
| SQLModel | 存储 | 类型安全/建表快 | 抽象厚 | 简历卖点+开发速度 |
| openai SDK | LLM | 重试/多 provider | 依赖重 | 兼容层最稳 |
| httpx | 爬虫 | MockTransport 可离网测试 | — | 测试基建硬需求 |
| BeautifulSoup4 | 清洗 | 容错解析 | 多一依赖 | 正则不够稳 |
| pyyaml+pydantic | 配置 | 类型校验 | 两依赖 | 校验可靠 |
| APScheduler | 调度 | 进程内/表达式灵活 | 多进程需锁 | 个人单进程 |
| subprocess git | 面经源 | 能力完整零依赖 | 需系统 git | dulwich 子集不划算 |
| pytest | 测试 | 生态成熟 | — | 事实标准 |
