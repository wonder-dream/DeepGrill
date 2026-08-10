"""知识库导入（RAG）：扫描 data/knowledge/ 的 md/txt → 切块 → bge-m3 向量化 → 入库 → version+1。

跑法：
    uv run python scripts/import_knowledge.py            # 全量扫描导入
    uv run python scripts/import_knowledge.py --file a.md # 只导入指定文件（可多次）

- 切块：段落优先（空行分隔），500-800 字/块；代码块（```）边界完整保留不切断
- 幂等：内容 hash 去重，重复导入跳过；可反复执行
- 导入成功后 knowledge_meta.version +1，FAISS 索引下次查询自动重建（无需重启）
"""
import argparse
import hashlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import load_config, secret_value
from app.db import commit, get_session, init_db
from app.embed import Embedder, _to_bytes
from app.models import KnowledgeChunk, KnowledgeMeta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import_knowledge")

KNOWLEDGE_DIR = Path("data/knowledge")
MIN_CHARS = 100   # 小于该长度的块丢弃（碎片）
MAX_CHARS = 800   # 单块上限（超长按句子边界截断）
BATCH = 32


def split_chunks(text: str, title: str) -> list[str]:
    """段落优先切块：空行分段；含代码块的段在代码块边界切，保证 ``` 完整。"""
    lines = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    in_code = False

    def flush():
        nonlocal buf
        block = "\n".join(buf).strip()
        buf = []
        if block:
            chunks.append(block)

    for line in lines:
        if line.strip().startswith("```"):
            if not in_code:
                flush()  # 代码块开始前先刷出前面的段落
            buf.append(line)
            in_code = not in_code
            if not in_code:
                flush()  # 代码块闭合后整块刷出（含首尾 ```）
            continue
        if not in_code and not line.strip():
            flush()
            continue
        buf.append(line)
        # 缓冲超长（非代码段）按句子边界切
        if not in_code and len("\n".join(buf)) > MAX_CHARS:
            block = "\n".join(buf)
            cut = max(block.rfind("。", 0, MAX_CHARS), block.rfind(".", 0, MAX_CHARS), block.rfind("\n", 0, MAX_CHARS))
            if cut > MIN_CHARS:
                chunks.append(block[: cut + 1].strip())
                buf = [block[cut + 1 :]]
            else:
                flush()
    flush()
    return [c for c in chunks if len(c) >= MIN_CHARS]


def import_file(path: Path, embedder) -> tuple[int, int]:
    """导入单个文档：切块 → 向量化 → 入库（hash 幂等）。返回 (新增块数, 跳过块数)。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = split_chunks(text, path.stem)
    logger.info("文件 %s：切块 %d", path.name, len(chunks))
    with get_session() as session:
        existing = {
            r[0]
            for r in session.execute(
                select(KnowledgeChunk.source_hash)
            ).all()
        }
    new_chunks = []
    for c in chunks:
        h = hashlib.sha256(c.encode("utf-8")).hexdigest()
        if h in existing:
            continue
        new_chunks.append(KnowledgeChunk(
            title=path.stem,
            content=c,
            source_hash=h,
        ))
        existing.add(h)
    if not new_chunks:
        return 0, len(chunks)
    # 向量化（批量）
    vectors = embedder.encode([c.content for c in new_chunks])
    for c, v in zip(new_chunks, vectors):
        c.embedding = _to_bytes(v)
    with get_session() as session:
        session.add_all(new_chunks)
        commit(session)
    logger.info("文件 %s：新增 %d 块，跳过 %d", path.name, len(new_chunks), len(chunks) - len(new_chunks))
    return len(new_chunks), len(chunks) - len(new_chunks)


def bump_version() -> None:
    with get_session() as session:
        meta = session.get(KnowledgeMeta, 1)
        if meta is not None:
            meta.version += 1
            commit(session)
            logger.info("knowledge_meta version → %d", meta.version)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=str, default=None, help="只导入指定文件（默认扫描 data/knowledge/）")
    args = parser.parse_args()

    init_db("sqlite:///data/interview.db")
    load_config(Path("config.yaml"))  # 仅确保配置存在（embedder 不需要 key）
    embedder = Embedder()

    files = []
    if args.file:
        p = Path(args.file)
        if not p.exists():
            sys.exit(f"文件不存在: {p}")
        files.append(p)
    else:
        if not KNOWLEDGE_DIR.is_dir():
            sys.exit(f"目录不存在: {KNOWLEDGE_DIR}（请创建并放入八股文 md/txt）")
        files = sorted(
            p for p in KNOWLEDGE_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in (".md", ".txt")
        )
    if not files:
        logger.info("没有可导入的文档")
        return

    total_new = 0
    for f in files:
        try:
            new, _ = import_file(f, embedder)
            total_new += new
        except Exception as e:
            logger.warning("文件 %s 导入失败：%s", f.name, str(e)[:120])
    if total_new > 0:
        bump_version()
    logger.info("完成：新增 %d 块", total_new)


if __name__ == "__main__":
    main()
