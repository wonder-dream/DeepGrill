# DeepGrill — 设计文档

> 状态：设计定稿，实现完成（M1-M14 + Phase 2，251 测试全绿）
> 更新日期：2026-08-07（收尾同步）

## 1. 背景与目标

个人求职准备工具：每天自动从牛客等渠道采集最新面经，基于面经生成面试题，向用户提问，答案由 LLM judge 按面试官标准判分并给出可行动的反馈。

- **求职方向**：LLM 应用开发 / Agent 开发岗位
- **核心价值**：把"看面经"升级为"模拟被问 + 获得判分反馈"，训练答题能力而非阅读能力
- **项目定位**：简历项目（README + 文档 + 核心模块测试 + 清晰目录），需经得起面试官看代码

## 2. 已确认决策（决策记录）

| 编号 | 维度 | 决策 | 理由 |
|---|---|---|---|
| D1 | 目标岗位 | LLM 应用开发 / Agent 开发 | 决定爬取内容、题型、判分维度 |
| D2 | 数据源 | 牛客(登录cookie) + GitHub面经仓库 + 简历导入 + 手动导入；社交平台 MVP 仅预留接口（`social.py` 占位，采集延后） | 多源互补，牛客实时但反爬严，GitHub 零反爬，简历驱动 project 题，手动兜底 |
| D3 | 交互形态 | 本地 Web 界面（FastAPI 后端 + 轻量前端） | 长答案输入在 CLI 体验差 |
| D4 | 技术栈 | Python + FastAPI | 爬虫/LLM SDK/数据处理同生态；面试方向匹配 |
| D5 | LLM | DeepSeek 默认，OpenAI 兼容接口，多 provider 可配置；生成与判分模型可分别指定 | 国内直连、成本低；强模型留作判分升级路径 |
| D6 | 存储 | SQLite（SQLModel） | 单文件零运维，个人工具足够；RAG 延后 |
| D7 | 题型 | knowledge 知识型 + design 场景设计题 + project 项目深挖题，均支持追问链模式；生成题目时同步产出 good/bad answer criteria | 覆盖知识题/设计题/简历深挖三类真实考点；判分引用随题生成的判分标准（借鉴 interview-pilot-ai），比 judge 现场自由发挥一致性更高 |
| D8 | 每日待做配额 | knowledge 3-5 题 + design 1-2 题 + project 0-1 题（未导入简历不生成） | 质量优先，成本可控，覆盖三类考点 |
| D9 | 判分输出 | 四维分项(准确/完整/条理/深度) + 总分 + 评语 + 参考答案 + 薄弱点标签 | 反馈可行动，标签沉淀为复习数据 |
| D10 | 判分时机 | 追问链整轮结束后统一判，每轮只作简短引导反馈 | 控制调用次数，避免每轮详细判分成本翻倍 |
| D11 | 调度 | APScheduler 常驻（随 Web 服务） | 进程内定时，无需系统配置 |
| D12 | 通知 | 浏览器 Notification + 页面待办红点，可配置关闭 | 最小可用的触达方式 |
| D13 | Cookie | 手动从浏览器 DevTools 复制到本地配置文件，gitignore 排除 | 简单可靠；绝不入 git |
| D14 | RAG | Phase 2：语义去重 + 薄弱点复习 + judge 参考检索 | MVP 无检索需求，避免过度设计；向量库选型届时再定 |
| D15 | 追问链轮数上限 | 20 轮问答回合，到限强制结束；深挖协议下 LLM 仅在"连续 2 次差评探到底"或"L5 连续 2 轮 correct 证明充分"时提前 finish（2026-08-08 更新，见 docs/深挖追问方案.md） | 追问到"不会为止"探测真实水平；上限兜底成本 |
| D16 | 每日生成上限 | 新入库**不重复问题** ≤ 36（去重后计数，**严格截断**：整批超限只入库额度内；清积压可临时调大 config 再改回），与待做配额（D8）分离 | 防首次同步面经仓库时一次性爆量爆成本 |
| D17 | 总分合成 | total = accuracy×30% + completeness×30% + clarity×20% + depth×20% | 准确与完整优先，条理/深度为辅 |
| D18 | 中断会话 | active 会话支持恢复，追问链从断点续答（✅ 前端入口 2026-08-08 落地：今日列表"进行中"徽标 + 时间线恢复，见 docs/会话恢复方案.md） | 答题中断是常态，上下文已持久化可恢复 |

