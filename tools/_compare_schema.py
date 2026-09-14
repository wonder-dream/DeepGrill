"""生成「文档 vs SQL」的字段对照表，用于人工审阅。

用法：
    python tools/_compare_schema.py            # 打印
    python tools/_compare_schema.py > x.md     # 存成文件

**它不是校验器，是审阅辅助。** 输出里可能仍有噪音（文档单元格写的是解释而不是字段名），
需要人判断。所以它不叫 check_*，也不接进钩子。

做法：两侧各自抽字段名，然后做差集。
  · 文档侧：`docs/v2数据模型.md` 的 `### \\`表名\\`` 小节 → 小节下的表格第一列。
    字段名在文档里有三种写法，都要认：
        反引号  `source_id`          粗体  **input_mode**        裸写  id / session_id
    并跳过「删除的 v1 字段」这类表 —— 那是要删的东西，不是要建的。
  · SQL 侧：`migrations/0001_initial.sql` 里 `CREATE TABLE` 的列名。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "v2数据模型.md"
SQL = ROOT / "migrations" / "0001_initial.sql"

# 表格第一列里不是字段名的东西
HEADER_WORDS = ("字段", "表", "说明", "v1 字段", "为什么删")
# 看起来像字段名：小写字母开头的标识符
IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
# 「删除的 v1 字段」这类表整张跳过 —— 里面的名字是**要删的**，不是要建的
SKIP_SECTIONS = ("删除", "对照", "开放", "风险", "已知", "质量实测", "迁移结论", "不做")

# 这三张表的字段写在 ``**`表名`**`` 子标题下，本工具抓不到（见输出第四节的局限说明）。
# 它们的字段已由人对照 v1 结构补进文档，这里只做记录，让局限可见。
DOC_READ_MANUALLY = {"question_feedback", "task_logs", "schema_migrations"}


def doc_tables() -> tuple[dict[str, list[str]], list[str]]:
    """返回 (表名 -> [字段名], 被跳过的表头们)。"""
    text = DOC.read_text(encoding="utf-8")
    tables: dict[str, list[str]] = {}
    skipped: list[str] = []
    current: list[str] = []
    in_skip = False
    # 一张表内部可能并列好几张两列表（如 `roles` | 说明 / `role_points` | 说明），
    # 靠"第一列自己就是表名"来把字段分流到正确的表。
    active_sub: list[str] = []

    for line in text.split("\n"):
        # 两级与三级标题都算小节边界 —— 「与 v1 的对照」是 `##`，只认 `###` 会让
        # 那张"v1 表 vs v2"对照表的表名被当成字段名（实测撞到过）。
        hm = re.match(r"^#{2,3}\s+(.*)$", line)
        if hm:
            heading = hm.group(1)
            head = heading.split("——")[0].split("—")[0]
            current = re.findall(r"`([a-z_]+)`", head)
            # 「删除的 v1 字段」这类表整张跳过 —— 里面的名字是要删的，不是要建的
            in_skip = any(w in heading for w in SKIP_SECTIONS)
            active_sub = []
            if in_skip and current:
                skipped.append(heading.strip())
            for t in current:
                tables.setdefault(t, [])
            continue

        if not current or in_skip:
            continue

        # 子表宣告的写法 ①：整行就是 ``**`表名`**``（或 `**表名**`），它不带 `|`，
        # 所以必须在"只处理表格行"那道门**之前**拦下来。
        stripped_line = line.strip()
        if (
            stripped_line.startswith("**")
            and stripped_line.endswith("**")
            and stripped_line.strip("`* ").strip() in current
        ):
            active_sub = [stripped_line.strip("`* ").strip()]
            # 子表标题同时**解跳**：`in_skip` 是本小节里某张"对照表"置上的，
            # 而它只该跳过那一张表。不清掉的话，同小节后面的字段表会被一起吞掉
            # （实测：`question_feedback` / `task_logs` 的字段因此全部丢失）。
            in_skip = False
            continue

        if not line.startswith("|"):
            continue

        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        cell0 = cells[0]
        if set(cell0) <= set("-: "):
            continue  # 表格分隔行
        if any(w in cell0 for w in ("v1 字段", "为什么删")):
            # 「删除的 v1 字段」表整张跳过 —— 里面的名字是要删的，不是要建的
            in_skip = True
            skipped.append(f"{current[0]} 的「{cell0}」表")
            continue

        bare0 = cell0.strip("`* ").strip()

        # 先处理"这一行是不是在宣告一张子表"，再处理"这一行是不是字段"。
        # 顺序很重要：`**`表名`**` 去掉星号后与表名相同，若先按"裸表名"判，
        # 它会被当成子表宣告但漏掉子表头那行，于是字段落进上一张子表。
        #
        # 子表宣告有三种写法：
        #   ① `**`表名`**` 单独成行，后面跟着它自己的 `| 字段 | 说明 |`
        #   ② 第一列就是本节里的另一张表名，且第二列是「说明」
        #      （`roles` | 说明 / `role_points` | 说明）
        #   ③ 表头是 `| 表 | 说明 |` 式的对照表 → 不是字段表，整张跳过
        if cell0.startswith("**") and cell0.endswith("**") and bare0 in current:
            # 两种写法都要认：`**`表名`**`（星号在外、反引号在内）与 `**表名**`
            active_sub = [bare0]
            continue
        if bare0 in current and cells[1:] == ["说明"]:
            active_sub = [bare0]
            continue
        if bare0 in current and len(cells) == 2 and cells[1]:
            # `| 表 | 说明 |` 形式的对照表：列 1 是表名而不是字段
            in_skip = True
            skipped.append(f"{current[0]} 的「表 / 说明」对照表")
            continue
        if cell0 in HEADER_WORDS:
            continue

        targets = active_sub or current

        # 三种写法一次抓全
        names = re.findall(r"`([a-z_]+)`", cell0)
        names += re.findall(r"\*\*([a-z_]+)\*\*", cell0)
        if not names:
            for part in re.split(r"[/、,]", cell0):
                p = part.strip().strip("*` ")
                if IDENT.match(p):
                    names.append(p)
        for t in targets:
            tables.setdefault(t, [])
            for n in names:
                if n not in tables[t]:
                    tables[t].append(n)
    return tables, skipped


def sql_tables() -> dict[str, list[str]]:
    """从迁移抽出 表名 -> [列名]。"""
    text = SQL.read_text(encoding="utf-8")
    tables: dict[str, list[str]] = {}
    reserved = {
        "primary", "foreign", "unique", "check", "constraint", "references",
        "key", "not", "null", "default", "autoincrement",
    }
    for m in re.finditer(
        r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\);", text, re.S
    ):
        name, body = m.group(1), m.group(2)
        cols: list[str] = []
        for raw in body.split("\n"):
            line = raw.strip()
            if not line or line.startswith("--"):
                continue
            cm = re.match(r'^[\["]?(\w+)[\]"]?\s+[A-Z]', line)
            if cm and cm.group(1).lower() not in reserved:
                cols.append(cm.group(1))
        # 复合主键写在表级约束里，也要算进去
        for pk in re.finditer(r"PRIMARY KEY\s*\(([^)]+)\)", body):
            for c in re.findall(r"\w+", pk.group(1)):
                if c not in cols:
                    cols.append(c)
        tables[name] = cols
    return tables


def main() -> int:
    d, skipped = doc_tables()
    s = sql_tables()

    print("# 文档 vs SQL 字段对照\n")
    print("左＝文档（`docs/v2数据模型.md`）　右＝SQL（`migrations/0001_initial.sql`）\n")
    print(f"SQL 建了 **{len(s)}** 张表；文档里有 **{len(d)}** 个表小节。\n")

    print("## 一、只在文档里有（**要重点看的：疑似漏建**）\n")
    print("| 表 | 文档提到、SQL 里没有 |")
    print("|---|---|")
    hits = 0
    for t in sorted(s):
        missing = [f for f in d.get(t, []) if f not in s[t]]
        if missing:
            hits += 1
            print(f"| `{t}` | {', '.join('`' + x + '`' for x in missing)} |")
    if not hits:
        print("| —— | 无 |")

    print("\n## 二、SQL 建了、文档没有的小节（**看是不是该补文档**）\n")
    extra_tables = [t for t in sorted(s) if t not in d]
    print("、".join(f"`{t}`" for t in extra_tables) if extra_tables else "（无）")

    print("\n## 三、被跳过的表（要删的 / 对照表 / 风险表，不参与差集）\n")
    for x in skipped:
        print(f"- {x}")

    print("\n## 四、逐表清单（**核对用**：✅=文档提到过这个字段名）\n")
    print(
        "> ⚠️ **已知局限**：`docs/v2数据模型.md` 里 `question_feedback` / `task_logs` /"
        " `schema_migrations` 三张表的字段写在 ``**`表名`**`` 这种子标题之下，"
        "本工具**抓不到**它们 —— 它们的标记全是 `—`，那不是「文档里真的没有」，"
        "而是工具读不到。第五节用人工核对补上。\n"
    )
    for t in sorted(s):
        doc_cols = d.get(t, [])
        unmarked = [c for c in s[t] if c not in doc_cols]
        print(f"### `{t}`　SQL {len(s[t])} 列，其中文档未提到 {len(unmarked)} 个\n")
        print("| 列 | 文档 |")
        print("|---|---|")
        for c in s[t]:
            mark = "✅" if c in doc_cols else ("📄" if t in DOC_READ_MANUALLY else "—")
            print(f"| `{c}` | {mark} |")
        print()

    print("\n## 五、工具读不到、人工核对的三张表\n")
    print('\n这三张原本只写了「沿用 v1」一句、字段不可核；已按 v1 实际结构补进文档。\n')
    for t in sorted(DOC_READ_MANUALLY):
        print(f"### `{t}`\n")
        print(f"- 文档侧：`docs/v2数据模型.md` 的 ``**`{t}`**`` 小节（已补全）")
        print(f"- SQL 侧：{len(s.get(t, []))} 列 —— {'、'.join('`' + c + '`' for c in s.get(t, []))}")
        print()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
