"""提交前检查：只查【这一次提交做了什么】。

`check_docs.py` 检查仓库的**当前状态**（链接在不在、状态行有没有、编号重不重）；
本脚本检查**提交这个动作本身** —— 它的输入是 `git diff --cached`，因此只在 pre-commit 里有意义。

它存在的理由（2026-09 的审计结论）：决策 33 被**就地反转**时，
`docs/adr/0004-frontend-is-server-rendered.md` 的旧描述漏改了，而当时 **312 项校验全部通过**。
原因不是检查不够多，而是没有一项检查看提交动作：
  · 台账那一行被就地改写 —— 编号不变、含义反转，所有「决策 33」的引用静默改义
  · 53 条决策里有 37 条是「没有 ADR、也没有影响文档清单」的实质行 —— 反转时无清单可用
  · 一次「消灭冗余」的提交删掉了「RTT 200-300ms」，而 ADR-0008 仍在指向它

四条规则，各对应一次真实事故或一处已验证的漏洞：
  A【阻断】台账编号只增不改 —— 就地改写会让所有引用静默改义
  B【提醒】新增、或**「状态」行有改动**的 ADR —— 它「影响文档」里列出的文件这次没碰
  C【提醒】从某份文档删掉了带数字的行，而某些 ADR 正指向那份文档
  D【提醒】已有 ADR 的正文改了、而「状态」行没动 —— 这是 B 原理上看不见的那一类

**B、C、D 只提醒不阻断** —— 它们给出的是「候选复查清单」，判断仍靠人。本项目的教训是
「一条满屏误报的规则比没有规则更糟 —— 它会被无视，连真问题一起放过」（见 `check_docs.py`）。
A 阻断，因为它的触发面极小，且有四种一行字的豁免。

规则 B 为什么以**状态行**为触发条件：状态行是「这条决策还算不算数」的唯一标记 ——
新增 = 从无到有，被取代 / 被修正 = 状态行会跟着改。只改 ADR 正文却不碰状态行，
说明这次改动不是决策层面的（改错别字、改字段名），不必惊动整张清单。

规则 D 正是 B 的补集：那个「不必惊动」的判断**由人来做，而不是由规则默默假设**。
决策层面的改动若没在状态行留痕（ADR-0002、ADR-0004 都留了），B 就永远不会为它触发。

**三条规则都是「diff 文本 → 判定」的纯函数**：读文件与读暂存区的活儿留在 `main()` 里，
这样 `selftest_check_staged.py` 能直接喂合成 diff 验证规则，不必造临时仓库。

用法：
    python scripts/check_staged.py          # 读 git 暂存区
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# 输出用 UTF-8 —— 否则 Windows 控制台（默认 GBK）会让脚本自己崩掉。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LEDGER = "docs/v2范围基线.md"
ADR_DIR = "docs/adr"
STATUS_PREFIX = "> 状态："


def status_line(text: str) -> str:
    """取文档头部那一行状态声明 —— 它是「这条决策还算不算数」的唯一标记。"""
    return next((l for l in text.split("\n") if l.startswith(STATUS_PREFIX)), "")


def _norm(path: str) -> str:
    """路径统一成 git 风格的正斜杠。

    不这么做的话，Windows 风格的路径（`os.path.join` / `glob` 返回的就是反斜杠）
    会让规则**静默地一条都不报** —— 看起来像「没问题」。这正是本项目反复吃过的
    「空转的检查」那一类，实测撞到过一次。
    """
    return path.replace("\\", "/")

# --- 规则 A 的豁免标记 --------------------------------------------------------
# 已有编号行的正文被改写时，正文里必须出现下面任一种「这是有意的」标记：
#   →        改为指向 ADR 的索引行 —— 把权威搬进 ADR，不是就地改义
#   曾为      把旧结论记在行内
#   废止      标记这条决策已失效
#   文字修订   纯措辞 / 错别字，含义未变
# 没有标记的就地改写一律拦下 —— 编号是公开指针，改正文等于让所有引用改义。
ROW_EXEMPT = ("→", "曾为", "废止", "文字修订")
ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|")

# --- 规则 C：什么算「一个可能被丢掉的数字」 -----------------------------------
# 只抓带量纲的，避免把「| id / name |」这种行也算进来（那会让提醒失去意义）。
QTY_RE = re.compile(
    r"\d[\d,.]*\s*(?:个|道|张|条|份|种|人|轮|次|字|ms|元|%|KB|MB|GB|天|小时|分钟|秒|倍)"
)


def git(*args: str) -> str:
    # 注意：**不能**清 GIT_* 环境变量 —— `git commit --only` 会准备一个临时索引并把
    # GIT_INDEX_FILE 交给钩子，清掉它就会去读真实索引、看到错的 diff。
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} 失败")
    return r.stdout


def _path_of(header: str) -> str | None:
    """从 diff 的 `+++ b/<path>` 头里取出仓库相对路径。"""
    p = header[4:].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p.strip('"')


def _rows(diff: str, path: str) -> tuple[dict[str, str], dict[str, str]]:
    """取出某个文件 diff 里被删/被加的台账编号行。"""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    cur: str | None = None
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            cur = _path_of(line)
            continue
        if cur != path or line[:1] not in "+-" or line.startswith(("---", "+++")):
            continue
        m = ROW_RE.match(line[1:])
        if m:
            (removed if line[0] == "-" else added)[m.group(1)] = line[1:].strip()
    return removed, added


def check_ledger(diff: str) -> list[str]:
    """规则 A：台账里已有编号行的正文被改写（且没有豁免标记）→ 失败。"""
    fails: list[str] = []
    removed, added = _rows(diff, LEDGER)
    for num, old in removed.items():
        new = added.get(num)
        if new is None:
            fails.append(
                f"决策 {num} 整行被删除 —— 编号是公开指针，废止它要**保留原行并标「废止」**，不能删"
            )
        elif new != old and not any(k in new for k in ROW_EXEMPT):
            fails.append(
                f"决策 {num} 的正文被就地改写（编号不变、含义可能已变）：\n"
                f"      旧：{old[:110]}\n"
                f"      新：{new[:110]}\n"
                f"      反转一条已落地的决策要：**新增一个编号写新结论 + 旧行标「废止」**；"
                f"只是措辞修订就在行内写「文字修订」"
            )
    return fails


def check_adr_sync(watched: list[str], staged: list[str], texts: dict[str, str]) -> list[str]:
    """规则 B：新增 / 状态行有改动的 ADR，其「影响文档」里列出却本次没碰的文件 → 提醒。

    已知的真实事故正是这个形状：ADR-0007 头部点了 `docs/v1行为规格.md` §4.10，
    而 §4.10 那次没有跟着改。

    `texts` 是这些 ADR 的**暂存区内容**（由调用方读，便于自检直接喂字符串）。
    """
    warns: list[str] = []
    touched = {_norm(f) for f in staged}
    texts = {_norm(k): v for k, v in texts.items()}
    for rel in sorted(
        f for f in map(_norm, watched)
        if f.startswith(ADR_DIR + "/") and f.endswith(".md")
    ):
        m = re.search(r"影响文档：(.+)", texts.get(rel, ""))
        if not m:
            continue
        missing = [r for r in re.findall(r"`([^`]+\.md)`", m.group(1)) if r not in touched]
        if missing:
            warns.append(
                f"{rel}（新增，或「状态」行有改动）在「影响文档」里列了 "
                f"{len(missing)} 个本次没碰的文件："
                + " · ".join(missing)
                + "（与那些文件无关就忽略本提醒）"
            )
    return warns


def check_adr_status_drift(drifted: list[str]) -> list[str]:
    """规则 D：已有 ADR 的正文被改动，但「状态」行没动 → 提醒。

    规则 B 以状态行为触发条件，所以这一类改动对它是**隐形**的。
    「这次改动算不算决策层面的」由人来判断 —— 但得先让人看见。
    """
    out: list[str] = []
    for rel in sorted(map(_norm, drifted)):
        if not (rel.startswith(ADR_DIR + "/") and rel.endswith(".md")):
            continue
        out.append(
            f"{rel} 的正文改了，但「状态」行没动 —— 若这是决策层面的改动"
            f"（结论变了 / 被取代 / 被修正），请在状态行注明（那是规则 B 的触发条件）；"
            f"只是改措辞或字段名就忽略本提醒"
        )
    return out


def reverse_index() -> dict[str, list[str]]:
    """文件 → 指向它的 ADR。这就是「谁在指我」的那张表。"""
    index: dict[str, list[str]] = {}
    adr_dir = Path(ADR_DIR)
    if not adr_dir.is_dir():
        return index
    for adr in sorted(adr_dir.glob("*.md")):
        m = re.search(r"影响文档：(.+)", adr.read_text(encoding="utf-8"))
        if not m:
            continue
        for ref in re.findall(r"`([^`]+\.md)`", m.group(1)):
            index.setdefault(ref, []).append(adr.name)
    return index


def check_deleted_numbers(diff: str, index: dict[str, list[str]]) -> list[str]:
    """规则 C：从某份文档删掉了带数字的行，而某些 ADR 正指向那份文档 → 提醒。"""
    dropped: dict[str, list[str]] = {}
    cur: str | None = None
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            cur = _path_of(line)
            continue
        if cur is None or not cur.endswith(".md") or line[:1] != "-":
            continue
        if line.startswith("---") or not QTY_RE.search(line[1:]):
            continue
        # 台账的编号行归规则 A 管 —— 同一次编辑被两条规则各报一遍，
        # 只会训练人无视提醒（这正是「误报毁掉规则」的另一种形态）。
        if cur == LEDGER and ROW_RE.match(line[1:]):
            continue
        dropped.setdefault(cur, []).append(line[1:].strip())

    warns: list[str] = []
    for f, lines in dropped.items():
        who = index.get(f)
        if not who:
            continue
        names = "、".join(who[:5]) + (f" 等 {len(who)} 个" if len(who) > 5 else "")
        warns.append(
            f"{f} 删掉了 {len(lines)} 行带数字的内容（例：{lines[0][:70]}）—— "
            f"这些 ADR 指向该文件：{names}。确认那个数字没有别的家"
        )
    return warns


def main() -> int:
    try:
        root = git("rev-parse", "--show-toplevel").strip()
    except RuntimeError as e:
        print(f"check_staged：不在 git 仓库里，跳过（{e}）")
        return 0
    os.chdir(root)

    files = [x for x in git("diff", "--cached", "--name-only").split("\n") if x.strip()]
    if not files:
        print("check_staged：暂存区为空，跳过")
        return 0

    diff = git("diff", "--cached", "-U0")

    # 规则 B 的触发集合：新增的 ADR，或「状态」行与 HEAD 不同的 ADR。
    # 规则 D 的触发集合：已有 ADR 的正文改了、而状态行没动（B 的补集）。
    texts: dict[str, str] = {}
    watched: list[str] = []
    drifted: list[str] = []
    for rel in [f for f in files if f.startswith(ADR_DIR + "/") and f.endswith(".md")]:
        try:
            texts[rel] = git("show", f":{rel}")
        except RuntimeError:
            continue
        try:
            head = git("show", f"HEAD:{rel}")
        except RuntimeError:
            head = ""  # 新增文件：HEAD 里没有
        if status_line(texts[rel]) != status_line(head):
            watched.append(rel)
        elif head:
            drifted.append(rel)

    fails = check_ledger(diff)
    warns = (
        check_adr_sync(watched, files, texts)
        + check_adr_status_drift(drifted)
        + check_deleted_numbers(diff, reverse_index())
    )

    if warns:
        print(f"check_staged：{len(warns)} 条提醒（不阻断，请自己判断）")
        for w in warns:
            print(f"  [提醒] {w}")
        print()

    if fails:
        print(f"check_staged：{len(fails)} 项未通过（共 1 条阻断规则）\n")
        for f in fails:
            print(f"  [FAIL] {f}")
        return 1

    print("check_staged 通过（台账编号只增不改）" + ("；上面有提醒" if warns else "；无提醒"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
