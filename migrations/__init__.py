"""迁移包（ADR-0011）。

**它是包（含本文件）**，否则 `python -m migrations.run` 会失败 ——
而"迁移跑不起来"会让整条装配链卡在第一步。

`_` 前缀的模块（`_runner.py`）是包内部件，不是迁移。
`*.sql` 才是迁移，一个文件一次 schema 变更。
"""

from migrations._runner import MigrationError, migrate, status

__all__ = ["MigrationError", "migrate", "status"]
