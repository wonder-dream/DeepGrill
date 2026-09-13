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
        status = next((l for l in t.split("\n") if l.startswith(STATUS_PREFIX)), "")
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
        ledger = {n for n in re.findall(r"^\|\s*(\d+)\s*\|", read(baseline), re.M)}
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
    NOT_A_MEASURE = re.compile(r"\b[1-5]\d\d\b|sha\d+|SHA256|v\d|\d+x\d+", re.I)
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
