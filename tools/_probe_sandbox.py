"""沙箱行为探针：哪些目录操作被允许。用于给 pytest 配一个能用的临时目录。"""

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def try_it(label: str, fn) -> None:
    try:
        r = fn()
        print(f"  [OK]   {label} -> {r}")
    except Exception as e:
        print(f"  [DENY] {label} -> {type(e).__name__}: {e}")


print(f"cwd = {os.getcwd()}")
print(f"TMP = {os.environ.get('TMP')}")
print(f"tempfile.gettempdir() = {tempfile.gettempdir()}")

for name in (".pytest-tmp", ".tmp", ".scratch", ".tmp/pytest", ".tmpx", ".tmpx/pytest"):
    d = ROOT / name
    print(f"\n--- {name} ---")
    try_it("mkdir", lambda d=d: (d.mkdir(parents=True, exist_ok=True), "ok")[1])
    try_it("scandir", lambda d=d: len(list(os.scandir(d))))
    try_it("iterdir", lambda d=d: len(list(d.iterdir())))
    try_it("递归 scandir（pytest 的 rm_rf 走这条）", lambda d=d: len(list(d.iterdir())))
    f = d / "x.txt"
    try_it("写文件", lambda f=f: (f.write_text("hi", encoding="utf-8"), "ok")[1])
    try_it("读文件", lambda f=f: f.read_text(encoding="utf-8"))
    try_it("删文件", lambda f=f: (f.unlink(), "ok")[1])

print("\n--- 对照：仓库根本身 ---")
try_it("scandir(仓库根)", lambda: len(list(os.scandir(ROOT))))
try_it("iterdir(仓库根)", lambda: len(list(ROOT.iterdir())))
print("\n--- 对照：系统临时目录 ---")
t = Path(tempfile.gettempdir())
try_it("scandir(系统 temp)", lambda: len(list(os.scandir(t))))
