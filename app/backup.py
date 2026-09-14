"""备份与**恢复验证**（ADR-0008）。

> 备份**必须验证能恢复** —— 数据库与机器同生共死，是当前最大单点风险
> （v1 发生过一次整库覆盖冲掉 users）

ADR-0008 同时写明"本决策不覆盖备份的具体做法（频率 / 保留策略 / 恢复演练 /
告警渠道）—— 待定"。这一版把**能确定的那部分**做完：

```
snapshot()      用 SQLite 自己的 `VACUUM INTO` 做一致性快照 → gzip
verify()        解压 → integrity_check → 表齐不齐 → 行数对不对 → schema 落后没
backup()        上面两步 + 保留策略 + 落一条 task_logs 报告（观测页看得见）
```

## 三条判断

① **用 `VACUUM INTO`，不用 `cp`**：库在 WAL 模式下运行时直接拷文件会得到
   "主库 + 半个 wal" 的组合 —— 那份副本**看起来是好的**，恢复的时候才发现不对。
   `VACUUM INTO` 由 SQLite 自己保证一致性快照，而且它顺带做了整理（体积也小）。

② **备份命令自己验一遍**（`backup()` = snapshot + verify）。"备份成功"但恢复不了，
   是这类工具里最难发现的失败：它在**需要它的那一天**才暴露。所以任何一次
   `backup` 的退出码就是"这份备份现在能不能恢复"的答案（cron 因此会报警）。

③ **不引云 SDK**：异地那一跳交给部署侧的 `rclone` / `aws-cli` + cron（ADR-0008 的
   "定时到对象存储"）。在代码里绑一个云厂商，等于把"备份"与"某家的 API key"绑死，
   而这一层的价值全在"它一定能跑"。

## 恢复演练的做法

`verify()` 打开的是**备份本身**（在临时文件里），不碰生产库。它检查四件事：
SQLite 自己的完整性、代码要用的表齐不齐、**行数与备份时的清单是否一致**、
迁移记账是否完整。最后一件会抓住"备份比代码旧"这种最常见的恢复失败。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import TaskLog
from app.db.startup import missing_tables

#: 备份目录的默认位置（相对仓库根由调用方给）。`data/` 已被 .gitignore 挡住。
DEFAULT_KEEP = 14

#: `task_logs.kind` —— 观测页按它找"最近一次备份"。
KIND = "backup"


@dataclass
class Manifest:
    """备份时写下的清单。**verify 拿它做行数对账** —— 它是"当时有多少行"的唯一记录。"""

    created_at: str
    database: str
    size_bytes: int
    sha256: str
    tables: dict[str, int] = field(default_factory=dict)
    migrations: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "created_at": self.created_at,
                "database": self.database,
                "size_bytes": self.size_bytes,
                "sha256": self.sha256,
                "tables": self.tables,
                "migrations": self.migrations,
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: object) -> Manifest:
        data = raw if isinstance(raw, dict) else {}
        tables = data.get("tables") or {}
        return cls(
            created_at=str(data.get("created_at") or ""),
            database=str(data.get("database") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            sha256=str(data.get("sha256") or ""),
            tables={str(k): int(v) for k, v in tables.items()},
            migrations=[str(m) for m in (data.get("migrations") or [])],
        )


@dataclass
class VerifyReport:
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        return [why for _name, passed, why in self.checks if not passed]

    def summary(self) -> str:
        if self.ok:
            return "这份备份可以恢复（完整性 / 表齐 / 行数 / 迁移记账都对得上）"
        return "这份备份**不能**保证可恢复：" + "；".join(self.failures)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integrity_verdict(db: Path) -> str:
    """跑一次 `PRAGMA integrity_check`，返回它给出的结论（正常是 `"ok"`）。

    单独成一个函数是为了**留一个缝**：真实的页级损坏很难在测试里稳定造出来
    （截断通常会让库直接打不开，走的是另一条错误路径），所以"返回一个非 ok 的结论"
    这件事得能被显式注入 —— 被测的是那行判断，不是 SQLite 的行为。
    """
    conn = sqlite3.connect(str(db))
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _table_counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db))
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                for name in sorted(names)}
    finally:
        conn.close()


def _applied_migrations(db: Path) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        try:
            rows = conn.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def snapshot(db_path: Path, dest_dir: Path) -> tuple[Path, Manifest]:
    """做一份一致性快照（`VACUUM INTO` + gzip），返回 `(压缩包, 清单)`。

    ⚠️ **不 `cp`**：WAL 模式下直接拷文件会漏掉尚未 checkpoint 的那部分，
    而副本**看起来是好的**（见模块 docstring 的判断①）。
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"没有库文件：{db_path} —— 先跑 python -m migrations.run")
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "snapshot.db"
        # `VACUUM INTO` 不能在事务里跑，所以这里直接用 sqlite3 连（不经过 SQLAlchemy 的会话）
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM INTO ?", (str(raw),))
        finally:
            conn.close()

        counts = _table_counts(raw)
        migrations = _applied_migrations(raw)

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        archive = dest_dir / f"deepgrill-{stamp}.db.gz"
        with raw.open("rb") as src, gzip.open(archive, "wb") as dst:
            shutil.copyfileobj(src, dst)

    manifest = Manifest(
        created_at=_now(),
        database=db_path.name,
        size_bytes=archive.stat().st_size,
        sha256=_sha256(archive),
        tables=counts,
        migrations=migrations,
    )
    archive.with_suffix(archive.suffix + ".json").write_text(
        manifest.to_json(), encoding="utf-8"
    )
    return archive, manifest


