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

import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_docs.py"

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
    ("规则12 台账标题范围与实际不符", "docs/v2范围基线.md", "（1–56）", "（1–50）"),
    ("规则13 accepted 的 ADR 缺 Consequences", "docs/adr/0006-offline-jobs-in-sql-with-a-worker.md", "## Consequences", "## 附注"),
    ("规则15 引用 ADR 缺 .md 后缀", "CONTEXT.md", "docs/adr/0005-code-is-split-by-domain.md", "docs/adr/0005"),
    ("规则16 AGENTS 未提 INDEX", "AGENTS.md", "`docs/INDEX.md`", "`docs/INDEX`", True),
    ("规则17 用对话轮次引用决策", "docs/v1行为规格.md", "（决策 28）", "（第 28 轮）"),
    ("规则18 实测数字无出处", "README.md",
     "本仓库目前包含的是重写前的完整设计文档：",
     "本仓库目前包含的是重写前的完整设计文档，共 2712 行："),
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
