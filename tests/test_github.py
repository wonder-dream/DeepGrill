import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.crawler import github as gh
from app.models import Source, SourceType

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def commit_all(repo_path):
    git(repo_path, "add", "-A")
    git(
        repo_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "-m",
        "update",
    )


def make_remote(root, name="owner/repo1", files=None):
    """在 root 下造一个真实 git 仓库（owner/repo1），提交给定文件。"""
    remote = root / name.replace("/", "/")
    remote.mkdir(parents=True, exist_ok=True)
    git(remote, "init", "-q", "-b", "main")
    for rel, content in (files or {}).items():
        p = remote / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    if files:
        commit_all(remote)
    return root


def count_sources(db):
    return db.scalars(select(func.count()).select_from(Source)).one()


# --- happy（真实 git 操作） ---


def test_clone_and_import(tmp_path, monkeypatch, db):
    root = make_remote(
        tmp_path,
        files={
            "README.md": "# 仓库说明",
            "一面.md": "# 阿里一面\n\n问了 HashMap。",
            "notes/二面.md": "二面：\n问了 JVM 调优。",
        },
    )
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))

    sources = gh.collect(["owner/repo1"], tmp_path / "cache")

    assert len(sources) == 2  # README 被跳过
    assert all(s.type == SourceType.github for s in sources)
    titles = {s.title for s in sources}
    assert "阿里一面" in titles
    assert "二面" in titles
    assert "HashMap" in next(
        s.cleaned_text for s in sources if s.title == "阿里一面"
    )
    assert any(
        "owner/repo1/raw/HEAD/notes/二面.md" in s.url for s in sources
    )
    assert count_sources(db) == 2


def test_pull_incremental(tmp_path, monkeypatch, db):
    root = make_remote(tmp_path, files={"a.md": "一面：\n问了 X。"})
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    cache = tmp_path / "cache"

    first = gh.collect(["owner/repo1"], cache)
    assert len(first) == 1

    (root / "owner" / "repo1" / "b.md").write_bytes(
        "二面：\n问了 Y。".encode("utf-8")
    )
    commit_all(root / "owner" / "repo1")

    second = gh.collect(["owner/repo1"], cache)
    assert len(second) == 1
    assert "Y" in second[0].cleaned_text
    assert count_sources(db) == 2


def test_deleted_file_no_longer_produced(tmp_path, monkeypatch, db):
    root = make_remote(tmp_path, files={"a.md": "一面：\n问了 X。"})
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    cache = tmp_path / "cache"

    gh.collect(["owner/repo1"], cache)
    (root / "owner" / "repo1" / "a.md").unlink()
    commit_all(root / "owner" / "repo1")

    second = gh.collect(["owner/repo1"], cache)
    assert second == []  # 删除文件不再产出新源
    assert count_sources(db) == 1  # 历史记录保留


# --- edge ---


def test_empty_repo(tmp_path, monkeypatch, db):
    root = make_remote(tmp_path)
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    assert gh.collect(["owner/repo1"], tmp_path / "cache") == []


def test_repo_without_markdown(tmp_path, monkeypatch, db):
    root = make_remote(tmp_path, files={"a.txt": "没有 markdown"})
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    assert gh.collect(["owner/repo1"], tmp_path / "cache") == []


def test_multiple_repos(tmp_path, monkeypatch, db):
    root = make_remote(tmp_path, name="owner/repo1", files={"a.md": "一面：\n问了 A。"})
    make_remote(tmp_path, name="owner/repo2", files={"b.md": "一面：\n问了 B。"})
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    sources = gh.collect(["owner/repo1", "owner/repo2"], tmp_path / "cache")
    assert len(sources) == 2
    assert count_sources(db) == 2


def test_large_file_skipped(tmp_path, monkeypatch, db):
    root = make_remote(
        tmp_path,
        files={"big.md": "x" * (gh.MAX_FILE_SIZE + 1), "small.md": "一面：\n问了 S。"},
    )
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    sources = gh.collect(["owner/repo1"], tmp_path / "cache")
    assert len(sources) == 1
    assert sources[0].title == "small"


def test_excluded_dirs_and_readme_skipped(tmp_path, monkeypatch, db):
    root = make_remote(
        tmp_path,
        files={
            "README.md": "# 说明",
            "code/x.md": "代码目录里的题",
            "scripts/y.md": "脚本目录",
            "assets/z.md": "资源",
            "images/w.md": "图片",
            "normal.md": "一面：\n问了正常题。",
        },
    )
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    sources = gh.collect(["owner/repo1"], tmp_path / "cache")
    assert len(sources) == 1
    assert sources[0].title == "normal"