def verify(archive: Path, *, manifest: Manifest | None = None) -> VerifyReport:
    """**恢复验证**：这份备份现在能不能恢复。四项检查，都不碰生产库。

    每条检查都带一句**两种读法都成立**的话（通过时说"一致/都在"，失败时说清差在哪）——
    只写失败文案会让成功路径的输出看起来像在报错（第一版就是这样，实测一眼看去
    像六个错误）。
    """
    report = VerifyReport(ok=False)

    if manifest is None:
        sidecar = archive.with_suffix(archive.suffix + ".json")
        if sidecar.is_file():
            manifest = Manifest.from_json(json.loads(sidecar.read_text(encoding="utf-8")))
    if not archive.is_file():
        report.checks.append(("exists", False, f"备份文件不存在：{archive}"))
        return report
    report.checks.append(("exists", True, "文件在"))

    if manifest is not None and manifest.sha256:
        actual = _sha256(archive)
        same = actual == manifest.sha256
        report.checks.append(
            (
                "checksum",
                same,
                f"校验和一致（{actual[:12]}…）"
                if same
                else f"文件内容与清单不符（{actual[:12]}… != {manifest.sha256[:12]}…）"
                f"—— 它可能被截断或改过",
            )
        )
    else:
        report.checks.append(("checksum", True, "没有清单，跳过校验和"))

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "restore.db"
        try:
            with gzip.open(archive, "rb") as src, raw.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        except (OSError, EOFError) as e:
            report.checks.append(("unpack", False, f"解压失败：{e}"))
            return _finish(report)
        report.checks.append(("unpack", True, "能解压"))

        try:
            verdict = _integrity_verdict(raw)
        except sqlite3.DatabaseError as e:
            report.checks.append(("integrity", False, f"打不开：{e}"))
            return _finish(report)
        report.checks.append(
            (
                "integrity",
                verdict == "ok",
                "SQLite 完整性检查通过" if verdict == "ok" else f"完整性检查返回：{verdict}",
            )
        )

        missing = missing_tables(raw)
        report.checks.append(
            (
                "tables",
                not missing,
                f"代码要用的表都在（{len(_table_counts(raw))} 张）"
                if not missing
                else f"缺了代码要用的表：{missing} —— 这份备份比代码旧",
            )
        )

        if manifest is not None and manifest.tables:
            counts = _table_counts(raw)
            mismatched = {
                name: (expected, counts.get(name))
                for name, expected in manifest.tables.items()
                if counts.get(name) != expected
            }
            report.checks.append(
                (
                    "row_counts",
                    not mismatched,
                    f"行数与清单一致（{len(manifest.tables)} 张表）"
                    if not mismatched
                    else f"行数与备份时的清单不符：{mismatched}",
                )
            )
        else:
            report.checks.append(("row_counts", True, "没有清单，跳过行数对账"))

        if manifest is not None and manifest.migrations:
            applied = _applied_migrations(raw)
            report.checks.append(
                (
                    "migrations",
                    applied == manifest.migrations,
                    f"迁移记账完整（{len(applied)} 条）"
                    if applied == manifest.migrations
                    else f"迁移记账与备份时不一致：{applied} != {manifest.migrations}",
                )
            )
        else:
            report.checks.append(("migrations", True, "没有清单，跳过迁移对账"))

    return _finish(report)


