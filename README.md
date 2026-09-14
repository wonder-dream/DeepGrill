# DeepGrill

> 状态：活文档（长期维护）｜ 会话开工前的入口顺序见 `docs/INDEX.md`

**AI 模拟面试平台。**

传统刷题工具给你一道题和一个分数。DeepGrill 给你一个**面试官**——他读完你的简历，用简历里的事生成只属于你的题目，一道一道问下去：答得含糊就继续追问，答不上来就换方向；面试结束后给你一份复盘，告诉你哪个知识点是真的掌握了、哪个只是背过。

## 它做什么

| | |
|---|---|
| **模拟面试** | 完整的一场：连续提问、逐题追问、结束时给出总报告 |
| **单题追问** | 挑一道题，让面试官一挖到底 |
| **题库练习** | 按岗位和知识点刷题，不消耗额度 |
| **简历定制** | 上传简历 → 生成只属于你的题目 → 用这些题来面试你 |
| **掌握度矩阵** | 「候选人 × 知识点」的二维视图，每格是你在该知识点上的考察点命中率 |

判分不只看对错：每道题会拆成若干**考察点**，逐条判定你答到了哪几条、漏了哪几条。所以你收到的不是一句「78 分」，而是「可见性说对了，但你说 volatile 能保证原子性——这是最常见的错误」。

## 现状

**那条主链能完整跑起来**：登录 → 题库挑题 → 逐轮追问（SSE 流式 + 语音输入）→ 收尾 →
面试报告 → 掌握度矩阵。在这条链之外还建起了：v1 题目全量导入、知识层构建管道（分批
提议 + 嵌入聚类 + 人审）、简历 → 私有题集、晋升与内容门禁、事后治理、反馈闭环、
注销与导出、后台与观测页、限流、备份与恢复验证。

**哪些已经做完、哪些还没有，权威在 [`docs/INDEX.md`](docs/INDEX.md) 的「代码的现状」
一节** —— 本文不复述那张表（复述一份必然与代码漂移，而 README 正是这么过期的）。

```bash
python -m migrations.run          # 建库（迁移是显式的一步，不在启动时跑）
python -m app.cli seed            # 灌演示数据（幂等）
python -m uvicorn app.main:app    # 起服务
```

演示账号 `demo@local` / `deepgrill-demo`（口令是公开常量，只用于本地演示库）；
邀请码 `DEEPGRILL-DEMO` 可用来注册新账号。

### 开发（装 dev extra，跑四条校验）

```bash
python -m pip install -e ".[dev]"   # pytest + ruff + mypy
python -m pytest                     # 测试
python -m ruff check .               # lint（决策 38）
python -m mypy                       # 类型检查（决策 38；两处豁免的理由在 pyproject.toml）
python scripts/check_docs.py         # 文档一致性
```

**这几条由 pre-commit 钩子自动跑**（`git config core.hooksPath .githooks`）。
本机没装 dev extra 时钩子会提示一句并跳过 lint/类型检查 —— 拦住提交会让人用
`--no-verify`，而那连文档校验一起跳过了。

运维：`python -m app.cli backup` 做一份备份并**当场验证它能不能恢复**（ADR-0008 的硬
要求 —— 备份"成功"但恢复不了，只在需要它的那一天暴露）；`python -m app.cli status`
看库里的规模。

### 配 API key（要真调模型才需要）

**它只影响"面试官说什么"**：不配也能注册、登录、浏览题库、开面试、出报告 ——
判定会走降级路径（页面上明确提示、本轮记为「未涉及」、会话**不中断**），
而不是崩掉或假装成功。

**方式一：写进 `.env`（推荐，本机长期用）**

```bash
cp .env.example .env        # Windows: Copy-Item .env.example .env
```

然后编辑 `.env`，填三行：

```ini
DEEPGRILL_LLM_API_KEY=sk-…
DEEPGRILL_LLM_BASE_URL=https://api.deepseek.com
DEEPGRILL_MODEL_INTERVIEWER=deepseek-flash
```

**这三个键的权威值是 `.env.example` 与 `app/config.py`**（本文不复述型号串：
换模型是改配置，不是改代码 —— 决策 50）。语音转写与嵌入是两个**独立**的供应商配置
（`DEEPGRILL_STT_PROVIDER` / `DEEPGRILL_EMBEDDING_PROVIDER`），默认 `none` =
**没接**：调用时明确失败并让用户改用别的路，而不是拿假数据糊过去。
⚠️ DeepSeek 不提供 embeddings，所以知识层聚类要另外配一家。

`.env` 已被 `.gitignore` 挡住（**它装的是密钥，不要提交**）；`.env.example` 是
可提交的模板。

**方式二：只给这一次（临时）**

```powershell
$env:DEEPGRILL_LLM_API_KEY = "sk-…"     # Windows PowerShell
python -m uvicorn app.main:app
```

```bash
export DEEPGRILL_LLM_API_KEY=sk-…        # macOS / Linux
```

**优先级**：真实环境变量 > `.env` > 代码里的默认值（所以临时换一次不必改文件）。

配置只有一个来源即环境（含 `.env` 这一层），全部键名以 `DEEPGRILL_` 开头；
模型名按**用途**命名（决策 50）。可配的键与默认值见 `.env.example` 与
`app/config.py`（配置的权威在那两个地方，本文不复述）。

### 设计与取舍都在文档里，**不在本文复述**

- 产品边界与验收标准 → [`docs/v2范围基线.md`](docs/v2范围基线.md)
- 架构决策（以及**被否决的备选**）→ [`docs/adr/`](docs/adr/)
- 术语的确切意思 → [`CONTEXT.md`](CONTEXT.md)
- 数据模型 → [`docs/v2数据模型.md`](docs/v2数据模型.md)（表结构的权威是
  `migrations/0001_initial.sql`）
- 接手前先读 → [`docs/INDEX.md`](docs/INDEX.md)

## 关于 v1

上一版是一个可运行的个人求职刷题工具：多个业务模块、RAG 知识库、多用户、审核门禁，以及数百个离线测试。**它的状态、规模与实测数字以 `docs/v1现状-20260913.md` 为准** —— 本文不复述。

它在**产品定位**上与本项目不同 —— v1 面向单一岗位的个人刷题，DeepGrill 面向大众的模拟面试。v1 的代码不迁移，但它的行为契约（踩坑结论、降级策略、调优常量）被整理成 [`docs/v1行为规格.md`](docs/v1行为规格.md) 作为重写的输入。

## 为什么要重写

v1 能跑，功能也齐 —— 它真正的问题不在功能，在**它的每一个设计都建立在一个后来被推翻的前提上**：

```
「岗位固定为 LLM/Agent 开发 + 单用户 + 人工逐题审核」
        ↓ 改成面向大众的多岗位平台后
标签体系、岗位相关性推荐、每日配额、RAG 知识库、审核门禁
        ↓ 全部失效
而它们共同长在同一个函数里 —— 一个数千行的 `routes.py`（`create_app` 是单个函数）
```

所以这次不是重构成更好看的代码，是**换掉前提、重建结构**：知识从「检索文档」变成「知识点森林（有向无环图）」，审核从「人工前置」变成「自动门禁 + 事后治理」，追问从「按题目难度走预设阶梯」变成「探候选人的能力边界」。

完整的推导过程与取舍记录在 [`docs/INDEX.md`](docs/INDEX.md) 与 [`docs/adr/`](docs/adr/)。

