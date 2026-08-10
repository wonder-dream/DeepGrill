# RAG 权威知识获取计划（2026-08-10）

> 状态：**已确认（卡码笔记方案）、待执行** ｜ 目的：为知识库 RAG 提供权威后端知识来源（判分/追问/复习卷的"标准参照"）
> 背景：知识库现有仅 harness.md（Agent 主题讲解文，已蒸馏），与后端题库主题不匹配；需补后端八股文

## 1. 来源评估汇总（已逐一核实）

| 来源 | Stars/许可 | 内容形式 | 覆盖 | 结论 |
|---|---|---|---|---|
| **卡码笔记**（notes.kamacoder.com，程序员Carl《最强八股文》第七版免费在线） | 免费在线、站点版权 | 问答式【简要回答/详细回答/知识扩展/面试官追问 Q&A】 | 计算机基础、C++、Java、Go、大模型（Agent/RAG） | ★★★★★ 首选：密度高、有追问环节与判分场景契合；无官方 GitHub 仓库，需抓取 |
| JavaGuide | 158k / Apache-2.0 | 答案式知识点详解 | Java 全家桶/MySQL/Redis/网络/OS/分布式/消息队列 | ★★★★★ 备选（clone 后按主题挑文件） |
| advanced-java | 79k / CC-BY-SA-4.0 | 问答式 | 高并发/分布式/Redis/消息队列/分库分表 | ★★★★☆ 进阶补充 |
| CS-Notes | 185k / BY-NC-SA | 教科书式笔记 | 网络/OS/数据库原理/Java/系统设计 | ★★★★☆ 基础补充（非商用许可） |
| Waking-Up | 10.3k / GPL-3.0 | 【问题+追问+答案】 | 网络/OS/数据库/Python | ★★★★☆ 追问场景匹配 |
| Go-Interview（honlu） | 69 / Apache-2.0 | 八股文 | Go 基础/并发 + MySQL/网络/OS/Redis | ★★★☆☆ Go 专项 |
| mianshiya（面试鸭） | 5.7k / MIT | 题库网站 | 全语言 1 万题 | ⚠️ 内容是网站数据，提取成本高，不推荐 |

## 2. 卡码笔记抓取方案（首选路径）

### 2.1 `scripts/crawl_kamacoder.py`（新脚本）

- **输入**：分类目录 URL（`https://notes.kamacoder.com/go/` 等）
- **流程**：
  1. 解析目录页 → 提取题目链接（`.html` 链接，过滤导航/专栏介绍/零基础课等非题目页）
  2. 逐题抓取（BeautifulSoup，项目已有依赖）→ 提取正文：标题（h1）+ 简要回答/详细回答/知识扩展/面试官追问 Q&A 各 section；排除侧边栏、页头页脚、评论、广告、赞赏区
  3. 转 markdown（标题层级/代码块/列表保留，图片丢弃）
  4. 存 `data/knowledge/kamacoder/<分类>/<slug>.md`；文件名用 slug（如 `go_gmp_model.md`），**正文首行为完整问题标题**（检索/注入显示友好）
  5. 幂等：文件已存在跳过；0.5s 限速礼貌抓取；参数 `--cat`（分类）+ `--limit`（试水数量）

### 2.2 `import_knowledge.py` 配套改动

- 扫描从 `iterdir()` 改为**递归**（`rglob("*.md")`），支持 `data/knowledge/kamacoder/...` 层级
- title 从 `path.stem` 改为**相对路径**（如 `kamacoder/go/go_gmp_model`）——防不同分类同名 slug 冲突，且注入时能看出处

## 3. 执行顺序

1. 写 `crawl_kamacoder.py` → **抓 `/go/` 试水**（≈40 题，补题库 Go 分类空缺）
2. `import_knowledge.py` 支持子目录递归 → 导入（答案式高密度，**不用 --distill**）
3. 验证：`knowledge_search("GMP 调度模型是什么")` 命中 + 一道 Go 题判分看注入
4. 效果 OK → 扩展抓 `/base/`（计算机基础）、`/java/`、`/llm/`（大模型补充 Agent 主题）
5. 全量回归（pytest + node --check）+ 重启 + 提交

## 4. 边界与合规（已与用户确认）

- 卡码笔记免费在线浏览，抓取内容**仅用于本项目本地个人 RAG 参考**（不收费、不对外分发、不商用）
- 内容存 `data/knowledge/`（**已 gitignore**，不进仓库/部署包）；服务器同步走「导出备份 zip → 导入」链路
- 站方若未来限制抓取：优先人工导出或换备选来源（JavaGuide 等 GitHub 仓库）

## 5. 待确认项（执行前）

1. 抓取顺序：先 Go 试水，还是直接全量抓 4 个分类
2. 正文首行放完整问题标题（推荐）确认
3. 抓取频率/总量：Go 40 题试水后评估再扩
