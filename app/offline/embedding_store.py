"""嵌入的缓存层（ADR-0008 的"嵌入走 API" + AGENTS.md §3.2 的回收者）。

**为什么必须有缓存**：全量装配要嵌入几千条文本，而重跑是常态（心跳回退、调阈值
再试、分批失败重来）。没有缓存，每次重跑都重新花钱 —— 而"重跑"恰恰是这个管道
最常做的事。缓存同时让聚类**可复现**：同一批输入得到同一批向量。

## 键与失效

`(kind, ref_id)` 是主键，`source_hash` 是**被嵌入那段文本**的哈希：

· `kind='question'` → `ref_id` 是题目 id（文本 = 题干 + 考察点）
· `kind='candidate'` → `ref_id` 是文本哈希（候选还没有 id）
· 文本改了 → 哈希对不上 → **自动重算**（与 `explanation_cache` 同一个手法：
  比"再存一列原文来比对"少一处会漏的条件）

## 存储形态

向量存 base64 的 float32 字节。1536 维下 JSON 数组约 30KB/行，float32 是 6KB ——
几千行时差几十 MB，而目标机器是 2C2G（ADR-0008）。
"""

from __future__ import annotations

import base64
import hashlib
from array import array
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import Embedding

#: 回收者的两个上限（§3.2）。向量不会"过时"，但表会随题目增长 ——
#: 容量上限是兜底，TTL 用来清掉长期没人再用到的行。
TTL_DAYS = 365
MAX_ROWS = 50000

QUESTION = "question"
CANDIDATE = "candidate"


def pack_vector(vector: list[float]) -> str:
    """float32 字节 → base64 文本。"""
    return base64.b64encode(array("f", [float(v) for v in vector]).tobytes()).decode("ascii")


def unpack_vector(blob: str) -> list[float]:
    """base64 文本 → float32 字节 → 浮点列表。"""
    raw = base64.b64decode(blob.encode("ascii"))
    values = array("f")
    values.frombytes(raw)
    return [float(v) for v in values]


def text_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def ref_for_text(text: str) -> str:
    """候选这一类用的 ref_id —— 它还没有 id，所以用文本哈希当 id。"""
    return text_hash(text)


def question_text(question, criteria: list | None = None) -> str:
    """一道题要被嵌入的那段文本。

    **题干 + 考察点**，不是只有题干：同一道题换一组考察点意味着它考的东西变了，
    而那正是聚类该看见的差别。
    """
    parts = [question.stem or ""]
    for criterion in criteria or []:
        text = getattr(criterion, "text", criterion)
        if text:
            parts.append(str(text))
    return "\n".join(parts)


@dataclass
class EmbedReport:
    embedded: int = 0
    reused: int = 0

    @property
    def total(self) -> int:
        return self.embedded + self.reused


def vectors_for(
    session: Session, *, items: dict[str, str], kind: str, embeddings
) -> tuple[dict[str, list[float]], EmbedReport]:
    """取 `{ref_id: 要嵌入的文本}` 的向量，**命中缓存的不再调供应商**。

    返回 `(ref_id → 向量, 报告)`。报告里的 `embedded`/`reused` 是**测试与观测要能
    区分**的那两个数：把它们混起来，"缓存失效了、每次都在花钱"就会静默。
    """
    report = EmbedReport()
    if not items:
        return {}, report

    model = getattr(embeddings, "model", "") or "unknown"
    hashes = {ref: text_hash(text) for ref, text in items.items()}

    cached: dict[str, Embedding] = {}
    for row in session.execute(
        select(Embedding).where(
            Embedding.kind == kind, Embedding.ref_id.in_(list(items.keys()))
        )
    ).scalars():
        if row.model == model and row.source_hash == hashes[row.ref_id]:
            cached[row.ref_id] = row

    todo = [ref for ref in items if ref not in cached]
    out: dict[str, list[float]] = {ref: unpack_vector(row.vector) for ref, row in cached.items()}
    report.reused = len(cached)

    if todo:
        fresh = embeddings.embed([items[ref] for ref in todo])
        if len(fresh) != len(todo):
            # 少一条向量会让"哪条文本对应哪个向量"整体错位 —— 宁可当场炸
            raise ValueError(f"嵌入返回 {len(fresh)} 条，期望 {len(todo)} 条")
        for ref, vector in zip(todo, fresh, strict=True):
            out[ref] = vector
            _upsert(session, kind=kind, ref_id=ref, model=model,
                    source_hash=hashes[ref], vector=vector)
        session.flush()
        report.embedded = len(todo)

    return out, report


def _upsert(
    session: Session, *, kind: str, ref_id: str, model: str, source_hash: str, vector: list[float]
) -> Embedding:
    row = session.get(Embedding, (kind, ref_id))
    if row is None:
        row = Embedding(kind=kind, ref_id=ref_id)
        session.add(row)
    row.model = model
    row.source_hash = source_hash
    row.dim = len(vector)
    row.vector = pack_vector(vector)
    row.created_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return row


@dataclass
class PurgeReport:
    by_age: int = 0
    by_capacity: int = 0

    @property
    def total(self) -> int:
        return self.by_age + self.by_capacity

    def __str__(self) -> str:
        return f"按时间清 {self.by_age} 条、按容量清 {self.by_capacity} 条"


def purge(
    session: Session, *, ttl_days: int = TTL_DAYS, max_rows: int = MAX_ROWS
) -> PurgeReport:
    """清掉过期与超量的向量。**幂等**（跑两遍结果一样），所以能做成离线任务。

    容量那一步用"第 N 新的 `created_at`"当分界，而不是"删掉不在前 N 名里的行"：
    这张表的主键是 `(kind, ref_id)` 复合键（没有单列 id 可用来 `NOT IN`）。
    代价是同秒并列时会多留几条 —— 容量上限本来就是兜底，不是精确配额。
    """
    report = PurgeReport()
    cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    report.by_age = (
        session.execute(delete(Embedding).where(Embedding.created_at < cutoff)).rowcount or 0
    )

    total = int(session.execute(select(func.count()).select_from(Embedding)).scalar_one())
    if total > max_rows:
        boundary = session.execute(
            select(Embedding.created_at)
            .order_by(Embedding.created_at.desc())
            .offset(max_rows - 1)
            .limit(1)
        ).scalar_one_or_none()
        if boundary is not None:
            report.by_capacity = (
                session.execute(
                    delete(Embedding).where(Embedding.created_at < boundary)
                ).rowcount
                or 0
            )
    session.flush()
    return report
