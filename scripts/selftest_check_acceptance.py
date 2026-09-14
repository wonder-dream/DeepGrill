"""验收执行器的自检：证明它**会报错**（决策 74）。

`check_acceptance.py` 全部 PASS 本身不能说明它有效 —— 一个永远返回 PASS 的执行器
也会"全部通过"，而它比没有执行器更糟（它会让人相信验收过了）。所以这里用变异证明：

1. **健康的判据失败时它必须报 FAIL 并以非零退出**（把一个判据指向不存在的测试）
2. 把 `_run_pytest` 改成永远返回 PASS → 上一条就抓不住了（说明第 1 条测的是真信号）
3. 把退出码改成恒 0 → 同样必须被抓住
4. `_pending_pool` 真的在**读库**，不是写死的：造一个"有待定题"的库，它必须报 BLOCKED；
   把判据改成恒 PASS → 必须被抓住

用法：
    python scripts/selftest_check_acceptance.py
    python scripts/selftest_check_acceptance.py -v
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_acceptance.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 把判据指向一个**不存在的测试选择器** —— pytest 会以 5（no tests ran）退出
BROKEN = '["app/db/test_constraints.py", "-k", "placeholder"]'
NO_MATCH = '["app/db/test_constraints.py", "-k", "zzz_no_such_test"]'

ALWAYS_PASS = "    return (PASS if code == 0 else FAIL), summary, text"
ALWAYS_PASS_MUT = "    return PASS, summary, text"

EXIT = "    return 1 if any(r.status == FAIL for r in results) else 0"
EXIT_MUT = "    return 0"

PENDING = "    if pending == 0:"
PENDING_MUT = "    if True:"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_acceptance_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # ⚠️ 必须先注册进 `sys.modules`：`@dataclass` 会去 `sys.modules[cls.__module__]`
    # 里找命名空间，不注册就是 `AttributeError: 'NoneType' object has no attribute
    # '__dict__'` —— 报错点离真因很远（实测为此排查了一轮）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_script(*args: str) -> int:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
    )
    return proc.returncode


def _with_mutation(old: str, new: str, body):
    original = SCRIPT.read_text(encoding="utf-8")
    assert original.count(old) == 1, f"变异锚点定位失败：{old[:60]!r}"
    try:
        SCRIPT.write_text(original.replace(old, new), encoding="utf-8")
        return body()
    finally:
        SCRIPT.write_text(original, encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. 真的读库
# ---------------------------------------------------------------------------
def _pending_pool_reads_the_db() -> tuple[bool, str]:
    """造两个库：一个有待定题（该 BLOCKED）、一个没有（该 PASS）。"""
    work = ROOT / ".tmp" / f"selftest-acceptance-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    module = _load_module()
    old_env = os.environ.get("DEEPGRILL_DATABASE_PATH")
    try:
        from app.db import create_db_engine, create_session_factory
        from app.db.models import Domain, KnowledgePoint, Question
        from migrations._runner import migrate

        db = work / "acceptance.db"
        migrate(db)
        with create_session_factory(create_db_engine(db))() as session:
            session.add(Question(kind="knowledge", stem="一道还没挂点的题", difficulty=3,
                                 origin="seed"))
            session.commit()
        os.environ["DEEPGRILL_DATABASE_PATH"] = str(db)
        status, detail = module._pending_pool()
        if status != module.BLOCKED:
            return False, f"有待定题时应当 BLOCKED，实际 {status}（{detail}）"

        # 挂上点之后应当 PASS
        with create_session_factory(create_db_engine(db))() as session:
            session.add(Domain(id=1, name="D"))
            session.flush()
            session.add(KnowledgePoint(id=1, domain_id=1, name="P", status="confirmed"))
            session.flush()
            row = session.query(Question).one()
            row.primary_point_id = 1
            session.commit()
        status, detail = module._pending_pool()
        if status != module.PASS:
            return False, f"没有待定题时应当 PASS，实际 {status}（{detail}）"
        return True, "有待定题 → BLOCKED；挂上点 → PASS"
    finally:
        if old_env is None:
            os.environ.pop("DEEPGRILL_DATABASE_PATH", None)
        else:
            os.environ["DEEPGRILL_DATABASE_PATH"] = old_env
        shutil.rmtree(work, ignore_errors=True)


def main(verbose: bool = False) -> int:
    caught = 0
    missed: list[tuple[str, str]] = []

    def check(name: str, ok: bool, why: str = "") -> None:
        nonlocal caught
        if ok:
            caught += 1
            print(f"  [抓到] {name}")
        else:
            missed.append((name, why))
            print(f"  [漏掉] {name} —— {why}")

    print("验收执行器自检：先确认它健康时会报错，再确认变异会让它失去这个能力")

    # ① 健康的判据失败 → 必须报错（否则这个执行器是空转）
    code = _with_mutation(BROKEN, NO_MATCH, lambda: _run_script("--only", "占位"))
    check("判据失败时以非零退出", code != 0, f"退出码 {code}（应为非零）")

    # ② 同样的坏判据，但执行器被改成永远 PASS → 它就该抓不住了（说明 ① 测的是真信号）
    code = _with_mutation(
        ALWAYS_PASS,
        ALWAYS_PASS_MUT,
        lambda: _with_mutation(BROKEN, NO_MATCH, lambda: _run_script("--only", "占位")),
    )
    check("变异：_run_pytest 永远返回 PASS（① 因此失效）", code == 0, f"退出码 {code}")

    # ③ 退出码被改成恒 0
    code = _with_mutation(
        EXIT, EXIT_MUT, lambda: _with_mutation(BROKEN, NO_MATCH, lambda: _run_script("--only", "占位"))
    )
    check("变异：退出码恒为 0（① 因此失效）", code == 0, f"退出码 {code}")

    # ④ 判据真的在读库
    ok, why = _pending_pool_reads_the_db()
    check("_pending_pool 真的读库", ok, why)

    # 把"有待定题就 BLOCKED"改成恒 PASS → ④ 必须因此失效
    original = SCRIPT.read_text(encoding="utf-8")
    try:
        SCRIPT.write_text(original.replace(PENDING, PENDING_MUT), encoding="utf-8")
        ok, why = _pending_pool_reads_the_db()
        check("变异：待定池判据恒 PASS（④ 因此失效）", not ok, why)
    finally:
        SCRIPT.write_text(original, encoding="utf-8")

    # ⑤ 健康状态下它必须真的全过（否则上面几条都是废话）
    code = _run_script("--only", "占位")
    check("健康状态下这一条判据过", code == 0, f"退出码 {code}")

    total = caught + len(missed)
    print(f"\n自检：{caught}/{total} 条符合预期")
    if missed:
        print("\n不符合预期的：")
        for name, why in missed:
            print(f"  {name} —— {why}")
    if verbose:
        print("\n（-v 只影响是否打印中间输出）")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main(verbose="-v" in sys.argv))
