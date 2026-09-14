"""迁移入口：`python -m migrations.run`

    python -m migrations.run           跑所有待执行的迁移
    python -m migrations.run --check   只报告状态，不改库
"""

from __future__ import annotations

import sys

from migrations._runner import main

if hasattr(sys.stdout, "reconfigure"):
    # Windows 控制台默认 GBK，迁移文件名与错误信息里的非 ASCII 会让脚本自己崩掉。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

if __name__ == "__main__":
    raise SystemExit(main())
