# 文档索引

> 状态：活文档（长期维护）｜ 它记录其他文档的职责与寿命；本文本身不承载任何结论。

> **用途**：回答两个问题 —— 「这件事该写在哪份文档里」与「这份文档现在还算数吗」。
> 遇到归属模糊时先查这里，**不要新建文档**。
> 本文随文档增删而改；每份文档的寿命由它自己声明，本文只汇总。

---

## 入口协议（每个会话开工前）

```
① README.md          → 这是什么产品
② 本文档（INDEX）     → 有哪几份文档、各自的职责与寿命
③ CONTEXT.md         → 术语的确切意思
④ docs/v2范围基线.md  → 做什么 / 不做什么
⑤ docs/adr/          → 已定架构决策与**被否决的备选**
```

**「哪一步必读」不写在这里** —— 那是 `AGENTS.md`「动手前必须读」表的职责，避免同一件事有两个来源。

其余文档按需深入，不必全读。

---

## 文档清单

### 活文档（长期维护，改了必须同步）

| 文档 | 负责回答什么 | 绝不能放什么 |
|---|---|---|
| `README.md` | 这是什么产品、给谁用、现在什么状态 | 实现细节、内部决策 |
| `docs/INDEX.md`（本文） | 有哪几份文档、职责边界、寿命 | 任何结论本身 |
| `CONTEXT.md` | **一个词在本项目里的确切意思** | 实现细节、范围陈述、会变的数字 |
| `docs/v2范围基线.md` | 做什么 / 不做什么 / 验收标准 / 决策台账 | 实现方式、表结构 |
| `docs/adr/*.md` | 每条架构决策：背景、决定、被否决的备选、影响。**物理布局与导入边界在 `0005` 的下一份 `0010`**（规则冲突时以 `0010` 为准）；**迁移的执行形态在 `0011`** | 操作步骤、表结构 |
| `AGENTS.md` | 工作规则：动手前读什么、文档纪律、项目硬规则 | 具体设计结论（只能引用） |

### 会过期（实现完成后由代码或实测替代，届时归档或删除）

| 文档 | 负责回答什么 | 被什么替代 |
|---|---|---|
| `docs/v2数据模型.md` | 表结构推导与迁移对照 | **代码里的模型定义 + 迁移脚本**；届时只保留「为什么这么设计」的部分（已在 ADR 里） |
| `docs/知识层构建管道.md` | 把 v1 的题目变成知识地图与归属的操作方案 | **离线领域的代码与 prompt 文件**；届时只保留「成本模型与验收标准」 |
| `docs/v1行为规格.md` | v1 的行为契约：继承 / 重标定 / 废弃 | **v2 的代码与测试**；迁移完成后可整体归档 |

### 历史快照（不再维护，只作证据）

| 文档 | 说明 |
|---|---|
| `docs/v1现状-20260913.md` | v2 立项时的 v1 快照（基线 commit `98daeb8`）。**它是历史证据，不是当前事实源** —— 里面记录的 v1 情况不会随 v2 变化 |

---

## 归属判据（避免写错地方）

| 你想写的内容 | 该放哪 |
|---|---|
| 一个新词的确切含义 | `CONTEXT.md` |
| 「我们决定用 X 而不用 Y，因为…」 | `docs/adr/`（新开一份） |
| 「v2 做 / 不做某功能」 | `docs/v2范围基线.md` |
| 「某张表有哪些字段」 | `docs/v2数据模型.md`（**且实现后改由代码定义**） |
| 「怎么保证 AI 不写错」 | `AGENTS.md` |
| 「怎么把旧数据搬过来」 | `docs/知识层构建管道.md` 或新开一份同类操作文档（会过期的那类） |
| 「这段一次性脚本 / 实验代码放哪」 | 仓库根的 `tools/`（跑完即废，可整体删除）。**本表不罗列它里面的文件名** —— 一列清单，文件删了清单还在，那正是 v1 `scripts/` 的腐烂方式 |

**关键判据**：如果这条内容**会随实现而变**，它就属于「会过期」那一类，写的时候要**声明寿命**；如果它**是决策或术语**，它属于活文档。

---

## 代码的现状（哪些已经存在、哪些还没有）

**那条主链已经跑通**：登录 → 题库挑题 → 逐轮追问 → 收尾 → 面试报告 → 掌握度矩阵。
在这条链之外，v1 题目已全量导入、知识层管道与人审页、简历→私有题集、注销与导出、
反馈闭环、后台与观测页都在了。跑法与演示账号见 `README.md`。

