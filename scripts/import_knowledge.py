"""知识库导入（RAG）：扫描 data/knowledge/ 的 md/txt → 切块 → bge-m3 向量化 → 入库 → version+1。

跑法：
    uv run python scripts/import_knowledge.py             # 全量扫描导入
    uv run python scripts/import_knowledge.py --file a.md # 只导入指定文件（可多次）
    uv run python scripts/import_knowledge.py --distill   # LLM 蒸馏（讲解文 → 高密度知识点）

- 切块：段落优先（空行分隔），500-800 字/块；代码块（```）边界完整保留不切断
- --distill：LLM 逐批蒸馏（保留技术要点、删叙事/口语/类比，不添加新知识；失败降级保留原文）；
  蒸馏前删除该文档旧块（重建语义，防残留重复）；适合讲解型/叙事型文档（信息密度低）
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
from app.llm.llm_client import LLMClient
from app.models import KnowledgeChunk, KnowledgeMeta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import_knowledge")

KNOWLEDGE_DIR = Path("data/knowledge")
MIN_CHARS = 100   # 小于该长度的块丢弃（碎片）
MAX_CHARS = 800   # 单块上限（超长按句子边界截断）
BATCH = 32
DISTILL_BATCH = 20

DISTILL_PROMPT = """将以下文字逐条改写为**高密度知识要点**（用于面试知识库）：

要求：
1. 只保留技术性知识点、定义、结论与关键细节，删除叙事、背景故事、口语表达、类比、感慨、重复
2. 不得添加原文没有的新知识（宁缺毋滥，不确定的删掉）
3. 代码、术语、数字原样保留
4. 每条输出紧凑要点，控制在 150-400 字

{numbered}

只输出 JSON，不要其他文字：{{"items": [{{"index": 序号, "content": "改写后的知识要点"}}]}}"""


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


def distill_chunks(chunks: list[str], llm) -> list[str]:
    """LLM 蒸馏：每批一次调用（输出 JSON 数组按 index 对齐）；缺失/失败块保留原文（降级不丢）。"""
    out: list[str] = [""] * len(chunks)
    for i in range(0, len(chunks), DISTILL_BATCH):
        batch = chunks[i : i + DISTILL_BATCH]
        numbered = "\n".join(f"{j + 1}. {c}" for j, c in enumerate(batch))
        try:
            parsed = llm.complete(
                [{"role": "user", "content": DISTILL_PROMPT.format(numbered=numbered)}],
                json_schema={},
            )
            items = parsed.get("items", []) if isinstance(parsed, dict) else []
            by_idx = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                idx = item.get("index")
                content = item.get("content")
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    continue
                if isinstance(content, str) and content.strip():
                    by_idx[idx] = content.strip()
            for j, c in enumerate(batch):
                out[i + j] = by_idx.get(j + 1, c)  # 缺失降级保留原文
        except Exception as e:
            logger.warning("蒸馏批次 %d 失败（全部保留原文）：%s", i // DISTILL_BATCH + 1, str(e)[:100])
            for j, c in enumerate(batch):
                out[i + j] = c
    return out


def import_file(path: Path, embedder, llm=None, distill: bool = False) -> tuple[int, int]:
    """导入单个文档：切块 →（可选蒸馏）→ 向量化 → 入库（hash 幂等）。

    distill=True 时先删除该文档（同 title）旧块再导入（重建语义，防残留重复）。
    返回 (新增块数, 跳过块数)。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = split_chunks(text, path.stem)
    if distill:
        if llm is None:
            raise ValueError("distill 模式需要 llm")
        chunks = distill_chunks(chunks, llm)
        logger.info("文件 %s：蒸馏后 %d 块", path.name, len(chunks))
    logger.info("文件 %s：切块 %d", path.name, len(chunks))
    with get_session() as session:
        existing = {
            r[0]
            for r in session.execute(
                select(KnowledgeChunk.source_hash)
            ).all()
        }
        if distill:  # 重建语义：删除同源旧块
            old = session.scalars(
                select(KnowledgeChunk).where(KnowledgeChunk.title == path.stem)
            ).all()
            for o in old:
                session.delete(o)
            commit(session)
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
    parser.add_argument("--distill", action="store_true",
                        help="LLM 蒸馏为高密度知识点（讲解/叙事型文档推荐；重建该文档旧块）")
    args = parser.parse_args()

    init_db("sqlite:///data/interview.db")
    cfg = load_config(Path("config.yaml"))
    embedder = Embedder()
    llm = None
    if args.distill:
        llm = LLMClient(
            cfg.llm.generate_model,
            cfg.llm.base_url,
            secret_value(cfg.llm.api_key_env),
        )

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
            new, _ = import_file(f, embedder, llm=llm, distill=args.distill)
            total_new += new
        except Exception as e:
            logger.warning("文件 %s 导入失败：%s", f.name, str(e)[:120])
    if total_new > 0:
        bump_version()
    logger.info("完成：新增 %d 块", total_new)


if __name__ == "__main__":
    main()
