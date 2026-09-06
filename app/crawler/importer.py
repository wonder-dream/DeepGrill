import hashlib
import logging
from pathlib import Path

from ..db import commit, find_source_by_hash, get_session
from ..errors import DuplicateSource, ImportError as AppImportError
from ..models import Source, SourceType
from ..parsers import decode_md_txt, extract_title
from ..pipeline.clean import clean_text

logger = logging.getLogger(__name__)

DEFAULT_IMPORT_DIR = Path("data/imports")
SUPPORTED_TYPES = ("manual", "resume")
_TYPES_BY_PREFIX = (("resume_", "resume"), ("manual_", "manual"))


def import_file(
    path: Path, source_type: str, *, project_generator=None
) -> Source:
    """导入本地 md/txt 为 Source；重复导入幂等返回已有记录。

    project_generator(source) 为可注入的 M8 project 题生成器（简历类型触发，
    由调用方携带 limit；生成失败仅记日志不影响入库）。
    """
    if source_type not in SUPPORTED_TYPES:
        raise AppImportError(f"unsupported source type: {source_type}")
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise AppImportError(f"cannot read {path}: {e}") from e
    content = _decode(raw, path)
    source_hash = hashlib.sha256(raw).hexdigest()

    with get_session() as session:
        existing = find_source_by_hash(session, source_hash)
        if existing:
            return existing
        source = Source(
            type=SourceType(source_type),
            title=extract_title(content, path),
            cleaned_text=clean_text(content),
            source_hash=source_hash,
        )
        session.add(source)
        try:
            commit(session)
        except DuplicateSource:
            return find_source_by_hash(session, source_hash)
        session.refresh(source)

    if source_type == "resume" and project_generator is not None:
        _generate_project_questions(source, project_generator)
    return source


def collect(import_dir: Path | None = None) -> list[Source]:
    """与其他源同协议（M12 遍历用）：扫描导入目录，按文件名前缀区分类型，幂等导入全部 md/txt。

    目录不存在返回空列表（首次运行无导入目录不报错）。
    """
    import_dir = import_dir or DEFAULT_IMPORT_DIR
    if not import_dir.is_dir():
        return []
    sources = []
    for path in sorted(import_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".md", ".txt"):
            continue
        sources.append(import_file(path, _type_from_filename(path.stem)))
    return sources


def _decode(raw: bytes, path: Path) -> str:
    try:
        return decode_md_txt(raw)
    except ValueError as e:
        raise AppImportError(f"cannot decode {path}: not utf-8 or gbk") from e


def _type_from_filename(stem: str) -> str:
    for prefix, source_type in _TYPES_BY_PREFIX:
        if stem.startswith(prefix):
            return source_type
    return "manual"


def _generate_project_questions(source: Source, project_generator) -> None:
    try:
        project_generator(source)
    except Exception as e:
        logger.warning("project question generation failed for source %s: %s", source.id, e)