## 3. 架构

### 3.1 模块图

```
┌─ 数据采集层 ────┐   ┌─ 生成层 ─────┐   ┌─ 交互层 ────┐   ┌─ 判分层 ────┐
│ nowcoder爬虫    │   │ 清洗/规范化   │   │ 今日题目列表 │   │ 四维判分     │
│ github仓库拉取   │ → │ 归一化精确+embedding去重 │ → │ 答题页(深挖追问链)│ → │ 参考答案生成  │
│ 手动导入CLI     │   │ 题目生成(LLM) │   │ 历史/判分展示 │   │ 薄弱点标签提取 │
└───────┬────────┘   └──────┬───────┘   └──────┬─────┘   └──────┬──────┘
        └─────────────────── SQLite (SQLModel) ────────────────┘
```

### 3.2 每日任务流（APScheduler 触发）

```
定时触发(默认 08:00)
  ├─ 1. 采集: 牛客新面经(增量) + GitHub 仓库 git pull + 简历源(若更新)
  ├─ 2. 清洗: 去HTML/广告/口语噪音, 规范化存 Source
  ├─ 3. 挑选: 取未生成过题目的新面经(按优先级/新鲜度排序, 控制在生成上限内)
  ├─ 4. 生成: 面经→knowledge/design 题, 简历→project 题; 均打类型/难度/标签 + good/bad criteria
  ├─ 5. 去重: 归一化精确重复快速路径 + bge-m3 embedding 余弦阈值（0.85）判断新题 vs 库内旧题
  ├─ 6. 入库: 新入库不重复问题累计 ≤36(D16); 从新题池按配额(D8)选当日待做
  └─ 7. 通知: 浏览器 Notification + 页面红点
```

## 4. 数据模型（SQLite / SQLModel）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `sources` | id, type(nowcoder/github/manual/social/resume), url(可空), title(默认空), raw_text, cleaned_text, fetched_at, source_hash(UNIQUE) | 面经/简历原文，UNIQUE 防重复抓取 |
| `questions` | id, source_id, type(knowledge/design/project), stem, tags(JSON), difficulty(int), good_criteria(JSON), bad_criteria(JSON), status(pending/today/done/skipped), created_at | 题库；criteria 为生成时随题产出的判分标准（D7）；枚举另有 DB 级 CHECK 约束 |
| `sessions` | id, question_id, kind(open/design/chain), status(active/finished), started_at, ended_at | 答题会话；追问链一次会话多轮；active 会话可恢复（D18） |
| `attempts` | id, session_id, round_no, is_followup, answer_text, feedback_text | 每轮回答；第 0 轮为原始作答 |
| `judgments` | id, session_id, scores(JSON: 四维，判分失败时含 status="failed"), total_score, review, reference_answer, weak_tags(JSON), model, created_at | 判分结果 |
| `task_logs` | id, task_name, status(str), fetched_count, generated_count, error, ran_at | 每日任务日志，可查错 |

## 5. 模块设计

### 5.1 采集层 `app/crawler/`

- `nowcoder.py`：牛客面经列表/详情接口，带登录 cookie（配置读取），1-3s 随机间隔、UA 伪装、重试与指数退避；失败降级跳过（不影响其他源）。实现可参考 `Zchary1106/agent-interview-hub` 的 `scripts/collect_interviews.py`（牛客/小红书/知乎/CSDN/掘金多平台采集设计）
- `github.py`：维护的面经/八股仓库列表，`git pull` 后解析 markdown 入库
- `importer.py`：CLI 命令 `python -m app import <file> [--type resume|manual]`——手动粘贴面经文本入库，或导入简历（markdown/txt，提取项目经历）
- `social.py`：占位模块（D2），仅定义采集/清洗/入库统一协议与异常处理，返回"未实现"，爬虫实现延后
- 各源输出统一为 `Source` 记录

**默认 GitHub 面经仓库列表**（与目标方向匹配的维护中仓库，`config.yaml` 可增删）：

