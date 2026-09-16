"""备份的异地那一份（第二份）：复制 + 校验和 + **失败让退出码非零**。

为什么这几条值得单独测：部署文档里那句话是全部理由 ——
「**"备份成功但没传出去"的绿色状态比没有备份更危险**」。所以这里钉三件事：

· 异地真的落了两个文件（快照 + 清单），且内容与本地那份**逐字节相同**（比 sha256）
· 异地目录不存在/写不进去时，`BackupResult.ok` **为假**（于是 systemd 单元标红）
· 本地那份没验过时不复制（把坏的复制出去只是把"坏"变成"两处都坏"）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.backup import _sha256, backup, snapshot, verify
from app.db import create_db_engine, create_session_factory
from app.db.models import User
from migrations._runner import migrate


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "mirror.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=99, email="m@local", username="m", password_hash="h", role="user"))
        s.commit()
    return path


def test_backup_copies_a_verified_second_copy(db: Path, tmp_dir: Path) -> None:
    """异地那一份要与本地那份**逐字节相同**（大小 + sha256 都核）。"""
    dest, offsite = tmp_dir / "here", tmp_dir / "offsite"
    result = backup(db, dest, mirror_dir=offsite)

    assert result.ok, result.mirror_error or result.error
    assert result.mirrored, "异地没落地"
    assert result.archive is not None
    local, remote = result.archive, offsite / result.archive.name
    assert remote.is_file() and remote.with_suffix(remote.suffix + ".json").is_file()
    assert remote.stat().st_size == local.stat().st_size
    assert _sha256(remote) == _sha256(local)
    # 异地那份**自己**也要能被验证（拿清单当输入）
    assert verify(remote).ok, "异地那份单独验不过"


def test_mirror_dir_can_be_created(db: Path, tmp_dir: Path) -> None:
    """异地目录还不存在时应当被建出来（首次部署就是这样）。"""
    offsite = tmp_dir / "offsite" / "nested"
    result = backup(db, tmp_dir / "here", mirror_dir=offsite)
    assert result.ok and offsite.is_dir()


def test_a_failed_mirror_makes_the_whole_backup_not_ok(db: Path, tmp_dir: Path) -> None:
    """**异地失败 = 这份备份不算成功**（否则单元是绿的，而第二份根本没出去）。

    构造方式：把"异地目录"指到一个**文件**上（`mkdir` 必然失败）。
    """
    blocker = tmp_dir / "not-a-dir"
    blocker.write_text("占位", encoding="utf-8")
    result = backup(db, tmp_dir / "here", mirror_dir=blocker / "sub")

    assert result.archive is not None and result.report.ok, "本地那份本身是好的"
    assert not result.ok, "异地失败必须让整体不 ok"
    assert "异地" in result.mirror_error


def test_mirror_keeps_only_the_recent_ones(db: Path, tmp_dir: Path) -> None:
    """异地目录也按保留份数清理（第二块盘也不是无限的）。"""
    dest, offsite = tmp_dir / "here", tmp_dir / "offsite"
    offsite.mkdir()
    for name in ("deepgrill-20000101-000000.db.gz", "deepgrill-20000102-000000.db.gz"):
        (offsite / name).write_bytes(b"x")   # 两份"旧备份"（名字排在新备份前面）

    result = backup(db, dest, mirror_dir=offsite, keep=1)
    assert result.ok
    remaining = sorted(p.name for p in offsite.glob("deepgrill-*.db.gz"))
    assert remaining == [result.archive.name], remaining


def test_a_broken_local_copy_is_not_mirrored(db: Path, tmp_dir: Path) -> None:
    """本地那份**没验过**就不往异地复制 —— 否则只是把"坏"复制成"两处都坏"。"""
    dest, offsite = tmp_dir / "here", tmp_dir / "offsite"
    archive, manifest = snapshot(db, dest)
    # 破坏本地那份（内容变了，但清单还是旧的 → verify 会失败）
    archive.write_bytes(b"corrupted")
    assert not verify(archive, manifest=manifest).ok

    result = backup(db, dest, mirror_dir=offsite)
    # 本次快照是新的一份，所以这里真正断言的是"异地里的东西都是验过的"
    for path in offsite.glob("deepgrill-*.db.gz"):
        assert verify(path).ok, f"异地里有一份验不过：{path.name}"
    assert result.ok
