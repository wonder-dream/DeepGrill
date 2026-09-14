# 仓库布局：应用、迁移、prompt 与一次性工具各占一层，导入方向由目录位置强制

> 状态：accepted ｜ 影响文档：`docs/adr/0005-code-is-split-by-domain.md`（补全其「基础设施层边界待定」）· `docs/v2范围基线.md` 决策 35–39、54–55 · `CONTEXT.md`（web）· `AGENTS.md` §3.7 · `docs/知识层构建管道.md`（运行入口）

ADR-0005 定了**概念**：按领域切、依赖单向、领域内四文件。本文定**物理位置** —— 哪个目录存在、每个目录里放什么、以及**哪些 import 在结构上不被允许**。

规则与 ADR-0005 冲突时以本文为准（它是实施层）。

## 目录

```
pyproject.toml            pytest 收集范围与测试路径**必须显式配死**（ADR-0005 的实测教训）
AGENTS.md  CONTEXT.md  README.md

app/                      应用包 —— 只有它是一个可导入的包
  main.py                 组装根：建 app、挂路由、启动期只做"读配置 + 校验"
  web/                    接口层：唯一同时 import 多个领域的地方，**不含业务规则**
    me_page.py            一个文件 = 一个 URL（C）/home_page.py /interview_page.py
    settings_page.py      /admin_page.py /jobs_page.py
    templates/            页面骨架（web 专属）
    static/               css / JS 模块（web 专属）
  interview/              领域：routes.py service.py pages.py repository.py
  report/                 └─ 同上（无 repository.py —— 报告主体由数据拼装，ADR-0003）
  bank/                   └─ repository.py 是「私有题不许泄露」的**唯一**强制点
  knowledge/              └─ 同上
  profile/                └─ 同上
  account/                └─ 同上
  offline/                编排层：跨领域动作的固定去处（挂载自修复）＋ worker.py
  llm/ db/ config/ errors/   基础设施（app/ 内）
    db/models.py          全部表集中于此 —— 表之间有外键，拆开会让关系断裂（ADR-0005）

migrations/               与 app/ **平级**。schema 变更的唯一入口
  0001_initial.sql        一个迁移一个文件；runner 是 `python -m migrations.run`（待建）
prompts/                  与 app/ **平级**。纯数据（ADR-0007）
  interviewer/ offline/ embed/

tools/                    一次性 / 低频的项目工具：import_v1.py、calibrate_*.py
scripts/                  元工具：check_docs.py、check_staged.py、两个 selftest
data/                     interview.db（gitignored，doctor 检查目录可写）
tests/                    仅跨领域测试 + conftest.py（共享 FakeLLM / 内存 SQLite）
```

领域内的测试**跟着领域走**（`app/bank/test_repository.py`），跨领域的住 `tests/` —— 与导入方向同构（ADR-0005）。

## 每个顶层目录的判据

**判据：它是不是可导入的代码? 它的使用者是谁?**

| 目录 | 为什么单独成层 |
|---|---|
| `app/` | 唯一进包的目录。改它 = 改应用 |
| `migrations/` | **位置就是 §3.7「迁移与启动分离」的强制手段** —— 启动路径要 import 它必须反向 import 一个平级目录，在 review 里一眼可见。规则从"靠自觉"变成"结构上够不着" |
| `prompts/` | 对 `offline` 它是**数据**：改 prompt 是改数据，不是改代码。放进 `app/` 会让每处读取都要 `import app.prompts`，等于把它当代码 |
| `tools/` | 一次性动作（导入 v1、标定阈值）。与 `scripts/` 分开的判据是**能不能被整体删除**：`scripts/` 被 pre-commit 钩子依赖，永远不能删；`tools/` 做完就能删。v1 把两类混在 `scripts/` 里，结果是 `docs/v1现状-20260913.md` 记着"被引用的 `migrate_tags.py` 等全部已删"，而运维清单还在引用它们 —— **没人分得清哪个已死** |
| `data/` | 运行期产物，不是源码 |
| `tests/` | 只放**跨领域**测试 —— 领域内的测试跟领域走（ADR-0005） |

## 导入方向

```
main.py  ──→  web/  ──→  六个领域  ──→  llm / db / config / errors
   └──────────────────────┘
   （main 也直接挂领域路由）

prompts/    被读取，不被 import
migrations/ 只 import 标准库与 sqlite3 —— 不 import app/ 任何模块
tools/      可以 import app/（它是宿主，不是库）
```

**禁止**（结构上就够不着，不是靠记规则）：

- `app/` 里任何模块 import `migrations` —— 迁移必须是**显式的一步**，不许成为启动的副作用
- 领域之间互相 import（ADR-0005 已定）
- 领域反向 import `web/` —— 领域不知道页面长什么样
- `web/` 含业务规则 —— 它 import 一切，是唯一无法被隔离测试的模块

## 本文解决的两处 ADR-0005 遗留待定

**① 「模板渲染算基础设施还是 web 专属」→ web 专属。**

判据是 ADR-0005 自己的那条线：**内容 vs 形状**。领域回答"数据是什么"，`web` 回答"长什么样"，模板是形状 —— 所以它属于 `web/`。

因而 ADR-0005 依赖图里的基础设施清单由五项收窄为四项：**`llm` · `db` · `config` · `errors`**（原列 `templates`，移入 `web/`）。**`db/models.py` 仍集中放全部表**，那是 ADR-0005 的明确决定，本文不改。

