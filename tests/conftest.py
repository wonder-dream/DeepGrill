"""跨领域测试的共享 fixture。

**这一层为什么存在**（ADR-0010）：pytest 会从领域测试向上找到根 conftest，
所以 `app/<领域>/test_*.py` 与 `tests/` 用的是**同一份** test double，
而不是每个领域各带一份要同步的替身。

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
# `pytest-of-<用户>`，那一步被拒（WinError 5）。v1 的 471 个测试"写完却跑不起来"
# 就是同一类问题（247 个用例报 PermissionError，当时被记成"非代码失败"）。
#
# 所以 `pyproject.toml` 关掉了 tmpdir 插件（`-p no:tmpdir`），改用下面这个。
_TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp"
_counter = itertools.count()


@pytest.fixture
def tmp_dir() -> Iterator[Path]:
    """给一个空的可写目录，测试结束删掉。

    比 `tmp_path` 多做的一件事：**失败时保留现场**（`DEEPGRILL_KEEP_TMP=1` 时），
    因为"迁移跑到一半失败"这类问题只有看到现场才有用。

    ⚠️ `exist_ok=True` 不是随手加的：**本环境下清理不可靠** ——
    `shutil.rmtree` 会把权限出问题的目录留在磁盘上（AGENTS.md §六 第 3 条记的
    同一类现象），于是下一次同名目录已经存在，`mkdir()` 直接 `FileExistsError`。
    实测撞过一次（`t9596-0`）：失败现场看起来像"测试框架坏了"，真因是上一条
    测试的残留。所以：**名字撞了就清掉重用，清不掉也照样用**。
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
