"""最小复现：Python 创建的子目录能不能立刻被 scandir / 写文件。"""

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def probe(label: str, d: Path) -> None:
    try:
        d.mkdir(parents=True, exist_ok=True)
        mk = "OK"
    except Exception as e:
        print(f"{label}: mkdir 就失败 {type(e).__name__}: {e}")
        return
    out = []
    for what, fn in (
        ("scandir", lambda: len(list(os.scandir(d)))),
        ("iterdir", lambda: len(list(d.iterdir()))),
        ("写文件", lambda: (d / "x").write_text("hi", encoding="utf-8") and "ok"),
        ("读文件", lambda: (d / "x").read_text(encoding="utf-8")),
    ):
        try:
            out.append(f"{what}=OK")
            fn()
        except Exception as e:
            out[-1] = f"{what}=拒绝({type(e).__name__})"
    print(f"{label}: mkdir={mk}  " + "  ".join(out))


print("--- 全新名字，一次到位 ---")
for name in (".rt1", ".rt1/sub", ".rt2", ".rt3/sub/deep"):
    probe(name, ROOT / name)

print("\n--- 清理 ---")
for name in (".rt1", ".rt2", ".rt3"):
    shutil.rmtree(ROOT / name, ignore_errors=True)
    print(f"  removed {name}")