def test_undecodable_file_skipped_others_imported(tmp_path, monkeypatch, db):
    root = make_remote(tmp_path, files={"good.md": "一面：\n问了 G。"})
    (root / "owner" / "repo1" / "bad.md").write_bytes(b"\xff\xfe\x00\x41")
    commit_all(root / "owner" / "repo1")
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    sources = gh.collect(["owner/repo1"], tmp_path / "cache")
    assert len(sources) == 1
    assert sources[0].title == "good"


def test_relative_cache_dir_no_nesting(tmp_path, monkeypatch, db):
    """回归：生产用相对 cache_dir（如 data/repos），clone 目标必须是绝对路径，否则嵌套。"""
    root = make_remote(tmp_path, files={"a.md": "一面：\n问了 A。"})
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    monkeypatch.chdir(tmp_path)
    cache = Path("cache")  # 相对路径

    sources = gh.collect(["owner/repo1"], cache)

    assert len(sources) == 1
    repo_path = (cache / "owner__repo1").resolve()
    assert (repo_path / ".git").exists()
    assert not list(Path("cache/cache").rglob("*")) or not Path("cache/cache").exists()


# --- fail ---


def test_no_git_skips_with_log(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(gh.shutil, "which", lambda name: None)
    with caplog.at_level("WARNING"):
        assert gh.collect(["owner/repo1"], tmp_path / "cache") == []
    assert "git not found" in caplog.text


class FakeFailedResult:
    returncode = 1
    stdout = ""
    stderr = "fatal: Authentication failed for 'https://github.com/...'"


def test_auth_failure_skips_repo(monkeypatch, caplog, tmp_path, db):
    monkeypatch.setattr(gh.subprocess, "run", lambda *a, **k: FakeFailedResult())
    with caplog.at_level("WARNING"):
        assert gh.collect(["owner/repo1"], tmp_path / "cache") == []
    assert "skipped" in caplog.text


def test_network_error_skips_repo(monkeypatch, caplog, tmp_path, db):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(gh.subprocess, "run", boom)
    with caplog.at_level("WARNING"):
        assert gh.collect(["owner/repo1"], tmp_path / "cache") == []
    assert "skipped" in caplog.text


def test_git_timeout_wraps_as_github_error(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=120)

    monkeypatch.setattr(gh.subprocess, "run", boom)
    with pytest.raises(gh.GitHubError, match="timed out|failed"):
        gh._ensure_repo("owner/repo1", tmp_path / "cache")


# --- license whitelist / provenance ---


MIT_LICENSE = """MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction.
The Software is provided "as is", without warranty of any kind.
"""


def test_no_license_skipped_when_required(tmp_path, monkeypatch, db, caplog):
    root = make_remote(tmp_path, files={"a.md": "一面：\n问了 A。"})
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    with caplog.at_level("WARNING"):
        assert gh.collect(["owner/repo1"], tmp_path / "cache", require_license=True) == []
    assert "no allowed license" in caplog.text
    assert count_sources(db) == 0


def test_license_allowed_and_provenance_fields(tmp_path, monkeypatch, db):
    root = make_remote(
        tmp_path,
        files={
            "LICENSE": MIT_LICENSE,
            "a.md": "一面：\n问了 A。",
        },
    )
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    sources = gh.collect(
        ["owner/repo1"], tmp_path / "cache",
        require_license=True, allowed_licenses=["MIT"],
    )
    assert len(sources) == 1
    assert sources[0].license == "MIT"
    assert sources[0].author == "owner"
    assert sources[0].repo_url.endswith("owner/repo1")


def test_disallowed_license_skipped(tmp_path, monkeypatch, db, caplog):
    root = make_remote(
        tmp_path,
        files={
            "LICENSE": "GNU GENERAL PUBLIC LICENSE\nVersion 3",
            "a.md": "一面：\n问了 A。",
        },
    )
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    with caplog.at_level("WARNING"):
        assert gh.collect(
            ["owner/repo1"], tmp_path / "cache",
            require_license=True, allowed_licenses=["MIT"],
        ) == []
    assert "no allowed license" in caplog.text


class FakeManualRepo:
    repo = "owner/repo1"
    expected_license = None
    manual_license = "with-author-permission"


def test_manual_license_override_imports(tmp_path, monkeypatch, db):
    root = make_remote(tmp_path, files={"a.md": "一面：\n问了 A。"})
    monkeypatch.setattr(gh, "CLONE_BASE", str(root))
    sources = gh.collect(
        [FakeManualRepo()], tmp_path / "cache",
        require_license=True, allowed_licenses=["MIT"],
    )
    assert len(sources) == 1
    assert sources[0].license == "with-author-permission"