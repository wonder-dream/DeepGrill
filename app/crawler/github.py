"""M7 GitHub 面经源：git clone/pull → markdown 遍历 → 清洗入库。

单仓库失败（网络/认证/冲突）跳过该仓库；单文件解析失败跳过该文件。
"""
import hashlib
import logging
import shutil
import subprocess
from pathlib import Path

from ..db import commit, find_source_by_hash, get_session
from ..errors import DuplicateSource, GitHubError
from ..models import Source, SourceType
from ..pipeline.clean import clean_text

logger = logging.getLogger(__name__)

CLONE_BASE = "https://github.com"  # 测试可 monkeypatch 为本地 file:// 仓库根
EXCLUDED_DIRS = {"code", "scripts", "assets", "images"}
MAX_FILE_SIZE = 1_000_000
_GIT_TIMEOUT = 120


def collect(repos: list[str], cache_dir: Path) -> list[Source]:
    """本日增量采集：逐仓库 clone/pull + 逐文件入库；单仓库/单文件失败隔离。"""
    if shutil.which("git") is None:
        logger.warning("git not found; skipping github source")
        return []
    sources: list[Source] = []
    for repo in repos:
        try:
            repo_path = _ensure_repo(repo, cache_dir)
            sources.extend(_import_repo(repo, repo_path))
        except GitHubError as e:
            logger.warning("github repo %s skipped: %s", repo, e)
    return sources


def ensure_repos(repos: list[str], cache_dir: Path) -> list[Path]:
    """clone（不存在）或 pull（已存在），返回成功仓库的本地路径列表。"""
    if shutil.which("git") is None:
        return []
    paths = []
    for repo in repos:
        try:
            paths.append(_ensure_repo(repo, cache_dir))
        except GitHubError as e:
            logger.warning("github repo %s skipped: %s", repo, e)
    return paths


def _ensure_repo(repo: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 必须绝对路径：git clone 以 cache_dir 为 cwd，相对目标会被解析成 cache_dir 内嵌套
    repo_path = (cache_dir / repo.replace("/", "__")).resolve()
    if (repo_path / ".git").exists():
        _git(repo_path, "pull", "--ff-only", "-q")
        return repo_path
    _git(cache_dir, "clone", "-q", f"{CLONE_BASE}/{repo}", str(repo_path))
    return repo_path


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise GitHubError(f"git {args[0]} failed in {cwd}: {e}") from e
    if result.returncode != 0:
        raise GitHubError(f"git {args[0]} failed in {cwd}: {result.stderr.strip()}")
    return result.stdout


def _import_repo(repo: str, repo_path: Path) -> list[Source]:
    sources = []
    for file in sorted(repo_path.rglob("*.md")):
        rel = file.relative_to(repo_path)
        if set(rel.parts[:-1]) & EXCLUDED_DIRS or file.name.upper().startswith("README"):
            continue
        if file.stat().st_size > MAX_FILE_SIZE:
            continue
        try:
            source = _import_file(repo, file, rel)
            if source is not None:
                sources.append(source)
        except Exception as e:
            logger.warning("skip %s: %s", rel, e)
    return sources


def _import_file(repo: str, file: Path, rel: Path) -> Source | None:
    raw = file.read_bytes()
    content = _decode_text(raw, rel)
    hash_ = hashlib.sha256(raw).hexdigest()
    with get_session() as session:
        if find_source_by_hash(session, hash_):
            return None
        source = Source(
            type=SourceType.github,
            url=f"{CLONE_BASE}/{repo}/raw/HEAD/{rel.as_posix()}",
            title=_extract_title(content, rel),
            raw_text=content,
            cleaned_text=clean_text(content),
            source_hash=hash_,
        )
        session.add(source)
        try:
            commit(session)
        except DuplicateSource:
            return None
        session.refresh(source)
    return source


def _decode_text(raw: bytes, rel: Path) -> str:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise GitHubError(f"cannot decode {rel}: not utf-8 or gbk")


def _extract_title(content: str, rel: Path) -> str:
    first_line = content.splitlines()[0].strip() if content.strip() else ""
    if first_line.startswith("# "):
        return first_line[2:].strip()
    return rel.stem