| 已经有 | 位置 |
|---|---|
| v2 的**表结构权威**（执行 `python -m migrations.run` 得到全库） | `migrations/0001_initial.sql`（后续增量：`migrations/0002_feedback_workflow.sql`） |
| 迁移执行器（按文件事务 / 逐语句执行 / 校验和漂移检测） | `migrations/_runner.py`、`migrations/test_runner.py` |
| 组装根、配置、引擎与会话、启动期校验、错误页 | `app/main.py`、`app/config.py`、`app/db/`、`app/errors.py`、`app/deps.py` |
| 全部表的映射 + 与迁移逐列对账 | `app/db/models.py`、`app/db/test_models.py` |
| LLM 客户端（重试纪律 / **JSON 容错：解析失败做一次修复重发**（决策 78）/ token 记账）+ prompt 从文件读 | `app/llm/`、`prompts/` |
| 领域：账号 / 题库 / 面试 / 报告 / 知识（掌握度）/ 画像与注销 | `app/account/`、`app/bank/`、`app/interview/`、`app/report/`、`app/knowledge/`、`app/profile/` |
| 额度与降级（决策 13）：单一额度点、耗尽后纯题库模式、token 第二道安全网 | `app/account/service.py`、`app/account/repository.py` |
| 离线队列与 worker（原子认领 / 心跳回退 / 幂等 / TTL 回收 / 任务报告） | `app/offline/`（`jobs` · `worker` · `tasks` 三个模块） |
| 离线管道：简历→档案→私有题集、知识层提议与人审 | `app/offline/profile_pipeline.py`、`app/offline/knowledge_pipeline.py` |
| 页面：首页 / 题库 / 登录注册 / 我的 / 答题 / 报告 / 反馈 / 收藏 / 后台 / 观测 | `app/web/`（一个文件 = 一个 URL） |
| 首页 = 内容推荐中心（决策 3/23/63）：主按钮 / 今日推荐题（按掌握度现算）/ 私有题集 / 继续未完成 / 收藏 | `app/web/home_page.py` |
| 观测页（决策 23）：离线队列 / 待定池 / 库体积 / 质量仪表板 / 离线报告 | `app/web/observability.py`、`app/web/observability_page.py` |
| 收藏夹（决策 63）：收藏 / 取消 / 我的收藏页 / 题库列表 ★ 标记 / 首页与「我的」入口 | `app/bank/favorites.py`、`app/web/favorites_page.py`、`app/web/templates/my_favorites.html` |
| 语音输入（决策 32/33/76）：**STT 接口 + 占位实现 + OpenAI 兼容的真实适配器**、面试页录音与模式切换、轮次标「语音」 | `app/llm/stt.py`、`app/web/static/interview.js` |
| 面试页 SSE 流式（ADR-0004 的第三件事）：两段式回复（散文 + 分隔行 + json）、流式客户端、`prose`/`done`/`error` 帧 | `app/llm/__init__.py`、`app/web/interview_page.py`、`app/web/static/interview.js` |
| 进程内限流（决策 66）：按 IP / 按用户三档、滑动窗口 + reserve/settle、TTL 与容量回收 | `app/ratelimit.py`、`app/main.py`、`app/deps.py` |
| 讲解按需生成 + 缓存（决策 67）：题目 id + 版本为键、不预生成、TTL 与容量回收 | `app/knowledge/explanation.py`、`migrations/0003_explanation_cache.sql` |
| 启动期检查：拒绝占位口令（决策 58）与**半迁移**的库 | `app/db/startup.py` |
| 晋升 + 自动门禁（决策 5/9/68）：四条确定性检查、过闸进公共待定池 | `app/bank/promotion.py` |
| 事后治理（决策 5 + ADR-0002）：重复检测（离线任务）与三个处置动作 | `app/bank/quality.py`、`app/web/quality_page.py` |
| 嵌入（ADR-0008）：接口 + 占位实现 + OpenAI 兼容客户端 + 缓存表（`0004`） | `app/llm/embeddings.py`、`app/offline/embedding_store.py` |
| 全量装配管道（决策 44/46）：分批提候选 → 嵌入粗筛聚类 → 逐簇 LLM 归并（`propose --batched`） | `app/offline/knowledge_pipeline.py` |
| 生成题这条内容来源（决策 4/5）：按缺题的已确认知识点补公共题，过门禁才插入（`app.cli generate`） | `app/offline/generation.py`、`prompts/offline/generate_public_questions.md` |
| 阈值标定报告（决策 70 / §未决 6）：§11 每个数字的样本分布 + `--live` 单位成本，**只读不改常量** | `app/offline/calibration.py` |
| lint 与类型检查（决策 38）：`ruff` + `mypy`，进 dev extra 与 pre-commit 钩子 | `pyproject.toml`、`.githooks/pre-commit` |
| 备份与**恢复验证**（ADR-0008）：`VACUUM INTO` 快照 + gzip + 清单 + 当场自验 | `app/backup.py`、`app/cli.py` |
| **部署产物**（决策 73/77）：server / worker / 备份 / 增量维护四个 systemd 单元 + 两个定时器 + **nginx 反代配置** + 部署步骤 | `deploy/`（`tests/test_deploy_units.py` 把单元/反代与代码的四个不变量钉住） |
| 演示数据与运维命令（`seed` / `status` / `propose` / `mount` / `worker` / `backup` / `generate` / `calibrate`） | `app/cli.py`、`app/offline/seed.py` |
| v1 题目导入（只读连接 + 幂等 + 标签映射） | `tools/import_v1.py`、`tools/test_import_v1.py` |
| 对照工具（文档 vs SQL 字段差集 —— **审阅辅助，不是校验器**） | `tools/_compare_schema.py` |
| 验收标准的**执行器**（决策 74）：逐条跑判据、打印 PASS/FAIL/BLOCKED/MANUAL；自检用变异证明它会报错 | `scripts/check_acceptance.py`、`scripts/selftest_check_acceptance.py` |
| 依赖与 pytest 配置（收集范围已配死） | `pyproject.toml` |

