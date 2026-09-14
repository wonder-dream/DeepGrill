"""观测：把"系统现在什么状态"算成一组可显示的指标（决策 23 的「最小观测」）。

决策 23 把三件事一起定了：**导出我的数据 / 中途退出继续 / 最小观测**，并写明
"**观测是三项里最重要的一项**"。理由很直接：没有观测，前面所有"降级不静默"的
设计都只是"记下来了"，而没人看得到。

## 它观测什么

```
离线任务   jobs 表：各状态计数 + 最近失败 + 最老的一条 pending（队列是否堵住）
待定池     有题没挂上知识点的数量（决策 46：挂不上就等，不自动新建知识点）
库体积     SQLite 文件 + WAL 的大小（2C2G 机器上这是要盯的数字）
质量仪表板 question_flags：矛盾记录 / 挂载可疑（ADR-0002 说的"白送的能力"）
离线报告   task_logs 最近几条（生成 / 质检 / 挂载 / 编译）
规模       题 / 知识点 / 考察点 / 用户
```

## 为什么它**不调 LLM、也不写库**

观测页会被反复打开（排查时尤其），所以它必须便宜且只读。任何"顺手算一个需要模型的
指标"都会让这个页面变贵、变慢、并且在模型挂掉时不可用 —— 而它恰恰是你要用来判断
"是不是模型挂了"的那个页面。

## 为什么它住在 `web/`

它要同时看多个领域的表（题库 / 知识层 / 账号 / 离线任务），而领域之间互不 import
（ADR-0005）。所以它只能是页面层的只读查询，不是任何一个领域的 `repository`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Criterion,
    Job,
    KnowledgePoint,
    Question,
    QuestionFlag,
    TaskLog,
    User,
)


@dataclass
class JobSummary:
    counts: dict[str, int] = field(default_factory=dict)
    oldest_pending: Job | None = None
    recent_failures: list[Job] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass
class LibrarySummary:
    questions: int = 0
    public_questions: int = 0
    private_questions: int = 0
    unmounted: int = 0          # 待定池（决策 46）
    knowledge_points: int = 0
    draft_points: int = 0       # 没经过人审的容器节点
    criteria: int = 0
    users: int = 0

    @property
    def mounted_ratio(self) -> float | None:
        """公共题里已挂载的比例。**None = 一道公共题都没有**（不是 0）。

        "一道都没有"与"一道都没挂上"是两件事：前者是"知识层还没建"，后者是
        "建了但挂不上"。显示成同一个 0% 会让这两种状态分不出来。
        """
        if self.public_questions == 0:
            return None
        return (self.public_questions - self.unmounted) / self.public_questions


@dataclass
class QualitySummary:
    open_flags: dict[str, int] = field(default_factory=dict)
    recent_flags: list[QuestionFlag] = field(default_factory=list)


@dataclass
class TaskReport:
    """一行"离线任务报告"，已经把 JSON 说成人话 —— **模板不做判断**。"""

    id: int
    kind: str
    created_at: str
    text: str
    failed: bool


@dataclass
class Observability:
    jobs: JobSummary
    library: LibrarySummary
    quality: QualitySummary
    task_logs: list[TaskReport]
    db_bytes: int
    db_wal_bytes: int

    @property
    def db_total_bytes(self) -> int:
        return self.db_bytes + self.db_wal_bytes


def _job_summary(session: Session) -> JobSummary:
    out = JobSummary()
    rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    for status, n in rows:
        out.counts[str(status)] = int(n)
    out.oldest_pending = session.execute(
        select(Job).where(Job.status == "pending").order_by(Job.id).limit(1)
    ).scalars().first()
    out.recent_failures = list(
        session.execute(
            select(Job).where(Job.status == "failed").order_by(Job.id.desc()).limit(5)
        )
        .scalars()
        .all()
    )
    return out


def _library_summary(session: Session) -> LibrarySummary:
    out = LibrarySummary()

    def count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for cond in where:
            stmt = stmt.where(cond)
        return int(session.execute(stmt).scalar_one())

    out.questions = count(Question)
    out.public_questions = count(Question, Question.owner_user_id.is_(None))
    out.private_questions = count(Question, Question.owner_user_id.is_not(None))
    # 待定池**只数公共题**：私有题没挂载是正常的，混进来会让这个数字失去意义。
    out.unmounted = count(
        Question, Question.owner_user_id.is_(None), Question.primary_point_id.is_(None)
    )
    out.knowledge_points = count(KnowledgePoint)
    out.draft_points = count(KnowledgePoint, KnowledgePoint.status == "draft")
    out.criteria = count(Criterion)
    out.users = count(User)
    return out


def _quality_summary(session: Session) -> QualitySummary:
    out = QualitySummary()
    rows = session.execute(
        select(QuestionFlag.kind, func.count())
        .where(QuestionFlag.status == "open")
        .group_by(QuestionFlag.kind)
    ).all()
    for kind, n in rows:
        out.open_flags[str(kind)] = int(n)
    out.recent_flags = list(
        session.execute(
            select(QuestionFlag)
            .where(QuestionFlag.status == "open")
            .order_by(QuestionFlag.id.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    return out


def _task_logs(session: Session, limit: int = 5) -> list[TaskReport]:
    """最近几行离线报告。

    `jobs` 是队列（终态会被回收者删掉），`task_logs` 是**报告**（不回收）——
    两份都要有：前者答"现在队列怎么样"，后者答"上个月那次跑出什么"。
    """
    rows = session.execute(select(TaskLog).order_by(TaskLog.id.desc()).limit(limit)).scalars()
    out: list[TaskReport] = []
    for row in rows:
        result = row.result if isinstance(row.result, dict) else {}
        text = result.get("message") or result.get("error") or "—"
        out.append(
            TaskReport(
                id=row.id,
                kind=row.kind,
                created_at=row.created_at,
                text=str(text),
                failed=result.get("status") == "failed",
            )
        )
    return out


def _file_sizes(db_path: Path) -> tuple[int, int]:
    """主库与 WAL 的字节数。**WAL 也要看** —— 它在写入繁忙时会明显长大。"""
    main = db_path.stat().st_size if db_path.is_file() else 0
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal = wal_path.stat().st_size if wal_path.is_file() else 0
    return main, wal


def collect(session: Session, *, db_path: Path) -> Observability:
    """把上面那些指标一次算出来。**只读、不调模型。**"""
    main, wal = _file_sizes(db_path)
    return Observability(
        jobs=_job_summary(session),
        library=_library_summary(session),
        quality=_quality_summary(session),
        task_logs=_task_logs(session),
        db_bytes=main,
        db_wal_bytes=wal,
    )


def human_bytes(n: int) -> str:
    """把字节数说成人话。观测页上"2.3 MB"比"2411724"好读得多。"""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