| 仓库 | 内容 | 初始优先级 |
|---|---|---|
| `Zchary1106/agent-interview-hub` | Agent 工程师 300+ 题带答案、9 家大厂真实面经（MIT） | 高 |
| `jingtian11/EasyOffer` | 大模型面经合集（手撕代码/思考题/面经） | 高 |
| `houchenll/origin-llm` | LLM 八股文 + 面经 + 经典论文 | 中 |
| `luxuantao/advanced_LLM_interview_notes` | 大模型进阶面经 | 中 |
| `datawhalechina/daily-interview` | 通用面经（ML/CV/NLP/开发） | 低 |

### 5.2 生成层 `app/pipeline/`

- `clean.py`：去 HTML、广告、水印；口语化处理；按"一面/二面/三面"结构拆分
- `generate.py`：面经→题目生成。提取策略：
  - knowledge 知识型：面经中明确提到的每个问题（"问了 X"）→ 补全题干生成开放问答题
  - design 场景设计题：按主题聚合（如"设计一个客服 agent"）生成场景设计题
  - project 项目深挖题：独立生成器 `project_generator`，从简历源提取项目清单 → 每项目生成深挖题（"讲一下 XX 项目的技术难点和取舍"），按 `daily.project_limit` 节流；未导入简历则不生成
  - 每道题同步生成 `good_criteria`（高分答案标准）与 `bad_criteria`（常见扣分特征），随题入库，判分时注入 judge prompt（D7）
- `dedup.py`：归一化精确重复快速路径（哈希集合，零 embedding）+ bge-m3 embedding 余弦阈值（0.85，标定见 docs/语义去重方案.md）判重；embedding 不可用降级仅哈希——宁重复勿丢题；入库时按**去重后的不重复问题数**累计，达 D16 上限(36)即停

### 5.3 判分层 `app/judge/`

- `judge.py`：统一 OpenAI 兼容客户端调用；prompt 结构：
  1. 系统提示：面试官角色 + 判分标准定义 + 输出 JSON schema（按题型差异化：knowledge 重准确性，design 重完整性/深度，project 重真实性/反思深度）
  2. 输入：题目、该题 good/bad criteria、完整对话记录（追问链则含所有轮次）、该题标签/难度
  3. 输出 JSON：`{scores: {accuracy, completeness, clarity, depth}, total_score, review, reference_answer, weak_tags}`
  4. 总分合成（D17）：`total = accuracy×0.3 + completeness×0.3 + clarity×0.2 + depth×0.2`
- `chain.py`：追问链状态机：
  - 每轮：judge 读"题目 + 对话历史 + 用户最新回答" → 决定 `continue`（给下一问）或 `finish`
  - 轮数上限 20 轮问答回合（D15），judge 可提前 finish，到限强制结束
  - `finish` 后：整体调用完整判分（D10），每轮只给一句简短引导反馈
  - 中断恢复（D18）：active 会话从断点续答，对话上下文从 attempts 表重建
- 模型分层：`generate_model`（生成/清洗/去重，便宜快）与 `judge_model`（判分/追问，可配更强模型），默认均 DeepSeek

### 5.4 Web 层 `app/web/`

- 页面：今日题目列表（红点提示待做）、答题页（开放题单轮 / 追问链多轮）、结果页（四维雷达或柱状 + 评语 + 参考答案 + 薄弱点标签）、历史记录页
- 中断会话恢复（D18）：今日列表与历史页展示"继续上次未完成"入口，恢复追问链会话
- 静态前端：原生 HTML/JS 或轻量模板渲染，不做重前端框架
- 通知：页面内 Notification API，config 可关

### 5.5 调度 `app/scheduler.py`

- APScheduler `CronTrigger`，默认 08:00，`config.yaml` 可改
- 任务失败写 `task_logs`，不阻塞服务

### 5.6 配置 `config.yaml`

```yaml
llm:
  base_url: https://api.deepseek.com/v1
  api_key_env: LLM_API_KEY        # 从 .env / 环境变量读, 不落盘
  generate_model: deepseek-chat
  judge_model: deepseek-reasoner  # 可配更强模型
nowcoder:
  cookie_env: NOWCODER_COOKIE     # 浏览器 DevTools 复制, gitignore
  request_interval: 1.5           # 秒
  retries: 3
daily:
  max_new_questions: 36           # 每日新入库不重复问题上限(D16)
  knowledge_limit: 5              # 当日待做: 知识型 3-5
  design_limit: 2                 # 当日待做: 场景题 1-2
  project_limit: 1                # 当日待做: 项目深挖 0-1
  chain_max_rounds: 20            # 追问链轮数上限(D15)
  schedule: "08:00"
notification:
  enabled: true
sources:
  github_repos: []                # 面经仓库列表
```

