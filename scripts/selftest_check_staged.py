"""`check_staged.py` 的自检：证明三条规则不是空转。

一套全是空转的断言也会「全部通过」—— 这是 `selftest_check_docs.py` 已经记下的教训。
这里用同样的办法（合成输入 → 断言判定），但**不碰文件系统**：三条规则都是
「diff 文本 → 判定」的纯函数，直接喂合成 diff 就够了。

> 为什么不用「临时 git 仓库」那种做法：第一版就是那么写的，结果在受限环境里
> 因为写不了系统临时目录而整个自检失效 —— **一个自己会因环境失败的自检等于没有自检**。

每条用例覆盖一条规则的一种行为，其中**四条是反向用例**（不该报的必须不报）：
误报比漏报更容易毁掉一条规则，见 `check_docs.py` 里那条「108 项误报」的注释。

用法：
    python scripts/selftest_check_staged.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.dont_write_bytecode = True  # 别在仓库里留下 __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_staged as cs  # noqa: E402  （必须先把它所在目录插进 sys.path）

LEDGER = cs.LEDGER
OTHER = "docs/v2数据模型.md"
ADR = "docs/adr/0001-fake.md"
ADR_TEXT = "> 状态：accepted ｜ 影响文档：`docs/v2范围基线.md`\n"

# commit 4d83638 那一次「决策 33 被就地反转」的**逐字原文**（从 git 里取的，不要改写成概括）。
# 拿它做重放回归：规则 A 必须拦下这种改写 —— 这是整个 check_staged 存在的理由。
ROW33_BEFORE = (
    "| 33 | 语音输入的三个连带规定：**① 转写结果先给用户确认/编辑再提交**（STT 会认错技术术语）"
    "**② 不保存原始录音**（只存转写文本）**③ 保留口语特征**，但报告要标明本场是语音模式 | "
    "①避免术语识别错误直接进判分；②录音是敏感数据且体积大；③口语停顿与措辞是「表达清晰度」与追问策略的真实信号 |"
)
ROW33_AFTER = (
    "| 33 | 语音输入的三个连带规定：**① 不设「确认」环节** —— 转写直接进判分，"
    "术语识别错误由 STT 模型与 LLM 的质量解决 **② 不保存原始录音**（只存转写文本）"
    "**③ 保留口语特征**，但报告要标明本场是语音模式 | "
    "①要求确认等于把语音省下的打字成本又还回去：它是**每轮都付**的固定成本，"
    "而识别错误是**偶发**、且下游模型本就能读通；②录音是敏感数据且体积大；"
    "③口语停顿与措辞是「表达清晰度」与追问策略的真实信号 |"
)


def diff_of(path: str, removed: list[str], added: list[str]) -> str:
    """拼一个最小但字段正确的 unified diff（-U0）。"""
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1,0 +1,0 @@"]
    lines += [f"-{x}" for x in removed]
    lines += [f"+{x}" for x in added]
    return "\n".join(lines) + "\n"


def ledger_case(removed: list[str], added: list[str], path: str = LEDGER) -> tuple[bool, str]:
    fails = cs.check_ledger(diff_of(path, removed, added))
    return bool(fails), "\n".join(fails)


def adr_case(watched: list[str], staged: list[str], texts: dict[str, str]) -> tuple[bool, str]:
    warns = cs.check_adr_sync(watched, staged, texts)
    return bool(warns), "\n".join(warns)


def drift_case(drifted: list[str]) -> tuple[bool, str]:
    warns = cs.check_adr_status_drift(drifted)
    return bool(warns), "\n".join(warns)


def deleted_case(removed: list[str], index: dict[str, list[str]], path: str = LEDGER) -> tuple[bool, str]:
    warns = cs.check_deleted_numbers(diff_of(path, removed, []), index)
    return bool(warns), "\n".join(warns)


def case(name: str, result: tuple[bool, str], expect_hit: bool, keyword: str | None = None):
    return (name, result[0], result[1], expect_hit, keyword)


CASES = [
    # --- 规则 A：台账编号只增不改 ---
    case("规则A 重放 4d83638：决策 33 被就地反转（真实事故原文）",
         ledger_case([ROW33_BEFORE], [ROW33_AFTER]),
         True, "就地改写"),
    case("规则A 就地改写（编号不变、含义已变）",
         ledger_case(["| 1 | 决策一 | 理由一 |"], ["| 1 | 决策一反转 | 理由一 |"]),
         True, "就地改写"),
    case("规则A 改为 → ADR 指针（豁免：权威搬进 ADR）",
         ledger_case(["| 1 | 决策一 | 理由一 |"], ["| 1 | → **ADR-0001**（决策一） | 理由一 |"]),
         False),
    case("规则A 行内标「曾为」（豁免）",
         ledger_case(["| 1 | 决策一 | 理由一 |"], ["| 1 | 决策一（曾为：旧写法） | 理由一 |"]),
         False),
    case("规则A 行内标「文字修订」（豁免）",
         ledger_case(["| 1 | 决策一 | 理由一 |"], ["| 1 | 决策一（文字修订） | 理由一 |"]),
         False),
    case("规则A 追加新编号行（应放行）",
         ledger_case([], ["| 3 | 决策三 | 理由三 |"]),
         False),
    case("规则A 删除编号行（指针会悬空）",
         ledger_case(["| 2 | 决策二 | 理由二 |"], []),
         True, "整行被删除"),
    case("规则A 别的文件里的编号行不受影响（作用域）",
         ledger_case(["| 1 | 决策一 | 理由一 |"], ["| 1 | 决策一反转 | 理由一 |"], path=OTHER),
         False),
    # --- 规则 B：新增 / 状态行改动的 ADR 的影响文档须同步 ---
    case("规则B 新增 ADR、其影响文档本次没碰（提醒）",
         adr_case([ADR], [ADR], {ADR: ADR_TEXT}),
         True, "影响文档"),
    case("规则B 新增 ADR、影响文档这次也碰了（不该报）",
         adr_case([ADR], [ADR, LEDGER], {ADR: ADR_TEXT}),
         False),
    case("规则B ADR 改了但状态行没动（不是决策层面的改动，不该报）",
         adr_case([], [ADR], {ADR: ADR_TEXT}),
         False),
    case("规则B 反斜杠路径也要生效（不能静默不报）",
         adr_case([ADR.replace("/", "\\")], [ADR], {ADR: ADR_TEXT}),
         True, "影响文档"),
    case("状态行提取：只取以「> 状态：」开头的那一行",
         (bool(cs.status_line(ADR_TEXT)), cs.status_line(ADR_TEXT)),
         True, "accepted"),
    # --- 规则 D：ADR 正文改了但状态行没动（规则 B 看不见的那一类）---
    case("规则D ADR 正文改了、状态行没动（提醒）",
         drift_case([ADR]),
         True, "状态"),
    case("规则D 非 ADR 路径不受影响（作用域）",
         drift_case([LEDGER]),
         False),
    # --- 规则 C：删掉带数字的行 —— 谁在指我 ---
    case("规则C 删掉带量纲数字的行且 ADR 指向该文件（提醒）",
         deleted_case(["- 境内直连美东 RTT 200-300ms"], {LEDGER: ["0001-fake.md"]}),
         True, "删掉了"),
    case("规则C 删掉的行没有量纲数字（不该报）",
         deleted_case(["- 详见第 2 节"], {LEDGER: ["0001-fake.md"]}),
         False),
    case("规则C 没有 ADR 指向该文件（不该报）",
         deleted_case(["- 境内直连美东 RTT 200-300ms"], {}),
         False),
    case("规则C 台账编号行归规则A管（不该重复报）",
         deleted_case(["| 5 | 语音输出暂缓（成本 10-30 倍） | 理由 |"], {LEDGER: ["0001-fake.md"]}),
         False),
]


def main() -> int:
    caught, missed = 0, []
    for name, hit, text, expect_hit, keyword in CASES:
        if hit != expect_hit:
            missed.append((name, f"期望{'命中' if expect_hit else '不命中'}，实际"
                                 f"{'命中' if hit else '不命中'}：{text.strip()[:200]}"))
        elif keyword and keyword not in text:
            missed.append((name, f"判定文本里没有「{keyword}」：{text.strip()[:200]}"))
        else:
            caught += 1
            print(f"  [符合预期] {name}")

    print(f"\ncheck_staged 自检：{caught}/{len(CASES)} 条符合预期")
    if missed:
        print("\n未通过（说明对应规则已失效，或用例本身写错了）：")
        for name, why in missed:
            print(f"  [漏掉] {name} —— {why}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
