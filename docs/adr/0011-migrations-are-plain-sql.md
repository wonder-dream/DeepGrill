# 迁移是纯 SQL：没有分支可写，也就写不出 v1 那次删库

> 状态：accepted ｜ 影响文档：`docs/v2数据模型.md`（`schema_migrations` 字段）· `docs/adr/0010-repository-layout-and-import-boundaries.md`（`0001` 自建记账表）· `docs/v2范围基线.md` 决策 55–56 · `AGENTS.md` §3.7 · `docs/v1现状-20260913.md`（被否决建议的记录对象）

`migrations/` 里每个文件是一段 **`executescript()` 直接执行的 SQL**。runner 本体只做三件事:比对已记账的文件、跑没跑过的、记账。**它不 import `app/` 的任何模块。**

## 为什么是纯 SQL，而不是 `upgrade(conn)` 函数

v1 那次事故的具体形态是:

```python
# db.py:146-179，启动时执行，幂等（有列即返回）
if "email" not in columns:
    for t in 六张子表: DELETE FROM t
    DROP TABLE users
```

**这行代码之所以能存在，是因为迁移是 Python** —— 它有 `if`。纯 SQL 的迁移**没有分支语法**，写不出「若无某列则删六张表」这种判断。

所以这条决策要的不是「记得备份」这类纪律（纪律会漏），而是**让那类语句在迁移里根本无法被表达**。代价是数据变换（如把 v1 的 `focus` 映射成 `role_points` 初始化数据）不能写在迁移里 —— 它属于 `tools/`，因此是一个**显式的、要单独敲的命令**。那正是 §3.7 想要的效果。

## 记账表：`schema_migrations` **由 runner 用 `CREATE TABLE IF NOT EXISTS` 保证存在**

**本文第一版写的是"由 `0001_initial.sql` 自己创建"，实现时被推翻了。** 那个写法在 runner 与迁移文件之间留下一条**隐式契约**：任何"第一批迁移没写这句"的情形，记账都会崩。它当场就在测试里崩了（合成迁移目录里没有那句 SQL → `no such table: schema_migrations`，而报错指向的却是迁移文件，看不出真因）。

正确写法是 runner 开头执行一次 `CREATE TABLE IF NOT EXISTS`。这与「纯 SQL、没有分支」不冲突：`IF NOT EXISTS` 是**幂等声明**，不是控制流。迁移文件里也保留同一段 DDL 作为 schema 的自文档 —— 两处都幂等，所以谁先谁后都成立。

| 字段 | 说明 |
|---|---|
| `filename` | **主键**。文件名（`0001_initial.sql`） |
| `checksum` | 文件内容的哈希 |
| `applied_at` | 执行时间 |

**`checksum` 不是为了执行，是为了发现"跑过的迁移被改了"。** 那类情况下库的当前状态与文件内容已经对不上，而**它不会有任何报错** —— 典型的静默漂移。runner 发现哈希不符时**报错退出**（不是重跑），因为重跑一个已经生效的迁移可能造成二次伤害。

## 执行语义

```
python -m migrations.run [--check] [--db <路径>]

1. 打开连接（sqlite3 标准库，autocommit 模式，不用 SQLAlchemy —— 见下）
2. CREATE TABLE IF NOT EXISTS schema_migrations
3. 取已记账的 filename 集合；校验 checksum（不符则报错退出）
4. 按文件名排序遍历待跑的迁移：
     BEGIN
       **逐条 execute** 该文件的每条语句
       INSERT INTO schema_migrations
     COMMIT
   任何一步失败 → ROLLBACK 整个文件 → 打印是哪个文件 → 退出码 1
5. 全部成功 → 打印跑了几个 → 退出码 0
```

**必须逐条 `execute`，不能用 `conn.executescript()`** —— 这是本文第一版踩的坑，也是本文自己预言过的那个「唯一跑一次才知道的点」：`executescript()` 执行前会**隐式提交挂起的事务**（Python 3.13 实测依然如此），于是 `BEGIN` 被顶掉、脚本在自动提交模式下跑完，而「失败即整文件回滚」**根本不生效** —— SQL 跑到一半报错会留下半个 schema 且不记账。

因此 runner 自己按 `;` 切语句（正确处理 `--` 注释、`/* */` 注释与字符串里的分号），逐条 `execute`，事务边界才真的在 runner 手里。`migrations/test_runner.py` 里的回滚测试就是这条的守门人：把它改回 `executescript`，那条测试会红（已用突变验证）。

**失败即整文件回滚 + 不记账**：SQLite 的 DDL 是事务性的（`CREATE TABLE` 在 `BEGIN` 里可回滚），所以"跑到一半失败"不会留下半个 schema。而**不记账**保证了下次重跑会从头再来 —— 这是幂等的正确形态（按文件，不是按语句）。

## 不可逆的代价

**没有 `downgrade`** —— 这是 ADR-0010 已经记录的取舍，本文补上它的纪律：**任何破坏性迁移（删列 / 删表 / 改语义）必须先备份再执行**（决策 55）。

## Considered Options

- **Python 模块 + `upgrade(conn)`**：迁移能写任意逻辑，包括数据变换。但**它同时意味着迁移可以含条件与循环** —— 而 v1 的灾难正是一个带 `if` 的迁移函数。**这类决策的目标不是让人少犯错，是让那类代码无法被写出来。**
- **SQL + 内联逃生口**（如 `-- python:` 标记允许夹一段 Python）：两种缺点都拿到 —— 既有分支能力，又让"这个文件是纯 SQL 吗"变成需要逐行确认的问题。
- **用 SQLAlchemy 的 `engine.begin()` 跑迁移**：runner 因此依赖 `app/db`，而 `app/db` 迟早要 import 全部模型（`db/models.py` 集中放表）。于是**跑一个建表迁移会先把所有模型加载一遍** —— 迁移需要的只是两条 SQL，却引入了整条应用依赖链。**runner 只用 `sqlite3` 标准库。**
- **不写 runner，手工 `sqlite3 < 0001.sql`**：ADR-0010 已否决 —— "这条跑过没有"全靠人记，而"迁移状态不可查"正是 v1 的教训之一。

## Consequences

- **迁移文件是字面可审的**：review 一个迁移 = 读一段 SQL，不需要模拟控制流。
- **runner 永远不会变成维护负担**：它没有业务概念，也没有可增长的地方。
- **数据变换成为显式步骤**：`tools/` 里的脚本被单独调用、单独观察、单独重跑。
- ✅ **`executescript()` 的事务语义已实测**（本文原列为"唯一跑一次才知道的点"）：**Python 3.13 下它照样先隐式提交挂起的事务**，因此"失败即整文件回滚"曾是假的。已改为逐条 `execute`，见上文「执行语义」。这条留在这里是因为**它示范了那类决策的验证方式**：不是读文档，是写一条会失败的测试。
- ✅ **首次实跑即抓出两处问题**，两处都已修并有测试守着：① `executescript` 的事务语义；② 记账表的隐式契约（改由 runner `IF NOT EXISTS` 保证）。
- ⚠️ **迁移文件里不许有 `BEGIN` / `COMMIT`**：事务边界由 runner 持有。文件里自己开事务会在回滚时留下不一致状态。
- **本决定没有涵盖**：`tools/` 里那些数据变换脚本的形态（那属于迁移通道，见基线 §未决 2）。
