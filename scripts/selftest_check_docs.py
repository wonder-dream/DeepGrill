"""校验器的自检：用变异测试证明 rules 不是空转。

**为什么这个文件必须存在于仓库里**：`check_docs.py` 全部通过，本身
不能说明它有效 —— 一套全是空转的断言也会"全部通过"。本项目真实发生过：
第一版有两条规则从未真正执行（规则 9 只查"名字出现过"、规则 14 的排除逻辑
写反了），是变异测试把它们暴露出来的。

做法：故意破坏文档 → 跑校验器 → 断言它报错 → 还原。
任何一条变异没被抓到，说明对应规则已失效或从未生效。

用法：
    python scripts/selftest_check_docs.py          # 全部变异
    python scripts/selftest_check_docs.py -v       # 打印每条变异的目标规则
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_docs.py"

# 规则 21 是**休眠规则**：它的对象在 `app/` 里，而那个目录此刻只有包的 docstring
# —— 没有变异就没有证据。所以自检为它**临时造一个文件**，跑完删掉。
# 这不是"为了有一条测试而造文件"：JSON 列的中文语义是 v1 明确要求继承的硬性契约
# （`docs/v1行为规格.md` §8.7），而它的失效方式是静默的（SQL 文本匹配失效）。
RULE21_PROBE = ROOT / "app" / "_selftest_ensure_ascii.py"
RULE21_BAD = 'import json\n\nx = json.dumps({"a": "中文"})\n'
RULE21_GOOD = 'import json\n\nx = json.dumps({"a": "中文"}, ensure_ascii=False)\n'

# 规则 12 的锚点**必须跟着台账的当前范围走**。
#
# 第一版把「（1–63）」写死在这里，于是台账一长到 64，自检就报「锚点找不到」——
# 从那以后规则 12 再也没人验过，而它拦下的是**与它无关**的一次提交（新增决策 64
# 那一笔真的撞上了）。这正是本项目反复吃过的「冻结的数字」：把会变的东西抄一份，
# 就一定会漂。现在范围从文档里读，变异体是「把上界减一」，永远与实际不符。
# 读不到就直接报错（`assert`）—— 标题形态变了要有人来改这个锚点，不能静默跳过。
_LEDGER_HEAD = re.search(
    r"## 决策台账（\d+[–—]\d+）", (ROOT / "docs" / "v2范围基线.md").read_text(encoding="utf-8")
)
assert _LEDGER_HEAD, "台账标题的形态变了 —— 规则 12 的变异锚点要跟着改"
_LEDGER_LO, _LEDGER_HI = re.search(r"(\d+)[–—](\d+)", _LEDGER_HEAD.group(0)).groups()
_LEDGER_WRONG = f"## 决策台账（{_LEDGER_LO}–{int(_LEDGER_HI) - 1}）"

# (说明, 目标文件, 原文, 篡改后) —— 每条对应一条规则，规则号写在说明里
MUTATIONS: list[tuple[str, str, str, str]] = [
    ("规则1 顶层文档缺状态行", "README.md", "> 状态：活文档", "> 状态没了"),
    ("规则2 ADR 缺影响文档字段", "docs/adr/0007-prompts-live-in-files.md", "影响文档：", "相关文件："),
    ("规则4 会过期却不说被谁替代", "docs/v2数据模型.md",
     "被什么替代：", "（这是刻意删掉替代说明的变异）"),
    ("规则5 markdown 链接指向不存在", "README.md", "(docs/v2范围基线.md)", "(docs/nope.md)"),
    ("规则7 决策编号重复", "docs/v2范围基线.md", "| 20 |", "| 19 |"),
    ("规则9 INDEX 表格缺 docs/ 前缀", "docs/INDEX.md", "| `docs/v2数据模型.md` |", "| `v2数据模型.md` |"),
    ("规则10 ADR 影响文档指向不存在", "docs/adr/0008-hosting-overseas-single-node.md", "`AGENTS.md`", "`docs/nonexistent.md`"),
    ("规则11 引用台账里没有的决策号", "docs/v2数据模型.md", "（决策 21）", "（决策 999）"),
    ("规则12 台账标题范围与实际不符", "docs/v2范围基线.md",
     _LEDGER_HEAD.group(0), _LEDGER_WRONG),
    ("规则13 accepted 的 ADR 缺 Consequences", "docs/adr/0006-offline-jobs-in-sql-with-a-worker.md", "## Consequences", "## 附注"),
    ("规则15 引用 ADR 缺 .md 后缀", "CONTEXT.md", "docs/adr/0005-code-is-split-by-domain.md", "docs/adr/0005"),
    ("规则16 AGENTS 未提 INDEX", "AGENTS.md", "`docs/INDEX.md`", "`docs/INDEX`", True),
    ("规则17 用对话轮次引用决策", "docs/v1行为规格.md", "（决策 28）", "（第 28 轮）"),
    # 规则18 的锚点要选**稳定**的行，而且必须是**正文段落**：
    # · 第一版锚在 README 的「本仓库目前包含的是重写前的完整设计文档」上，
    #   那句话在"MVP 能跑了"那一笔里被改写 —— 变异随即报「锚点找不到」
    #   （看起来像通过，其实那条规则已经没人验了）。
    # · 第二次锚到 `## 它做什么` 这个**标题**上 —— 而规则 18 本来就跳过
    #   以 `#` / `|` / `>` 开头的行（那是表格、引用与标题），于是变异注入了数字
    #   却永远不会被报，变成一条**空转变异**。★ 修复它本身也靠这条自检报出来。
    # 现在锚在一句描述产品、不随进度改的正文上。
    ("规则18 实测数字无出处", "README.md",
     "传统刷题工具给你一道题和一个分数。",
     "传统刷题工具给你一道题和一个分数，全库共 2712 行。"),
    # 规则19/20 的目标是"引用已被决策废弃的标识符"与"术语取值被写成两值"。
    # 两条都用**反向变异**：把修好的地方改回出错的样子。
    #
    # 规则19 的锚点必须选在**不含废弃标记**的行上：规则19 会跳过写着
    # 「删除 / 移出 / 废止」的行（那是记录废弃，不是失效引用）。第一版锚点选在
    # §5 的「不快照…`hits` 读 `attempts`」那句上，而那句本身含「删除」二字
    # —— 变异因此被跳过，自检报"校验器没有报错"，看起来像规则失效（实测撞到）。
    ("规则19 引用已被决策 28 移走的 evaluations.hits", "docs/v2数据模型.md",
     "评语 → 读 evaluations；hits → 读 attempts",
     "评语 → 读 evaluations；hits → 读 evaluations.hits"),
    ("规则20 命中状态被写成两值", "docs/v2数据模型.md",
     "JSON：`criterion_id → 命中状态`", "JSON：`criterion_id → 命中/未命中`"),
    # 规则21 的对象在 `app/` 里（见 RULE21_PROBE 的说明）——自检会临时造文件。
    ("规则21 JSON 列入库漏掉 ensure_ascii=False", "<probe>",
     RULE21_GOOD, RULE21_BAD),
]


def run_checker() -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, str(CHECKER)], capture_output=True, text=True, encoding="utf-8"
    )
    out = (r.stdout or "") + (r.stderr or "")
    return "FAIL" in out, out


def main(verbose: bool = False) -> int:
    caught, missed = 0, []
    for mut in MUTATIONS:
        name, rel, old, new = mut[0], mut[1], mut[2], mut[3]
        replace_all = len(mut) > 4 and mut[4]
        # 规则 21 的探测文件：造出来、写变异内容、跑校验器、删掉。
        # 它必须**每次都被清掉** —— 残留会让下一次 check_docs 报出一个假失败。
        if rel == "<probe>":
            try:
                RULE21_PROBE.write_text(new, encoding="utf-8", newline="\n")
                ok, _ = run_checker()
            finally:
                RULE21_PROBE.unlink(missing_ok=True)
            if ok:
                caught += 1
                if verbose:
                    print(f"  [抓到] {name}")
            else:
                missed.append((name, "校验器没有报错"))
            continue

        p = ROOT / rel
        if not p.exists():
            missed.append((name, f"文件不存在：{rel}"))
            continue
        origin = p.read_text(encoding="utf-8")
        if old not in origin:
            # 这条与"校验器没抓到"是两回事：说明变异本身写错了（锚点过期），
            # 而不是对应规则失效。混在一起会让我误判规则。
            missed.append((name, f"[变异写错了] 锚点找不到：{old[:30]}…"))
            continue
        if old == new:
            missed.append((name, "变异与原文相同，测试无效"))
            continue
        try:
            count = -1 if replace_all else 1
            p.write_text(origin.replace(old, new, count), encoding="utf-8", newline="\n")
            ok, _ = run_checker()
        finally:
            p.write_text(origin, encoding="utf-8", newline="\n")
        if ok:
            caught += 1
            if verbose:
                print(f"  [抓到] {name}")
        else:
            missed.append((name, "校验器没有报错"))

    # 还原后必须干净 —— 否则说明某次还原失败，仓库已被污染
    ok, out = run_checker()
    print(f"变异测试：{caught}/{len(MUTATIONS)} 条被抓到")
    if missed:
        print("\n未通过的变异（说明对应规则已失效或测试本身无效）：")
        for name, why in missed:
            print(f"  [漏掉] {name} —— {why}")
    # run_checker 的返回值含义是「校验器报了错」（ok=True 表示它拦住了）。
    # 还原之后我们希望它【不】报错 —— 所以判断的是 ok 为真即异常。
    # 这里原来写成 `if not ok:`，把"通过"误判成"被污染"（自检自己抓出来的）。
    if ok:
        print("\n⚠️ 还原后校验器仍报错 —— 某次变异没还原干净，仓库可能已被污染：")
        print(out.strip()[:800])
        return 1
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main(verbose="-v" in sys.argv))