## 6. 测试策略（核心逻辑，不测 UI）

| 模块 | 方法 |
|---|---|
| 判分 | mock LLM（预录响应）测 JSON 解析、四维评分、追问链状态转移 |
| 追问链 | 模拟"答得好→finish" / "答得差→继续追问" 两条路径 |
| 爬虫解析 | fixture HTML 单测，不请求真实网络 |
| 清洗/去重 | 真实面经样本 + 重复样本断言 |
| 生成 | mock LLM 验证 prompt 组装与结果落库 |

## 7. 目录结构

```
DeepGrill/
├── config.yaml
├── requirements.txt
├── .env.example             # 密钥模板（.env 本体 gitignore 排除）
├── README.md               # 架构 + 设计决策 + 快速开始
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI 入口, 启动 scheduler
│   ├── errors.py           # 全项目异常层次（1.2 节）
│   ├── models.py
│   ├── db.py
│   ├── config.py
│   ├── scheduler.py
│   ├── llm/
│   │   └── llm_client.py
│   ├── crawler/
│   │   ├── nowcoder.py
│   │   ├── github.py
│   │   └── importer.py     # social 以 SourceProvider 协议占位（D2）
│   ├── pipeline/
│   │   ├── clean.py
│   │   ├── generate.py
│   │   ├── dedup.py
│   │   └── daily.py
│   ├── judge/
│   │   ├── judge.py
│   │   └── chain.py
│   └── web/
│       ├── routes.py
│       └── static/         # index.html + app.js + style.css
├── tests/
│   ├── fixtures/           # HTML 仿真样本
│   ├── fakes.py            # FakeLLM / FakeSource
│   ├── conftest.py         # db fixture（内存 SQLite）
│   ├── test_config.py      # M1
│   ├── test_models.py      # M2
│   ├── test_llm_client.py  # M3
│   ├── test_clean.py       # M4
│   ├── test_importer.py    # M5
│   ├── test_nowcoder.py    # M6
│   ├── test_github.py      # M7
│   ├── test_generate.py    # M8
│   ├── test_dedup.py       # M9
│   ├── test_judge.py       # M10
│   ├── test_chain.py       # M11
│   ├── test_daily.py       # M12
│   ├── test_scheduler.py   # M13
│   └── test_routes.py      # M14
├── docs/                   # 设计/模块/审查/修复记录
└── .gitignore              # 含 .env / .venv / cookie 配置
```

## 8. 安全与反爬

- Cookie / API Key 一律 `.env` 文件 + 环境变量 + 配置文件（`.env`/cookie 配置 `.gitignore` 强制排除），README 警示；`load_config` 加载 config.yaml 同目录 `.env`，真实环境变量优先
- 牛客：随机间隔、UA、重试退避、单源失败降级，不写死频率上限可调
- 本地服务只绑 127.0.0.1

## 9. Phase 2 规划（RAG）

