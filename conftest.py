"""仓库根的共享 fixture。

**为什么在根而不是 `tests/`**（ADR-0010）：pytest 会从任一测试文件向上找到
根 conftest，所以 `app/<领域>/test_*.py`、`migrations/test_runner.py` 与
`tests/` 用的是**同一份** fixture，而不是各自带一份要同步的副本。
这条是实测撞出来的：`tmp_dir` 一开始放在 `tests/conftest.py`，而
`migrations/test_runner.py` 找不到它（`fixture 'tmp_dir' not found`）。

fixture 的具体清单**遵循"由测试决定"**：第一个需要它的测试写出来时才加，
不预先列一份猜测的清单（那会过期，而"文档说有、代码里没有"正是本项目
花了几轮在消灭的东西）。
"""

from __future__ import annotations

import itertools
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

# 临时目录放在仓库内，**不用系统的**。
#
# 这是实测的结论，不是偏好：这个环境下 pytest 自带的 `tmp_path` / `tmpdir`
# 必然失败 —— 它启动时要 `scandir` 系统临时目录里自己上次留下的
# `pytest-of-<用户>`，那一步被拒（WinError 5）。把 basetemp 指到工作区内也
# 没用：跑完那个目录就变得不可读，删掉重建照样被拒。
# v1 的 471 个测试"写完却跑不起来"是同一类问题（247 个用例报
# PermissionError，当时被记成"非代码失败"）。
#
# 所以 `pyproject.toml` 关掉了 tmpdir 插件（`-p no:tmpdir`），改用下面这个。
_TMP_ROOT = Path(__file__).resolve().parent / ".tmp"
_counter = itertools.count()


@pytest.fixture
def tmp_dir() -> Iterator[Path]:
    """给一个空的可写目录，测试结束删掉。

    留了 `DEEPGRILL_KEEP_TMP=1` 这个开关：排查"迁移跑到一半失败"这类问题时，
    现场比日志有用。

    ⚠️ `exist_ok=True` 不是随手加的：**本环境下清理不可靠** ——`shutil.rmtree`
    会把权限出问题的目录留在磁盘上（AGENTS.md §六 第 3 条记的同一类现象），
    于是下一次同名目录已经存在，`mkdir()` 直接 `FileExistsError`。实测撞过一次，
    失败现场看起来像"测试框架坏了"，真因是上一条测试的残留。
    """
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    d = _TMP_ROOT / f"t{os.getpid()}-{next(_counter)}"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        if not os.environ.get("DEEPGRILL_KEEP_TMP"):
            shutil.rmtree(d, ignore_errors=True)
