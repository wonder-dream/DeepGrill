from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.crawler.importer import collect, import_file
from app.errors import ImportError as AppImportError
from app.models import Source, SourceType


def write_file(tmp_path, name, content, encoding="utf-8"):
    path = tmp_path / name
    path.write_bytes(content.encode(encoding))
    return path


def count_sources(db):
    return db.scalars(select(func.count()).select_from(Source)).one()


# --- happy ---


def test_import_markdown_with_title(db, tmp_path):
    p = write_file(tmp_path, "m1.md", "# 阿里一面面经\n\n问了 HashMap。")
    source = import_file(p, "manual")
    assert source.title == "阿里一面面经"
    assert source.type == SourceType.manual
    assert source.cleaned_text == "# 阿里一面面经\n问了 HashMap。"  # clean_text 压缩空行
    assert "HashMap" in source.cleaned_text
    assert count_sources(db) == 1


def test_import_txt_title_from_filename(db, tmp_path):
    p = write_file(tmp_path, "随便写的面经.txt", "一面：问了 MySQL 索引。")
    source = import_file(p, "manual")
    assert source.title == "随便写的面经"


def test_reimport_is_idempotent(db, tmp_path):
    p = write_file(tmp_path, "m1.md", "同一份内容")
    first = import_file(p, "manual")
    second = import_file(p, "manual")
    assert second.id == first.id
    assert count_sources(db) == 1


def test_resume_import_triggers_project_generation(db, tmp_path):
    calls = []

    def fake_generator(source):
        calls.append(source)
        return []

    p = write_file(tmp_path, "resume_我.md", "# 简历\n\n项目：Agent 系统")
    source = import_file(p, "resume", project_generator=fake_generator)
    assert source.type == SourceType.resume
    assert len(calls) == 1
    assert calls[0].id == source.id


def test_manual_import_does_not_trigger_generator(db, tmp_path):
    calls = []
    p = write_file(tmp_path, "m1.md", "普通面经")
    import_file(p, "manual", project_generator=lambda s: calls.append(s))
    assert calls == []


# --- edge ---


def test_empty_file_imports(db, tmp_path):
    p = write_file(tmp_path, "空文件.md", "")
    source = import_file(p, "manual")
    assert source.cleaned_text == ""
    assert source.title == "空文件"


def test_large_file_imports(db, tmp_path):
    p = write_file(tmp_path, "big.md", "长" * 1_000_000)
    source = import_file(p, "manual")
    assert len(source.cleaned_text) == 1_000_000


def test_utf8_bom_stripped(db, tmp_path):
    p = tmp_path / "bom.md"
    p.write_bytes("\ufeff# 标题\n正文".encode("utf-8"))
    source = import_file(p, "manual")
    assert source.title == "标题"
    assert not source.cleaned_text.startswith("\ufeff")


def test_gbk_encoding_fallback(db, tmp_path):
    p = tmp_path / "gbk.txt"
    p.write_bytes("一面：问了 Redis 持久化。".encode("gbk"))
    source = import_file(p, "manual")
    assert "Redis 持久化" in source.cleaned_text


def test_collect_scans_dir_with_prefix_types(db, tmp_path):
    write_file(tmp_path, "resume_张三.md", "# 简历")
    write_file(tmp_path, "manual_随手记.txt", "面经内容")
    write_file(tmp_path, "无前缀.md", "默认 manual")
    write_file(tmp_path, "ignore.log", "不是 md/txt")
    (tmp_path / "子目录").mkdir()
    write_file(tmp_path / "子目录", "nested.md", "子目录不扫")

    sources = collect(tmp_path)
    assert [s.type for s in sources] == [
        SourceType.manual,
        SourceType.resume,
        SourceType.manual,
    ]
    assert count_sources(db) == 3


def test_collect_missing_dir_returns_empty():
    assert collect(Path("不存在/目录")) == []


# --- fail ---


def test_path_not_found_raises(db, tmp_path):
    with pytest.raises(AppImportError, match="cannot read"):
        import_file(tmp_path / "nope.md", "manual")


def test_directory_path_raises(db, tmp_path):
    with pytest.raises(AppImportError, match="cannot read"):
        import_file(tmp_path, "manual")


def test_invalid_source_type_raises(db, tmp_path):
    p = write_file(tmp_path, "m1.md", "内容")
    with pytest.raises(AppImportError, match="unsupported source type"):
        import_file(p, "social")


def test_undecodable_content_raises(db, tmp_path):
    p = tmp_path / "bad.bin"
    p.write_bytes(b"\xff\xfe\x00\x41")
    with pytest.raises(AppImportError, match="cannot decode"):
        import_file(p, "manual")


def test_generator_failure_does_not_break_import(db, tmp_path):
    def boom(source):
        raise RuntimeError("llm down")

    p = write_file(tmp_path, "resume_李四.md", "# 简历")
    source = import_file(p, "resume", project_generator=boom)
    assert source.id is not None
    assert count_sources(db) == 1