**② 「同层的许可依赖清单也待补全」→ 见上「导入方向」。** 同层内部唯一允许的依赖仍只有 `report → interview`（ADR-0005）。

## 一处与 v1 建议的冲突，记录在此

`docs/v1现状-20260913.md` 的重构建议第 7 条写着「**迁移系统换 Alembic**」。**v2 不采纳**，理由：

- Alembic 的价值是 **autogenerate + downgrade**，两者都以上线前 schema 反复演化为前提。而 v2 是一次性重写，`0001_initial.sql` 就是完整 schema，**它的能力花不出去**
- v1 的真正事故不是"手写迁移"，而是**"迁移在启动时跑且带删除"** —— 换成 Alembic 一样会发生（它同样能在 `create_app` 里被调用）。根治它的是本文的目录位置
- 代价是**没有 `downgrade`**，所以配一条硬规则：**任何破坏性迁移（删列 / 删表 / 改语义）必须先备份再执行** —— 这正是 v1 那条建议里真正重要的另一半

v1 快照保持原样（它是历史证据），本条是它的**否决记录**。

## Considered Options

- **迁移放 `app/db/migrations/`**：依赖图上它自然坐在基础设施层。但它与启动代码同属一个包 —— §3.7 退回成一句话，只能再靠一条审查规则兜底。
- **迁移是纯 SQL、连 runner 都不写**（手工 `sqlite3 < 0001.sql`）：最不可静默 —— `ALTER` 与数据迁移的顺序、以及"这条跑过没有"全靠人记，而"迁移状态不可查"正是 v1 的教训之一。
- **跨领域页面放进某个领域的 `pages.py`**：`bank` 就会 import `knowledge` —— 边界一开口，就不会只有一处（ADR-0005）。
- **`web/` 用一个 `pages.py` 装所有页面**：它会长成第二个 v1 `routes.py` —— 因为"加一个页面"的动作变成"往同一个文件里再写一段"。
- **`prompts/` 放 `app/prompts/`**：每处读取都要 import 应用包，等于把 prompt 当代码；而 ADR-0007 要的正是"改 prompt 不算改代码"。

## Consequences

- **启动路径碰不到迁移与 prompt**：它们各自是一个平级目录，不是包内模块。
- **`web/` 的增长被摊到目录上**：加页面 = 加文件，不是一个文件变长。
- **`app/` 里的模块数量有天然上限**：6 个领域 + 1 个编排层 + 4 个基础设施 + `main.py`。
- **`tools/` 是一次性的，可以整体删除** —— 迁移与标定做完后它没有残留价值。
- **`migrations/` 必须是包**（含 `__init__.py`）：否则 `python -m migrations.run` 会失败，而"迁移跑不起来"会让整条装配链卡在第一步。`_` 前缀的文件（`_common.py` / `_runner.py`）是包内部件，不是迁移。
- ⚠️ **`prompts/` 是文件不是包** —— 它不在 `app/` 里，因此**读法唯一**：相对仓库根 `open()`。若放进 `app/prompts/`，正确读法是 `importlib.resources`，而普通 `open` 在开发机上照样能跑、**打包进 wheel/docker 后才失效** —— 这是本项目最怕的失败形状（本地全绿、上线读空）。所以这个位置不只是分类问题，它消灭了第二种读法。
- ⚠️ **`prompts/` 的路径解析必须相对仓库根**（不是相对 `cwd`），否则从别的工作目录跑 `tools/` 时会静默读到空集合 —— 那是 §3.1「静默降级」的一个入口。
- **`web/` 只调领域暴露的数据函数，领域不知道页面长什么样。** 这解决了「跨领域页面归 `web/`」留下的那个洞：`web/me_page.py` 调 `account.remaining_units()` / `knowledge.mastery_matrix()` / `interview.list_recent()` 等，**形状由 `web/` 决定**。反过来的做法（领域导出"片段"函数）会让领域公开面被页面需求牵着走，而 ADR-0005 说领域 `pages.py` 只许调本领域 service，正是为了不让领域知道页面。
- ⚠️ **`web/` 是全项目 import 面最广的目录，因此"按页面切文件"是它成立的前提，不是风格偏好。** import 面广 + 单文件积累 = 又一条 v1 `routes.py` 的路线。这条把「`web/` 按页面切」从审美选择变成结构必要条件。
- **测试替身是契约级的，放 `tests/fakes.py`，由根 `tests/conftest.py` 当 fixture 暴露。** pytest 会从领域测试向上找到根 conftest，所以 `app/<领域>/test_*.py` 与 `tests/` 用的是**同一份** fake —— 与本文的导入规则同构：替身放在能被所有使用者看见的那一层，而不是复制到每个用户手里。判据是**它有几种失效方式**：替身住 `app/` 里时，它多出一条必须与真 client 同步的方法（对流式 / tool call 各写一遍），失效方式是"真实调用路径与 fake 路径静默分叉"，只有跑线上才暴露；住 `tests/` 时失效方式是"所有依赖它的测试一起变红" —— 响亮。
- ⚠️ **fixture 的具体清单不在本文里定**：它的形态由测试决定，不由设计决定。此刻写下的任何清单都是猜测，而猜出来的清单会过期 —— 那正是本仓库三轮在消灭的东西（文档说有、代码里没有）。**本文只定"替身住在哪一层"这条不会过期的规则。**
- **本决定没有涵盖**：`pyproject.toml` 的依赖清单、Dockerfile、观测页面（`jobs_page.py`）的具体形态。
