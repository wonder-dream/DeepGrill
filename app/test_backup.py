"""备份与恢复验证的测试（ADR-0008）。

这一层的失败方式是**最贵的一种**：备份看起来成功了，而**需要它的那一天**才发现
恢复不了。所以每条断言都针对一种"看起来没问题"的假象：

· `cp` 出来的副本在 WAL 下**看着是好的** —— 所以快照必须用 `VACUUM INTO`
  （测试用"WAL 里还有未 checkpoint 的数据"来证明这一点）
· 备份文件写出来了 ≠ 能恢复 —— 所以 `backup()` **当场验证**，失败就非 0 退出
· 备份比代码旧（少一张表）是最常见的恢复失败 —— 单独一条
· 截断/改过的文件要被抓出来（清单里的校验和）
· 保留策略删的是**最旧的**，而且连清单一起删
"""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from app import backup
from app.db import create_db_engine, create_session_factory
from app.db.models import TaskLog, User
from migrations._runner import migrate


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "main.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="b@local", username="b", password_hash="x", role="user"))
        s.commit()
    return path


def _count_users(path: Path) -> int:
    with sqlite3.connect(str(path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def test_snapshot_round_trip(db: Path, tmp_dir: Path) -> None:
    archive, manifest = backup.snapshot(db, tmp_dir / "backups")
    assert archive.is_file()
    assert manifest.tables["users"] == 2, "迁移里的占位 owner + 我们加的那个"
    assert "0001_initial.sql" in manifest.migrations

    report = backup.verify(archive, manifest=manifest)
    assert report.ok, report.summary()


def test_snapshot_uses_vacuum_not_a_file_copy(db: Path, tmp_dir: Path) -> None:
    """**WAL 里还有未 checkpoint 的数据时，`cp` 会漏掉它** —— 而副本看起来是好的。

    做法：写入之后**不 checkpoint**（WAL 里躺着新行），然后比"快照里的行数"与
    "直接拷主库文件的行数"。快照来自 SQLite 自己，所以它看得到 WAL 里的数据。
    """
    with create_session_factory(create_db_engine(db))() as s:
        s.add(User(id=3, email="wal@local", username="w", password_hash="x", role="user"))
        s.commit()

    archive, manifest = backup.snapshot(db, tmp_dir / "backups")
    assert manifest.tables["users"] == 3, "快照必须包含 WAL 里那一条"

    naive = tmp_dir / "naive.db"
    naive.write_bytes(db.read_bytes())  # `cp` 的等价物：只拷主库文件
    assert _count_users(naive) < 3, "只拷主库文件会漏掉 WAL 里的数据（这正是不能 cp 的理由）"


def test_snapshot_refuses_a_missing_db(tmp_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup.snapshot(tmp_dir / "nothing.db", tmp_dir / "backups")


# ---------------------------------------------------------------------------
# 恢复验证
# ---------------------------------------------------------------------------
def test_verify_catches_a_truncated_file(db: Path, tmp_dir: Path) -> None:
    """被截断的备份（拷贝中断、磁盘满）—— 校验和是第一道闸。"""
    archive, manifest = backup.snapshot(db, tmp_dir / "backups")
    with archive.open("r+b") as handle:
        handle.truncate(archive.stat().st_size // 2)

    report = backup.verify(archive, manifest=manifest)
    assert not report.ok
    assert any("校验和" in why or "解压" in why for _n, passed, why in report.checks if not passed)


def test_verify_catches_a_swapped_but_valid_file(db: Path, tmp_dir: Path) -> None:
    """**校验和是唯一能抓住这一种的东西**：文件被换成另一份**有效**的备份。

    行数、表、迁移记账全都对得上（同一套结构），只有内容变了 ——
    没有校验和就发现不了（"有人把备份换成旧的/错的"是真实事故）。
    """
    archive, manifest = backup.snapshot(db, tmp_dir / "backups")

    # 解压 → 改一行（**行数不变**）→ 重新压回同一个路径
    raw = tmp_dir / "tampered.db"
    with gzip.open(archive, "rb") as src, raw.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    with sqlite3.connect(str(raw)) as conn:
        conn.execute("UPDATE users SET username = 'someone-else' WHERE id = 2")
        conn.commit()
    with raw.open("rb") as src, gzip.open(archive, "wb") as dst:
        shutil.copyfileobj(src, dst)

    report = backup.verify(archive, manifest=manifest)
    assert not report.ok
    failed = [name for name, passed, _why in report.checks if not passed]
    assert failed == ["checksum"], f"只有校验和能抓住它，实际失败的是 {failed}"


def test_verify_catches_a_corrupt_database_without_a_sidecar(db: Path, tmp_dir: Path) -> None:
    """**清单不在时，完整性检查就是最后一道闸**。

    做法：解压后砍掉后半段（SQLite 头部还在，所以它**打得开**，但页结构已经坏了），
    然后删掉清单 —— 这样只有 `PRAGMA integrity_check` 能发现它。
    """
    archive, _ = backup.snapshot(db, tmp_dir / "backups")
    archive.with_suffix(archive.suffix + ".json").unlink()

    raw = tmp_dir / "broken.db"
    with gzip.open(archive, "rb") as src, raw.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    size = raw.stat().st_size
    with raw.open("r+b") as handle:
        handle.truncate(size - size // 3)
    with raw.open("rb") as src, gzip.open(archive, "wb") as dst:
        shutil.copyfileobj(src, dst)

    report = backup.verify(archive)
    assert not report.ok
    assert any(
        name == "integrity" and not passed for name, passed, _why in report.checks
    ), "坏掉的库必须被完整性检查抓住"


def test_verify_fails_when_integrity_check_reports_a_problem(
    db: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PRAGMA integrity_check` 给出**非 ok 的结论**时，这份备份就不算可恢复。

    真实的页级损坏很难稳定造出来（截断会让它直接打不开，走另一条错误路径），
    所以这里显式注入一个"非 ok 的结论"——被测的是那行判断。
    """
    archive, manifest = backup.snapshot(db, tmp_dir / "backups")
    monkeypatch.setattr(
        backup, "_integrity_verdict", lambda path: "row 3 missing from index users"
    )

    report = backup.verify(archive, manifest=manifest)
    assert not report.ok
    assert any(
        name == "integrity" and not passed for name, passed, _why in report.checks
    )
    assert "missing from index" in report.summary()


def test_verify_catches_a_backup_older_than_the_code(db: Path, tmp_dir: Path) -> None:
    """**最常见的恢复失败**：备份比代码旧（少一张表）。

    做法：造一个只跑到 `0003` 的库（把 `embeddings` 表删掉），再快照它。
    """
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DROP TABLE embeddings")

    archive, manifest = backup.snapshot(db, tmp_dir / "backups")
    report = backup.verify(archive, manifest=manifest)
    assert not report.ok
    assert any("比代码旧" in why for _n, passed, why in report.checks if not passed)


def test_verify_catches_a_manifest_that_does_not_match(db: Path, tmp_dir: Path) -> None:
    """行数对账：清单说 3 行、备份里只有 2 行 —— 说明这不是同一份数据。"""
    archive, manifest = backup.snapshot(db, tmp_dir / "backups")
    manifest.tables["users"] = 999

    report = backup.verify(archive, manifest=manifest)
    assert not report.ok
    assert any("行数" in why for _n, passed, why in report.checks if not passed)


def test_verify_without_a_sidecar_still_checks_integrity(db: Path, tmp_dir: Path) -> None:
    """清单丢了不该让验证变成"全过"：没有清单就**跳过对账**，但完整性与表齐照查。

    （这也是"只有一份裸备份文件、清单没跟着传"的真实场景。）
    """
    archive, _ = backup.snapshot(db, tmp_dir / "backups")
    archive.with_suffix(archive.suffix + ".json").unlink()

    report = backup.verify(archive)
    assert report.ok, "文件本身是好的"
    checks = {name: (passed, why) for name, passed, why in report.checks}
    assert checks["integrity"][1] == "SQLite 完整性检查通过"
    assert "都在" in checks["tables"][1]
    assert "跳过校验和" in checks["checksum"][1]
    assert "跳过行数对账" in checks["row_counts"][1]


def test_verify_rejects_a_missing_file(tmp_dir: Path) -> None:
    report = backup.verify(tmp_dir / "nope.db.gz")
    assert not report.ok
    assert "不存在" in report.summary()


# ---------------------------------------------------------------------------
# 备份 = 快照 + 当场验证
# ---------------------------------------------------------------------------
def test_backup_writes_a_manifest_next_to_the_archive(db: Path, tmp_dir: Path) -> None:
    result = backup.backup(db, tmp_dir / "backups")
    assert result.ok
    assert result.archive is not None
    sidecar = result.archive.with_suffix(result.archive.suffix + ".json")
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["sha256"] == result.manifest.sha256


def test_backup_fails_loudly_when_the_destination_is_not_writable(
    db: Path, tmp_dir: Path
) -> None:
    """**写不出文件也不许假装成功**（§3.1）：`ok=False` + 一句原因。

    做法：把"备份目录"指到一个**文件**上 —— `mkdir` 会失败，于是 `snapshot` 抛错。
    """
    dest = tmp_dir / "not-a-dir"
    dest.write_text("我是个文件，不是目录", encoding="utf-8")

    result = backup.backup(db, dest)
    assert result.ok is False
    assert result.error, "失败必须带原因"
    assert result.archive is None


def test_backup_is_not_ok_when_its_own_verification_fails(
    db: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**这一步是这一层存在的理由**：备份"成功"但恢复不了，只在需要它的那天暴露。

    所以 `backup()` 的 ok 必须跟着**验证结果**走，而不是跟着"文件写出来了"走。
    这里把验证结果换成失败的那一份，断言 ok 跟着变、报告也写成 failed。
    """
    broken = backup.VerifyReport(ok=False)
    broken.checks.append(("integrity", False, "完整性检查返回：malformed"))
    monkeypatch.setattr(backup, "verify", lambda *a, **kw: broken)

    result = backup.backup(db, tmp_dir / "backups")
    assert result.archive is not None, "文件确实写出来了"
    assert result.ok is False, "但验证没过 —— 那就不是一份成功的备份"
    assert result.report.failures

    with create_session_factory(create_db_engine(db))() as s:
        row = backup.record(s, result)
        s.commit()
        assert row.result["status"] == "failed"
        assert row.result["verified"] is False


def test_backup_records_a_report(db: Path, tmp_dir: Path) -> None:
    result = backup.backup(db, tmp_dir / "backups")
    with create_session_factory(create_db_engine(db))() as s:
        row = backup.record(s, result)
        s.commit()
        assert row.kind == backup.KIND
        assert row.result["status"] == "done"
        assert row.result["verified"] is True

        latest = backup.latest(s)
        assert latest is not None
        assert backup.age_hours(latest) is not None


def test_a_failed_backup_is_recorded_as_failed_too(db: Path) -> None:
    """失败也要写一行（§3.1）：一份"看起来有、其实不能恢复"的备份比没有更危险。"""
    bad = backup.BackupResult(
        None, None, backup.VerifyReport(ok=False), error="快照失败：磁盘满"
    )
    with create_session_factory(create_db_engine(db))() as s:
        row = backup.record(s, bad)
        s.commit()
        assert row.result["status"] == "failed"
        assert "磁盘满" in str(row.result["error"])
        assert "有问题" in str(row.result["message"])


def test_age_hours_uses_the_manifest_time_not_the_row_time(db: Path, tmp_dir: Path) -> None:
    """年龄按**备份清单里的时间**算，不按"报告写下来的时间" —— 两者可以差很远。"""
    result = backup.backup(db, tmp_dir / "backups")
    assert result.manifest is not None
    result.manifest.created_at = "2020-01-01 00:00:00"
    with create_session_factory(create_db_engine(db))() as s:
        row = backup.record(s, result)
        s.commit()
        hours = backup.age_hours(row, now="2020-01-03 00:00:00")
    assert hours == pytest.approx(48.0)


def test_age_hours_is_none_without_a_report() -> None:
    assert backup.age_hours(None) is None


# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------
def test_prune_keeps_the_newest(db: Path, tmp_dir: Path) -> None:
    dest = tmp_dir / "backups"
    dest.mkdir()
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        archive = dest / f"deepgrill-{stamp}.db.gz"
        archive.write_bytes(gzip.compress(b"x"))
        archive.with_suffix(archive.suffix + ".json").write_text("{}", encoding="utf-8")

    removed = backup.prune(dest, keep=2)
    left = sorted(p.name for p in dest.glob("deepgrill-*.db.gz"))
    assert left == [f"deepgrill-{s}.db.gz" for s in ("20260102-000000", "20260103-000000")]
    assert len(removed) == 2, "清单也要跟着删（否则清单目录会一直长）"
    assert not (dest / "deepgrill-20260101-000000.db.gz.json").exists()


def test_prune_is_a_noop_below_the_limit(tmp_dir: Path) -> None:
    dest = tmp_dir / "backups"
    dest.mkdir()
    (dest / "deepgrill-20260101-000000.db.gz").write_bytes(b"x")
    assert backup.prune(dest, keep=5) == []


def test_prune_with_zero_keeps_everything(tmp_dir: Path) -> None:
    """`keep=0` 是"别删"（把保留策略关掉），不是"全删" —— 全删是灾难性的误读。"""
    dest = tmp_dir / "backups"
    dest.mkdir()
    (dest / "deepgrill-20260101-000000.db.gz").write_bytes(b"x")
    assert backup.prune(dest, keep=0) == []
    assert (dest / "deepgrill-20260101-000000.db.gz").is_file()


# ---------------------------------------------------------------------------
# 观测页要看得见（"最近一次有多旧"与"能不能恢复"是同一个问题的两半）
# ---------------------------------------------------------------------------
def test_observability_shows_the_backup_state(db: Path, tmp_dir: Path) -> None:
    from app.web import observability

    result = backup.backup(db, tmp_dir / "backups")
    with create_session_factory(create_db_engine(db))() as s:
        backup.record(s, result)
        s.commit()

        summary = observability.backup_summary(s)
        assert not summary.never
        assert summary.verified is True
        assert summary.failed is False
        assert summary.age_hours is not None and summary.age_hours < 1
        assert summary.stale is False


def test_observability_says_never_when_there_is_no_backup(db: Path) -> None:
    """**没备份过 ≠ 没问题** —— 页面要能区分"从没备份"与"备份很久没更新"。"""
    from app.web import observability

    with create_session_factory(create_db_engine(db))() as s:
        summary = observability.backup_summary(s)
    assert summary.never is True
    assert summary.stale is False


def test_observability_flags_a_stale_backup(db: Path, tmp_dir: Path) -> None:
    from app.web import observability

    result = backup.backup(db, tmp_dir / "backups")
    assert result.manifest is not None
    with create_session_factory(create_db_engine(db))() as s:
        # 把清单时间改老：观测页显示的是"最近一次备份距今多久"
        row = backup.record(s, result)
        row.result = dict(row.result or {})
        row.result["created_at"] = "2020-01-01 00:00:00"
        s.commit()
        summary = observability.backup_summary(s)
    assert summary.stale is True
    assert summary.age_hours and summary.age_hours > 48


def test_task_logs_show_the_backup_report_too(db: Path, tmp_dir: Path) -> None:
    """它落在 `task_logs` 里 —— 观测页的"离线任务报告"那一节会显示它（免费获得）。"""
    result = backup.backup(db, tmp_dir / "backups")
    with create_session_factory(create_db_engine(db))() as s:
        backup.record(s, result)
        s.commit()
        rows = list(s.execute(select(TaskLog).where(TaskLog.kind == backup.KIND)).scalars())
    assert len(rows) == 1
    assert "已验证" in str(rows[0].result.get("message"))
