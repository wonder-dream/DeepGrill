"""验收标准的**执行器**：把「判据 : 怎么验」那张表真的跑一遍（决策 74）。

## 为什么它必须存在于仓库里

`docs/v2范围基线.md` 文末那张表已经给每条判据配了"怎么验"，但**表是死的**：
它不会告诉你现在过没过，也不会在有人把某条判据弄坏时出声。这个脚本做两件事：

1. 逐条**真的跑**（能自动化的那些）：跑对应的测试、查库、走一遍备份并当场验证
2. 把结果打成一张表，**没过的以非零退出码结束** —— 这样它可以直接进 CI 或
   发布前的检查清单

## 三种状态，语义必须分清

| 状态 | 含义 |
|---|---|
| `PASS` / `FAIL` | 自动判据的两种结果。`FAIL` 会让整个脚本以 1 退出 |
| `BLOCKED` | **前置条件还没满足**（例如知识层装配还没跑，所以"待定池为 0"必然不成立）。它不是失败 —— 是"这条现在验不了"，且原因打印出来 |
| `MANUAL` | 判据本身要求人做（真题 1065 走一遍、每季度恢复演练）。写成自动检查会让检查**变形** |

⚠️ `BLOCKED` 与 `MANUAL` **不影响退出码**：把"还没到那一步"算成失败，结果是所有人
学会忽略这个脚本。

用法：
    python scripts/check_acceptance.py            # 全跑
    python scripts/check_acceptance.py --list      # 只列判据，不跑
    python scripts/check_acceptance.py --only 备份  # 按关键词挑几条
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent

# ⚠️ **必须把仓库根塞进 `sys.path`**：从 `scripts/` 里直接跑 pytest 时，`sys.path[0]`
# 是 `scripts/` 而不是仓库根，于是 `migrations` / `tests` 这些**包外的顶层模块**
# 全部 import 不了 —— 表现是"34 个测试文件收集失败"，看起来像测试坏了，
# 而真因是这个脚本站在了错的目录上。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL, BLOCKED, MANUAL = "PASS", "FAIL", "BLOCKED", "MANUAL"

#: 图标只影响可读性，判据在 `status` 上
ICON = {PASS: "✅", FAIL: "❌", BLOCKED: "⏸", MANUAL: "👤"}


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    output: str = ""


@dataclass
class Check:
    """一条判据的"怎么验"。

    `kind`：
      · `tests`   —— 跑 pytest（`target` 是参数列表）
      · `fn`      —— 调一个函数，返回 `(status, detail)`
      · `manual`  —— 只打印要人做什么
    """

    name: str
    how: str
    kind: str
    target: object = None
    note: str = ""


# ---------------------------------------------------------------------------
# 自动判据的实现
# ---------------------------------------------------------------------------
def _run_pytest(args: list[str]) -> tuple[str, str, str]:
    """在**进程内**跑 pytest。返回 `(状态, 摘要, 完整输出)`。

    进程内而不是子进程：子进程要接管道，而本机对"程序捕获另一个程序的输出"有
    已知的权限坑（AGENTS §六）。`redirect_stdout` 同样能拿到全部输出。
    """
    import pytest

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = pytest.main(["-q", "-p", "no:cacheprovider", *args])
    text = buffer.getvalue()
    summary = next(
        (line.strip() for line in reversed(text.splitlines()) if "passed" in line or "failed" in line or "error" in line),
        "",
    )
    return (PASS if code == 0 else FAIL), summary, text


def _pending_pool() -> tuple[str, str]:
    """标准：全部题已按知识点归位（`primary_point_id` 不为空）。"""
    from app.bank import repository
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory

    db = Settings().resolved_database_path()
    if not db.exists():
        return BLOCKED, f"库还不存在（{db}）—— 先跑 python -m migrations.run"
    engine = create_db_engine(db)
    try:
        with create_session_factory(engine)() as session:
            pending = repository.unmounted_public_count(session)
    finally:
        engine.dispose()
    if pending == 0:
        return PASS, "待定池为空"
    return BLOCKED, (
        f"待定池还有 {pending} 道 —— 装配还没跑（配嵌入 → propose --batched → 人审 → mount）"
    )


def _calibrate_runs() -> tuple[str, str]:
    """标准：§11 的每个数字都有对应的实验脚本与结论（决策 70）。"""
    from app import cli

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(["calibrate"])
    text = buffer.getvalue()
    if code != 0:
        return FAIL, f"calibrate 退出码 {code}"
    missing = [t for t in ("①", "②", "③", "④", "⑤", "⑥") if t not in text]
    if missing:
        return FAIL, f"报告缺了 {'/'.join(missing)} 这几节"
    return PASS, "六节报告都出得来（`--live` 那节要真调用，不计入）"


def _backup_restorable() -> tuple[str, str]:
    """标准：备份**验证过能恢复**（ADR-0008）。

    它真的做一份备份再验证一遍 —— 这是唯一能让"备份可用"这句话有证据的方式。
    """
    import shutil

    from app import cli
    from app.config import Settings

    if not Settings().resolved_database_path().exists():
        return BLOCKED, "库还不存在，没有东西可备份"
    work = ROOT / ".tmp" / f"acceptance-{__import__('os').getpid()}"
    try:
        work.mkdir(parents=True, exist_ok=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            made = cli.main(["backup", "--dest", str(work), "--keep", "2"])
        files = sorted(work.glob("*.db.gz"))
        if made != 0 or not files:
            return FAIL, f"备份没做出来（退出码 {made}）"
        with contextlib.redirect_stdout(buffer):
            verified = cli.main(["verify-backup", str(files[-1])])
        if verified != 0:
            return FAIL, f"{files[-1].name} 恢复验证没过"
        return PASS, f"做了一份并当场验证可恢复（{files[-1].name}）"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 判据清单（顺序与基线文末那张表一致）
# ---------------------------------------------------------------------------
CHECKS: list[Check] = [
    Check(
        "三种形态都能完整跑通，共享同一面试官",
        "app/interview + app/web 的相关测试",
        "tests",
        ["app/interview/test_service.py", "app/web/test_interview_pages.py",
         "app/web/test_voice_page.py"],
    ),
    Check(
        "额度点扣减准确；耗尽后题库可用",
        "app/web/test_quota_degradation.py",
        "tests",
        ["app/web/test_quota_degradation.py"],
    ),
    Check("全部题已按知识点归位", "查库：待定池应为 0", "fn", _pending_pool),
    Check(
        "一道综合题同时更新多个知识点的掌握度",
        "app/knowledge/test_mastery.py + 人工用真题 1065 走一遍",
        "fn",
        lambda: (MANUAL, "自动部分见 app/knowledge/test_mastery.py；真题 1065 需人工走一遍"),
    ),
    Check(
        "「没考过」与「考了但没答」在矩阵上可区分",
        "app/knowledge/test_mastery.py（三值命中状态）",
        "tests",
        ["app/knowledge/test_mastery.py"],
    ),
    Check(
        "流式输出中断时数据不丢",
        "app/web 的面试页与语音页测试",
        "tests",
        ["app/web/test_interview_pages.py", "app/web/test_voice_page.py"],
    ),
    Check(
        "无进程内无界状态；无 async 上下文中的同步 DB 查询",
        "app/ratelimit 的 TTL 回收测试 + 人工自查 deps.py 的同步依赖约定",
        "tests",
        ["app/test_ratelimit.py"],
        note="async 那半条是人工判据 —— 写正则会让检查变形",
    ),
    Check(
        "所有降级都有用户可见状态 + 可查询记录",
        "面试页的「失败要看得见」测试 + 标定工具的失败路径",
        "tests",
        ["-k", "not_silent or visible or failure_is_printed", "app", "tests"],
    ),
    Check(
        "TLS 是部署文档里的前置条件",
        "tests/test_deploy_units.py（README 三个开关 + 反代）",
        "tests",
        ["tests/test_deploy_units.py"],
    ),
    Check(
        "带着占位口令的库拒绝启动（决策 58）",
        "app/db/test_constraints.py 的那一条",
        "tests",
        ["app/db/test_constraints.py", "-k", "placeholder"],
    ),
    Check(
        "§11 的每个数字都有实验脚本与结论",
        "python -m app.cli calibrate 出得来六节",
        "fn",
        _calibrate_runs,
    ),
    Check(
        "备份验证过能恢复，且演练过",
        "做一份 + verify-backup；每季度人工恢复一次",
        "fn",
        _backup_restorable,
        note="季度演练那一半是人工判据",
    ),
]


def run(check: Check) -> Result:
    if check.kind == "manual":
        return Result(check.name, MANUAL, str(check.target or ""))
    if check.kind == "fn":
        status, detail = check.target()  # type: ignore[operator]
        return Result(check.name, status, detail)
    status, summary, output = _run_pytest(list(check.target))  # type: ignore[arg-type]
    return Result(check.name, status, summary, output)


def render(results: list[Result]) -> str:
    width = max(len(r.name) for r in results)
    lines = ["验收标准逐条检查（决策 74）", ""]
    for r in results:
        lines.append(f"  {ICON[r.status]:<2} {r.name:<{width}}  {r.status:<7} {r.detail}")
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, BLOCKED, MANUAL)}
    lines.append("")
    lines.append(
        f"  {counts[PASS]} 过 / {counts[FAIL]} 未过 / {counts[BLOCKED]} 待前置 / {counts[MANUAL]} 人工"
    )
    if counts[FAIL]:
        lines.append("")
        lines.append("  未过的判据要先修 —— 它们是这份清单存在的理由。")
    if counts[BLOCKED]:
        lines.append("  待前置不是失败：它说的是「这一步还验不了」，原因写在上面的括号里。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/check_acceptance.py")
    parser.add_argument("--list", action="store_true", help="只列判据，不跑")
    parser.add_argument("--only", default="", help="只跑名字里含这个词的判据")
    parser.add_argument("-v", "--verbose", action="store_true", help="未过时打印完整输出")
    args = parser.parse_args(argv)

    selected = [c for c in CHECKS if args.only in c.name] if args.only else CHECKS
    if not selected:
        print(f"没有名字含「{args.only}」的判据")
        return 2
    if args.list:
        for c in selected:
            print(f"  {c.name}\n      怎么验：{c.how}")
            if c.note:
                print(f"      注：{c.note}")
        return 0

    results: list[Result] = []
    for check in selected:
        print(f"… {check.name}", flush=True)
        results.append(run(check))
    print()
    print(render(results))
    for r in results:
        if r.status == FAIL:
            print(f"\n===== {r.name} 的输出 =====")
            print(r.output[-4000:] if args.verbose else r.output[-800:])
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
