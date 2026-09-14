"""文档一致性检查。

它不是"通用文档 linter"，而是**从本项目实际发生过的失败出发**的一组断言：
每一条规则都对应一次真实踩坑，而不是抽象的整洁性偏好。

用法：
    python scripts/check_docs.py            # 检查，有问题则退出码 1
    python scripts/check_docs.py -v         # 打印全部检查项（含通过的）

设计原则：只查**机器能确定对错**的事。语义漂移（同一个词在两处被理解为不同东西）
机器查不出来 —— 那仍然靠人读，见 docs/INDEX.md 的入口协议。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 输出用 UTF-8 —— 否则 Windows 控制台（默认 GBK）会让脚本自己崩掉。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# 顶层文档必须声明状态；docs/ 下的设计文档同理
STATUS_PREFIX = "> 状态："
# 会过期的文档必须说明被什么替代
# 判据必须用【明确的字段标签】，不能用"替代"这种泛词 ——
# 状态行原文是「会过期（…脚本替代）｜ 被什么替代：…」，那个"替代"是另一句话
# 里的词，会让判据永远通过（变异测试抓出来的空转）。
SUPERSEDED_HINT = ("被什么替代",)

failures: list[str] = []
checks_run = 0


def check(ok: bool, msg: str) -> None:
    global checks_run
    checks_run += 1
    if not ok:
        failures.append(msg)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


    # 关于「已归 ADR 的决策，别处只能索引不能复述」这条纪律：
    # 它由文档纪律第 9 条与人读保证，**不做成检查规则**。
    # 试过一版（按关键词匹配 + 邻近行找 ADR 编号），108 项报错里绝大多数是误报 ——
    # 它把 ADR 自己文件里的「worker」「领域」「森林」都判成"复述"。
    # 教训：判断一句话是【引用】还是【复述】需要理解语义，关键词匹配做不到；
    #       一条满屏误报的规则比没有规则更糟 —— 它会被无视，连真问题一起放过。

def main(verbose: bool = False) -> int:
    # --- 规则 1：顶层活文档必须声明状态 ---------------------------------
    # 对应失败：v1 的 65 份文档里，没一份说过自己什么时候过期，
    #          于是「描述已删除产物的文档」被继续引用。
    for name in ("README.md", "AGENTS.md", "CONTEXT.md"):
        p = ROOT / name
        if p.exists():
            check(
                STATUS_PREFIX in read(p),
                f"{name} 缺少状态声明（{STATUS_PREFIX}…）",
            )

    # --- 规则 2：ADR 必须有状态行 + 影响文档 ----------------------------
    # 对应失败：决策改了但散落各处没同步（第 52 轮要求改 7 处只改了 3 处）。
    adr_dir = DOCS / "adr"
    adrs = sorted(adr_dir.glob("*.md")) if adr_dir.exists() else []
    for p in adrs:
        t = read(p)
        check(STATUS_PREFIX in t, f"{p.name} 缺少状态行")
        check("影响文档：" in t, f"{p.name} 缺少「影响文档：」字段")
        check("## Consequences" in t, f"{p.name} 缺少 Consequences 小节")

    # --- 规则 3：docs/ 下的设计文档必须有状态行 --------------------------
    # 无例外 —— INDEX.md 也声明自己的状态，否则"每份文档都声明"这条规则
    # 会有一个只有人记得的口子。
    for p in sorted(DOCS.glob("*.md")):
        check(STATUS_PREFIX in read(p), f"docs/{p.name} 缺少状态声明")

    # --- 规则 4：会过期的文档必须写明被什么替代 --------------------------
    # 对应失败：一份描述已删除产物的文档仍在被当作需求来源（v1 的 8 份孤儿文档）。
    for p in sorted(DOCS.glob("*.md")):
        t = read(p)
        # 只在【状态声明那一行】里判断，不要全文搜 —— 第一版写成 any(h in t)，
        # 而状态行原文是「会过期（…替代）｜ 被什么替代：…」，本身含「替代」二字，
        # 于是这条规则永远通过，等于空转（变异测试抓出来的）。
        status = next((line for line in t.split("\n") if line.startswith(STATUS_PREFIX)), "")
        if "会过期" in status:
            check(
                any(h in status for h in SUPERSEDED_HINT),
                f"docs/{p.name} 声明为「会过期」，但状态行里没写被什么替代",
            )

    # --- 规则 5：文档里的相对路径链接必须存在 ----------------------------
    # 对应失败：ADR-0004 改名后，README/AGENTS 里的引用一度指向旧文件名。
    for p in [ROOT / n for n in ("README.md", "AGENTS.md", "CONTEXT.md")] + sorted(
        DOCS.rglob("*.md")
    ):
        if not p.exists():
            continue
        for m in re.finditer(r"\]\(([^)#]+\.md)\)", read(p)):
            target = (p.parent / m.group(1)).resolve()
            check(
                target.exists(),
                f"{p.relative_to(ROOT)} 里的链接指向不存在的文件：{m.group(1)}",
            )

    # --- 规则 6：行内引用的文档路径必须存在 ------------------------------
    # 覆盖 `docs/xxx.md` 这种写在正文里的引用（不是 markdown 链接）
    # 含通配符的（如 `docs/adr/*.md`）是描述整个目录，不当作具体路径校验。
    for p in [ROOT / n for n in ("README.md", "AGENTS.md", "CONTEXT.md")] + sorted(
        DOCS.rglob("*.md")
    ):
        if not p.exists():
            continue
        for m in re.finditer(r"`((?:docs/)[^`]+\.md)`", read(p)):
            ref = m.group(1)
            if "*" in ref or "?" in ref:
                continue
            check(
                (ROOT / ref).exists(),
                f"{p.relative_to(ROOT)} 引用了不存在的文档：{ref}",
            )

    # --- 规则 7：决策编号不得重复 ----------------------------------------
    baseline = DOCS / "v2范围基线.md"
    if baseline.exists():
        nums = re.findall(r"^\| (\d+) \|", read(baseline), re.M)
        dupes = {n for n in nums if nums.count(n) > 1}
        check(not dupes, f"决策编号重复：{sorted(dupes)}")

    # --- 规则 8：INDEX.md 必须覆盖 docs/ 下所有文档 ----------------------
    index = DOCS / "INDEX.md"
    if index.exists():
        t = read(index)
        for p in sorted(DOCS.glob("*.md")):
            if p.name == "INDEX.md":
                continue
            check(p.name in t, f"docs/{p.name} 未被 INDEX.md 收录")
        for p in adrs:
            check("docs/adr/" in t, f"INDEX.md 未提到 docs/adr/（漏了 {p.name}）")

    # --- 规则 9：INDEX.md 的职责表里必须能用该路径定位到真实文件 ----------
    # 对应失败：文档被正文提到但没进职责表，于是"这份文档负责什么"仍无答案。
    # 注意：查的是"路径能解析到真实文件"，不是"出现过这个名字" ——
    #       漏掉 `docs/` 前缀的写法必须被抓住。
    if index.exists():
        t = read(index)
        for p in sorted(DOCS.glob("*.md")):
            if p.name == "INDEX.md":
                continue
            found = False
            for m in re.finditer(r"^\|[^|\n]*`([^`]+\.md)`", t, re.M):
                if (ROOT / m.group(1)).resolve() == p.resolve():
                    found = True
                    break
            check(found, f"docs/{p.name} 没有以可定位的路径出现在 INDEX.md 的职责表里")

    # --- 规则 10：ADR 的「影响文档」写的路径必须存在 ----------------------
    # 对应失败：ADR-0004 改名后，别处引用一度指向旧文件名。
    for p in adrs:
        t = read(p)
        m = re.search(r"影响文档：(.+)", t)
        if not m:
            continue
        for ref in re.findall(r"`([^`]+\.md)`", m.group(1)):
            if "*" in ref:
                continue
            check((ROOT / ref).exists(), f"{p.name} 的「影响文档」指向不存在的文件：{ref}")

    # --- 规则 11：正文里引用的「决策 N」必须真的在台账里 ------------------
    # 对应失败：引用过「决策 13」，而台账只有 17-53 —— 悬空指针。
    if baseline.exists():
        ledger = set(re.findall(r"^\|\s*(\d+)\s*\|", read(baseline), re.M))
        for p in [ROOT / n for n in ("README.md", "AGENTS.md", "CONTEXT.md")] + sorted(
            DOCS.rglob("*.md")
        ):
            if not p.exists():
                continue
            for n in set(re.findall(r"决策 (\d+)", read(p))):
                check(n in ledger, f"{p.relative_to(ROOT)} 引用了台账里不存在的「决策 {n}」")

    # --- 规则 12：台账标题里的编号范围必须与实际一致 ----------------------
    # 对应失败：标题写着「17-50」而实际已到 53 —— 冻结的数字。
    if baseline.exists():
        t = read(baseline)
        nums = [int(n) for n in re.findall(r"^\| (\d+) \|", t, re.M)]
        m = re.search(r"^##\s*决策台账（(\d+)[-–—](\d+)）", t, re.M)
        if nums and m:
            check(
                int(m.group(1)) == min(nums) and int(m.group(2)) == max(nums),
                f"台账标题写的是 {m.group(1)}-{m.group(2)}，实际是 {min(nums)}-{max(nums)}",
            )

    # --- 规则 13：ADR 的类型决定它有没有 Consequences ---------------------
    # 按 docs/INDEX.md 的归属判据：「我们决定用 X 而不用 Y，因为…」才写成 ADR。
    # 那类决策必然有权衡，因而必须有 Consequences；纯操作方案不该写成 ADR。
    for p in adrs:
        t = read(p)
        if re.search(r"状态：accepted", t):
            check("## Consequences" in t, f"{p.name} 是 accepted 决策但没有 Consequences")

    # --- 规则 14：CONTEXT.md 里不许出现会变的数字 -------------------------
    # 依据 AGENTS.md 文档纪律第 3 条：CONTEXT.md 只放术语，不放会变的数字。
    # 范围限定：只查"用中文量词描述规模"的写法。纯数字（如"第 N 条"、版本号）
    # 不在此列 —— 否则规则会满屏误报，最后被无视。
    # 限制说明：以 `**术语**：` 开头的定义行整体豁免 —— 术语定义里出现数字
    # （如"二元组"）并不违反纪律。因此一个数字塞进定义行本身是查不出来的，
    # 这类漏网只能靠人读。
    ctx = ROOT / "CONTEXT.md"
    if ctx.exists():
        for i, line in enumerate(read(ctx).split("\n"), 1):
            if line.startswith(("**", "#", ">")):
                continue
            bad = re.findall(r"\d[\d,]*\s*(?:个|道|张|条|份|种|人|轮|次)", line)
            check(not bad, f"CONTEXT.md:{i} 出现会变的规模数字 {bad} —— 术语表只放定义")

    # --- 规则 15：引用 ADR 必须带 .md 后缀 --------------------------------
    # 对应失败：CONTEXT.md 里写 `docs/adr/0005` —— 少了后缀，链接点不动，
    #          而规则 5/6 只查以 `.md` 结尾的引用，所以漏掉了。
    for p in [ROOT / n for n in ("README.md", "AGENTS.md", "CONTEXT.md")] + sorted(
        DOCS.rglob("*.md")
    ):
        if not p.exists():
            continue
        for m in re.finditer(r"`(docs/adr/[\w\-./]+)`", read(p)):
            ref = m.group(1)
            check(
                ref.endswith(".md") or "*" in ref,
                f"{p.relative_to(ROOT)} 引用 ADR 时缺 .md 后缀：{ref}",
            )

    # --- 规则 16：AGENTS.md 的「动手前必须读」表必须覆盖入口协议的文件 -----
    # 对应失败：AGENTS.md 缺 docs/INDEX.md —— 而它是入口协议的第 ② 步，
    #          于是"该读索引"这件事在每个会话里都不会发生。
    agents = ROOT / "AGENTS.md"
    if agents.exists():
        t = read(agents)
        check("docs/INDEX.md" in t, "AGENTS.md 没提到 docs/INDEX.md（入口协议的第 ② 步）")

    # --- 规则 17：引用决策不要写对话轮次 --------------------------------
    # 对应失败：文档里写「（第 28 轮）」「（第 51/52 轮）」—— 轮次只在我们这次
    #          对话里有意义，半年后读者查不到；决策号是自包含、可 grep 的。
    # 例外：「第 1 轮命中 ①」这类是【命中轨迹的举例内容】，不是设计轮次引用。
    for p in [ROOT / n for n in ("README.md", "AGENTS.md", "CONTEXT.md")] + sorted(
        DOCS.rglob("*.md")
    ):
        if not p.exists():
            continue
        for i, line in enumerate(read(p).split("\n"), 1):
            # 排除条件原为「同行含"命中"就跳过」—— 太宽，把像
            # 「改为逐轮命中轨迹（决策 28）：…第 1 轮命中 ①」这样的合规行也放过了。
            # 收窄为：只排除【举例命中轨迹】本身，即"第 N 轮命中"这种句式。
            if re.search(r"第 \d+ 轮[^\d\s]", line) and not re.search(
                r"第 \d+ 轮命中", line
            ):
                check(False, f"{p.relative_to(ROOT)}:{i} 用对话轮次引用决策，应改用决策号")

    # --- 规则 18：设计文档里的实测/状态数字必须自带权威指向 ----------------
    # 对应失败：ADR 里散落着未标出处的 v1 实测数（3033 行 / 471 个测试 /
    #          104 个监听器），读者无法核实，也无法判断它是否已过期。
    # 判据：同一行里出现数量级数字时，应当同时出现权威指向
    #       （`docs/...` / ADR-NNNN / 决策 N / 见 §）。
    # 例外：产品参数（额度点、名额上限、成本估算、外部服务单价、举例用的数字）
    #       是本文自己的权威值，不需要指向别处。
    # 命中「像实测数字」的模式：三位以上的整数，或带中文量词的数量
    # 只抓"像实测"的：三位以上整数，或 20 以上的数量。
    # 小序数（"抽查 3 个批次""5 道题"）与正文里的举例无法与实测区分，
    # 强行要求出处会让规则满屏误报 —— 误报的规则等于没有规则。
    NUM = re.compile(r"\d{3,}|(?:[2-9]\d|\d{3,})\s*(?:个|道|张|条|份|种|人|轮|次|字)")
    PRODUCT_HINTS = (
        "额度", "名额", "成本", "单价", "元/", "估算", "推算", "约", "例如", "举例",
        "阈值", "上限", "预算", "真题", "量级", "毫秒",
        # 尺度假设与粗估：本项目在上线前没有真实用量，这类数字是假设不是实测
        "假设", "假定", "粗估", "用户", "人以内", "名额",
    )
    # 不是实测数的形态：状态码、哈希/十六进制、版本号、常见技术数字
    # 以及**迁移文件名里的序号**（`0001_initial.sql` / `0001`）—— 规则 19 那次
    # 改写窗口的讨论里满篇都是它，第一版没排除，于是那一段整段被规则 18 判成
    # "实测数字"。注意 `\b0001\b` 匹配不到 `0001_initial`（`_` 是词字符），
    # 所以这里要写成前缀形式 —— 第一次就是漏了这个，报错照旧。
    NOT_A_MEASURE = re.compile(
        r"\b[1-5]\d\d\b|sha\d+|SHA256|v\d|\d+x\d+|\b0001[0-9a-z_]*", re.I
    )
    for p in [ROOT / n for n in ("README.md", "AGENTS.md", "CONTEXT.md")] + sorted(
        DOCS.rglob("*.md")
    ):
        if not p.exists() or "v1现状" in p.name or "v1行为规格" in p.name:
            continue  # v1 自己的文档就是那些数字的权威处
        in_fence = False
        for i, line in enumerate(read(p).split("\n"), 1):
            s = line.strip()
            if s.startswith("```"):
                in_fence = not in_fence   # 代码块内的内容不是正文陈述，跳过
                continue
            if in_fence or s.startswith(("|", ">", "#")) or not s:
                continue
            # 权威指向的几种写法：跨文档引用（`docs/…` / ADR-NNNN / 决策 N / 见 §）
            # 或文档内自指（见上表 / 上表 / 数据来源见上 / 见下方）
            if re.search(
                r"`docs/|ADR-\d|决策 \d|见 §|权威|见上表|上表|来源见上|见下方|见上文|见下文",
                line,
            ):
                continue
            if any(h in line for h in PRODUCT_HINTS):
                continue
            if NUM.search(line) and not NOT_A_MEASURE.search(line):
                check(False, f"{p.relative_to(ROOT)}:{i} 出现实测数字但没标出处（或加产品参数说明）")

    # --- 规则 19：引用已被决策废弃的标识符 --------------------------------
    # 对应失败：`migrations/0001_initial.sql` 的两处注释还写着 `evaluations.hits`
    #          —— 而决策 28 早把它移到了 `attempts`，同一个文件里另一处注释
    #          就写着「hits 不在本表」。照那条注释写注销流程会去查一个不存在的列。
    #
    # 判据：`<表>.<列>` 形态的引用，若该表**真的建出来了**、而该列不在它的列集里
    #       → 这是一条失效引用（schema 是权威，见 docs/INDEX.md）。
    #
    # 两条排除，都是实测撞出来的（第一版报了 8 项、其中 5 项是误报：
    # `v1现状` 里的 `sessions.user_id`、`0001` 自述里被删掉的 `evaluations.hits`）：
    #   · 历史快照与 v1 规格**就是那些已删字段的权威处**，不许拿 v2 的 schema 去管它；
    #   · 同一行里明确标了「删除 / 移出 / 不在 / 废止」的是**记录废弃**，不是失效引用。
    # 只查"表存在但列不存在"：表本身不存在是另一类问题（规格漏项），
    # 由 tools/_compare_schema.py 与人审负责 —— 混在一起会让这条规则变成噪音源。
    V1_AUTHORITY = ("v1现状", "v1行为规格")
    REMOVAL_MARKER = ("删", "移出", "不在", "废止", "曾为", "改名为", "改名")
    sql_path = ROOT / "migrations" / "0001_initial.sql"
    if sql_path.exists():
        sql_text = read(sql_path)
        table_cols: dict[str, set[str]] = {}
        for m in re.finditer(
            r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\);", sql_text, re.S
        ):
            cols = {
                c.group(1).lower()
                for c in re.finditer(r'^\s*[\["]?(\w+)[\]"]?\s+[A-Z]', m.group(2), re.M)
            }
            cols |= {
                c
                for pk in re.findall(r"PRIMARY KEY\s*\(([^)]+)\)", m.group(2))
                for c in re.findall(r"\w+", pk)
            }
            table_cols[m.group(1).lower()] = cols

        for p in [ROOT / n for n in ("README.md", "AGENTS.md", "CONTEXT.md")] + sorted(
            DOCS.rglob("*.md")
        ) + [sql_path, ROOT / "migrations" / "_runner.py"]:
            if not p.exists() or any(h in p.name for h in V1_AUTHORITY):
                continue
            for i, line in enumerate(read(p).split("\n"), 1):
                if any(h in line for h in REMOVAL_MARKER):
                    continue
                for ref in re.finditer(r"\b([a-z_]+)\.([a-z_]+)\b", line):
                    t, c = ref.group(1).lower(), ref.group(2).lower()
                    if t not in table_cols or c in table_cols[t]:
                        continue
                    check(
                        False,
                        f"{p.relative_to(ROOT)}:{i} 引用了 {t}.{c}，"
                        f"但 migrations/0001_initial.sql 的 {t} 没有这一列（列："
                        f"{', '.join(sorted(table_cols[t]))}）",
                    )

    # --- 规则 20：命中状态的取值只许有一种说法 ---------------------------
    # 对应失败：`CONTEXT.md` 定义命中状态为三值（命中 / 未命中 / **未涉及**），
    #          而 `docs/v2数据模型.md` 与 0001 的注释写的是「命中/未命中」两值。
    #          这不是措辞问题：**掌握度矩阵的分母是"被考过的考察点"**，
    #          靠"未涉及"把没问到的排到分母之外；两值说法会让实现丢掉一个状态。
    #
    # 判据（窄且可判定）：凡出现「criterion_id → 命中」这种**枚举写法**的地方，
    # 同一行必须出现「未涉及」或指向 CONTEXT.md（术语权威）。
    # 只在枚举写法上判定 —— 正文里说"命中/未命中"的地方很多，全文匹配会满屏误报。
    for p in [ROOT / n for n in ("README.md", "AGENTS.md", "CONTEXT.md")] + sorted(
        DOCS.rglob("*.md")
    ) + [ROOT / "migrations" / "0001_initial.sql"]:
        if not p.exists():
            continue
        for i, line in enumerate(read(p).split("\n"), 1):
            if "criterion_id" not in line or "命中" not in line:
                continue
            if "未涉及" in line or "CONTEXT.md" in line:
                continue
            # 纯映射说明（只有两个箭头目标）才算枚举，避免误伤散文
            if re.search(r"命中\s*[/、]\s*未命中", line):
                check(
                    False,
                    f"{p.relative_to(ROOT)}:{i} 把命中状态写成了两值（命中/未命中）——"
                    f"它是**三值**（命中 / 未命中 / 未涉及），权威定义见 CONTEXT.md；"
                    f"少了「未涉及」，掌握度矩阵的分母就无法排除本轮没问到的考察点",
                )

    # --- 规则 21：JSON 列入库必须 ensure_ascii=False ----------------------
    # 对应失败：`docs/v1行为规格.md` §8.7 把这条标成**硬性继承** —— v1 用自定义
    #          类型 `JSONUtf8` 实现，且注明「历史遗留 bug，重构必须保留此语义」。
    #          漏掉的后果是静默的：中文被存成 `\uXXXX` 之后，**对 JSON 列做 SQL
    #          文本匹配会失效**（v1 真发生过，见那份文档记的 `tags LIKE '%"RAG"%'`）。
    #
    # 只查 `json.dumps`，因为它就是那一层会写进 JSON 列的东西。
    # ⚠️ **此刻它是休眠规则**：`app/` 里还没有实现代码，所以没有任何断言会执行。
    #    按本仓库的规矩（"测试全绿不是证据，能被变红才是"），给它配了一条变异 ——
    #    见 `scripts/selftest_check_docs.py` 的规则 21 那条（自检会临时建出
    #    `app/_selftest_ensure_ascii.py` 再删掉）。
    app_dir = ROOT / "app"
    if app_dir.is_dir():
        for p in sorted(app_dir.rglob("*.py")):
            for i, line in enumerate(read(p).split("\n"), 1):
                if "json.dumps(" not in line or "ensure_ascii" in line:
                    continue
                check(
                    False,
                    f"{p.relative_to(ROOT)}:{i} 的 json.dumps 没有 ensure_ascii=False "
                    f"—— 中文会存成 \\uXXXX，之后对这个 JSON 列的 SQL 文本匹配"
                    f"**静默失效**（docs/v1行为规格.md §8.7）",
                )

    # --- 输出 ------------------------------------------------------------
    # 注意：标记只用 ASCII —— Windows 控制台默认 GBK，✗/✓ 会让脚本自己崩掉。
    if failures:
        print(f"文档一致性检查：{len(failures)} 项未通过（共 {checks_run} 项）\n")
        for f in failures:
            print(f"  [FAIL] {f}")
        return 1

    print(f"文档一致性检查通过（{checks_run} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(verbose="-v" in sys.argv))