| 还没有 | 归属 |
|---|---|
| 语音转写的**账号**（代码已就绪：`DEEPGRILL_STT_PROVIDER=api` + 三家 OpenAI 兼容的端点都能用；缺的只是一份 key 与模型名） | 决策 76 |
| **嵌入的真实供应商**（接口 / 占位实现 / 缓存都就位；DeepSeek 不提供 embeddings，见 `.env.example`）—— 配好之前**全量装配跑不了** | ADR-0008 |
| **全量装配本身还没跑**：库里 3007 道导入题仍在待定池（真跑：配嵌入 → `propose --batched` → 人审 → `mount`） | 决策 44/46 |
| 知识层的**增量维护**只剩"机器上装没装"这一步 —— 定时器已在 `deploy/deepgrill-maintenance.{service,timer}`（`generate --enqueue` + `worker --once`），装上即生效 | 决策 46 / 73 |
| **备份的异地那一跳**：仓库做「快照 + 清单 + 当场自验 + 保留策略」与**定时**（`deploy/deepgrill-backup.timer`），上传对象存储仍留在部署侧一行 `ExecStartPost`（ADR-0008 明说频率与保留策略待定） | ADR-0008 / 决策 73 |
| 验收标准里剩两条不是绿的就跑不完：**待定池为 0**（等装配）与**综合题人工验证**（真题 1065）—— 其余 10 条由 `scripts/check_acceptance.py` 自动跑 | 决策 74 |

> **已经还掉的三笔债**（原先在这张表里）：枚举列的 `CHECK` 约束（决策 57）已进
> `migrations/0001_initial.sql`；**启动拒绝占位口令**（决策 58）已进
> `app/db/startup.py`（开关 `DEEPGRILL_REQUIRE_SECURE_DB`，**线上必须开**）；
> **v1 题目导入**（§未决 2）已进 `tools/import_v1.py`。

> **收藏夹已经不在这张表里** —— 决策 63 整条落地了（表 + 动作 + 三个页面入口，见上表）。
> **反馈闭环也不在** —— 决策 64 把它定成继承 v1 的工单形态（`migrations/0002_feedback_workflow.sql`）。

> **这张表会过期**，它的用途只是"接手时别以为代码已经在那儿了"。`docs/v2范围基线.md` §未决是待办清单的权威。

---

## 每份文档头部应声明的事

活文档与会过期的文档，头部都要有一行说明自己的身份：

```
> 状态：活文档（长期维护） / 会过期（实现后由代码替代） / 历史快照（不再维护）
```

**没有这一行 = 读者无法判断它还算不算数** —— 而 v1 的死法正是「一份描述已删除产物的文档还在被引用」。