1. **语义去重**：embedding 相似度阈值判断新题 vs 旧题重复，替代 LLM 去重（✅ 2026-08-08：bge-m3 + 余弦阈值 0.85，SQLite BLOB 存储 + numpy 暴力比对；标定见 docs/语义去重方案.md；向量库仍延迟引入，题量万级再评估）
2. **薄弱点复习**：按 `weak_tags` 标签 + 相似度检索历史题目，生成针对性复习卷（v1 ✅ 2026-08-08：SQL 标签匹配版 `/api/review`，判分结果页薄弱点可点击进入；向量检索版待评估后升级）
3. **judge 参考检索**：判分时检索库内同类高分回答片段作为参考答案参照（✅ 2026-08-08：`app/retrieval.py` text/vector/hybrid 三实现，评估集 16 条 hit_rate@5/MRR@5：vector 0.75/0.575 ≥ text 0.50/0.450，hybrid(w=0.8) 0.75/0.596 最优——达标上线；judge_v4 注入参考段 + 深挖层级，空检索降级；评估可复跑 `scripts/eval_retrieval.py`，详见 docs/参考检索方案.md）
- 向量库选型（sqlite-vec / chroma）届时对比定夺
- ~~前置：生成与判分共享标签词表~~ ✅ 已完成（2026-08-08）：`app/tags.py` 单层主题词表（**58 词**，按 6 大类组织），生成 tags / 判分 weak_tags 均经 prompt 约束 + 代码过滤；**旧题标签已 LLM 迁移**（151 题词表重打，词表外残留 0），新题走词表
- **检索评估**：落地前用 hit_rate / MRR 对比 text / vector / hybrid 三种检索方式，参考 `dmytrovoytko/llm-interview-assistant` 的做法（该项目的评估结论：vector 检索普遍优于纯 text，hybrid 最优），不达标不上线

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| 牛客风控/改版导致爬虫失效 | 多源架构 + 失败降级 + task_logs 可观察；爬虫与解析分离便于快速修复 |
| 判分质量不稳定（prompt 漂移） | 判分 prompt 版本化，fixture 回归测试锁行为；judge_model 可升级 |
| 题目重复/低质量 | 归一化精确 + embedding 余弦去重；生成 prompt 明确"避免与已存题目重复"；题量上限控制 |
| LLM 调用成本失控 | 生成用便宜模型、36 题生成上限、判分时机收敛（整轮一次）；追问链 20 轮 × 每日 4-7 题的成本由 judge 提前 finish 兜底（D15/D16） |
| RAG 标签不可靠 | 生成与判分共享标签词表（✅ 已落地，app/tags.py） |
| project 题质量依赖简历导入 | 生成 prompt 要求"基于真实经历、可被追问验证"；简历更新后重新生成 |

## 11. 竞品对比与差异化（README 素材，调研日期 2026-08-07）

### 11.1 调研结论

| 类别 | 代表项目 | 做法 | 与本项目的差异 |
|---|---|---|---|
| 面经资料库 | `agent-interview-hub`(148⭐)、`EasyOffer`(812⭐)、`origin-llm`(353⭐)、`daily-interview`(3784⭐) | 人工整理面经/题库，静态仓库 | 本项目将此类仓库作为**数据源**自动化消费，而非手动阅读 |
| AI 模拟面试 | `interview-pilot-ai`(124⭐) | Claude 扮演面试官，多 persona + STT/TTS | 手动喂简历/职位；本项目**每日自动增量采集面经出题**，判分引用随题生成的 good/bad criteria |
| 多 agent 面试 | `AI-Interview-Agent`(5⭐) | InterviewerAgent + CoachAgent（每轮反馈+最终报告），FastAPI+React | 架构思路相似；本项目判分收敛为"整轮统一判"控制成本，且题库来源自动化 |
| RAG 面试问答 | `llm-interview-assistant`(9⭐) | RAG+ElasticSearch 问答助手，带检索评估 | 用户主动提问；本项目**主动出题考用户**，RAG 为 Phase 2 薄弱点复习 |
| 面经→答案 | `Offer-Patato`(8⭐) | 面经+工作经历→生成个性化答案 | 生成答案给用户读；本项目**用户作答+LLM 判分**，训练答题能力 |

### 11.2 差异化定位（简历卖点）

1. **自动化闭环**：现有项目均为"用户手动喂数据→模拟面试"，本项目是唯一"每日定时增量采集面经 → 自动出题 → 用户作答 → LLM 四维判分 + 薄弱点标签沉淀"的完整闭环
2. **双源出题**：覆盖面经题（knowledge/design）+ 简历项目深挖题（project），比单一题库或单一简历面试覆盖面更全
3. **判分一致性**：good/bad criteria 随题生成、judge 引用判分，而非现场自由发挥
3. **成本可控**：生成用便宜模型 + 每日题量上限 + 整轮统一判，个人日成本≈几毛钱
4. **RAG 延迟引入**：MVP 用 SQL 过滤 + Phase 2 按检索评估（hit_rate/MRR）验收后才上，避免为技术而技术

### 11.3 可直接采用的参考实现

- 采集层：`agent-interview-hub/scripts/collect_interviews.py` 的多平台采集设计
- 判分：`interview-pilot-ai` 的 good/bad answer criteria 思路（已采纳为 D7）
- 检索评估：`llm-interview-assistant` 的 hit_rate/MRR 对比方法（已写入 Phase 2）