def _finish(report: VerifyReport) -> VerifyReport:
    report.ok = all(passed for _name, passed, _why in report.checks)
    return report


def prune(dest_dir: Path, *, keep: int = DEFAULT_KEEP) -> list[Path]:
    """保留最近 `keep` 份备份（连同 sidecar 清单一起删）。返回删掉的文件。"""
    if keep < 1:
        return []
    archives = sorted(dest_dir.glob("deepgrill-*.db.gz"))
    doomed = archives[:-keep] if len(archives) > keep else []
    removed: list[Path] = []
    for path in doomed:
        for target in (path, path.with_suffix(path.suffix + ".json")):
            if target.is_file():
                target.unlink()
                removed.append(target)
    return removed


@dataclass
class BackupResult:
    archive: Path | None
    manifest: Manifest | None
    report: VerifyReport
    pruned: list[Path] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.archive is not None and self.report.ok and not self.error


def backup(db_path: Path, dest_dir: Path, *, keep: int = DEFAULT_KEEP) -> BackupResult:
    """**做一份备份，并且当场验证它能不能恢复。**

    这个函数存在的理由就是那句话：备份"成功"但恢复不了是最难发现的失败 ——
    它只在需要它的那一天暴露。所以退出码（`BackupResult.ok`）回答的是
    "这份备份现在能不能用"，而不是"文件写出来了没有"。
    """
    try:
        archive, manifest = snapshot(db_path, dest_dir)
    except (OSError, sqlite3.Error) as e:
        return BackupResult(None, None, VerifyReport(ok=False), error=f"快照失败：{e}")

    report = verify(archive, manifest=manifest)
    return BackupResult(archive, manifest, report, pruned=prune(dest_dir, keep=keep))


def record(session: Session, result: BackupResult) -> TaskLog:
    """把这次备份写进 `task_logs`（观测页据此显示"最近一次备份有多旧"）。

    **失败也要写**（§3.1）：一份"看起来有、其实不能恢复"的备份比没有备份更危险。
    """
    body: dict[str, object] = {
        "status": "done" if result.ok else "failed",
        "created_at": result.manifest.created_at if result.manifest else _now(),
        "file": result.archive.name if result.archive else "",
        "size_bytes": result.manifest.size_bytes if result.manifest else 0,
        "verified": result.report.ok,
    }
    if result.error:
        body["error"] = result.error
    elif not result.report.ok:
        body["error"] = result.report.summary()
    body["message"] = (
        f"备份 {'成功且已验证' if result.ok else '有问题'}："
        f"{body['file'] or '（没写出文件）'}（{body['size_bytes']} 字节）"
    )
    row = TaskLog(kind=KIND, payload={"dest": str(result.archive.parent) if result.archive else ""},
                  result=body)
    session.add(row)
    session.flush()
    return row


def latest(session: Session) -> TaskLog | None:
    """最近一次备份报告（观测页用）。`None` = 从来没备份过。"""
    from sqlalchemy import select

    return session.execute(
        select(TaskLog).where(TaskLog.kind == KIND).order_by(TaskLog.id.desc()).limit(1)
    ).scalar_one_or_none()


def age_hours(row: TaskLog | None, *, now: str | None = None) -> float | None:
    """最近一次备份距今多少小时。`None` = 没有备份记录。

    ⚠️ 只认**自己那份清单里记的时间**（`result.created_at`），不认 `task_logs.created_at`：
    后者是"报告写下来的时间"，而我们要回答的是"备份是什么时候做的"。
    """
    if row is None or not isinstance(row.result, dict):
        return None
    stamp = str(row.result.get("created_at") or "")
    if not stamp:
        return None
    try:
        made = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    reference = (
        datetime.strptime(now, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        if now
        else datetime.now(UTC)
    )
    return (reference - made).total_seconds() / 3600
