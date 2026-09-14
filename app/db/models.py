"""全部表的 ORM 映射（ADR-0005：表集中放这里，不散落到各领域）。

**为什么用 mapper 风格而不是 `Mapped[...]` 注解**：`0001_initial.sql` 是手写的，
我们**故意**不让 SQLAlchemy 去推断类型 —— 那类解析发生在 import 时，正是本项目
最怕的失败形状（"本地能跑、换个写法就炸"）。`registry.map_imperatively` 只吃显式
`Column`，列名写错在 import 时就报。

**映射的完整性由测试守**（`app/db/test_models.py`）：它把 `0001_initial.sql` 解析成
「表 → 列」，再和这里的 `metadata` 逐表对一遍 —— "加了一张表忘了映射"或"列名漂了"
是红测试，而不是几个月后某个 `no such column`。

三条容易漏、但都由那张表说了算的约定：

① **时间列是 `TEXT`**（库里存的是 `datetime('now')` 的字符串），不是 DATETIME。
② **DEFAULT 属于 DDL，必须写进映射**（`server_default`）—— 漏了的话，用 ORM 写入
   与用 SQL 写入行为不一致：ORM 会显式送 NULL，于是撞 NOT NULL。实测撞过。
③ **JSON 列用 `JsonText`**（`docs/v1行为规格.md` §8.7 硬性继承）：中文不许转义成
   `\\uXXXX`，否则对 JSON 列的 SQL 文本匹配会静默失效。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.orm import registry

from app.db import JsonText

__all__ = [
    "metadata",
    "Attempt",
    "CandidateProfile",
    "Criterion",
    "Domain",
    "Evaluation",
    "Interview",
    "InviteCode",
    "Job",
    "KnowledgePoint",
    "KnowledgePointEdge",
    "Question",
    "QuestionFeedback",
    "QuestionFlag",
    "QuestionPoint",
    "QuestionPointStat",
    "QuotaLedger",
    "ReportItem",
    "Role",
    "RolePoint",
    "Session_",
    "TaskLog",
    "User",
    "UserFavorite",
    "UserToken",
]

mapper_registry = registry()
metadata: MetaData = mapper_registry.metadata

#: `datetime('now')` —— 库里所有时间列的默认值。抽成常量是为了让"这是 DDL 的
#: 默认值"在每一行都可读，而不是一串看起来像业务代码的表达式。
_NOW = func.datetime("now")


# ---------------------------------------------------------------------------
# 账号侧
# ---------------------------------------------------------------------------
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String, nullable=False, unique=True),
    Column("username", String, nullable=False),
    Column("password_hash", String, nullable=False),
    Column("role", String, nullable=False, server_default="user"),
    Column("created_at", String, nullable=False, server_default=_NOW),
)

invite_codes = Table(
    "invite_codes",
    metadata,
    Column("code", String, primary_key=True),
    Column("created_by", ForeignKey("users.id")),
    Column("expires_at", String),
    Column("used_by", ForeignKey("users.id")),
    Column("used_at", String),
)

user_tokens = Table(
    "user_tokens",
    metadata,
    Column("token_hash", String, primary_key=True),
    Column("user_id", ForeignKey("users.id"), nullable=False),
    Column("expires_at", String, nullable=False),
    Column("created_at", String, nullable=False, server_default=_NOW),
)

quota_ledger = Table(
    "quota_ledger",
    metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    # kind = day / month —— 日月两种粒度同表（决策 23 + 补齐的 kind 列）
    Column("kind", String, primary_key=True, server_default="day"),
    Column("day", String, primary_key=True),
    Column("units_used", Integer, nullable=False, server_default="0"),
    Column("tokens_used", Integer, nullable=False, server_default="0"),
    Column("updated_at", String, nullable=False, server_default=_NOW),
)

# ---------------------------------------------------------------------------
# 知识侧
# ---------------------------------------------------------------------------
domains = Table(
    "domains",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("parent_id", ForeignKey("domains.id")),
    Column("name", String, nullable=False),
)

roles = Table(
    "roles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=False, unique=True),
)

knowledge_points = Table(
    "knowledge_points",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("domain_id", ForeignKey("domains.id"), nullable=False),
    Column("name", String, nullable=False),
    Column("status", String, nullable=False, server_default="draft"),
    Column("origin", String, nullable=False, server_default="proposed"),
    Column("exclusions", Text),
    Column("question_count", Integer, nullable=False, server_default="0"),
)

criteria = Table(
    "criteria",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("point_id", ForeignKey("knowledge_points.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("text", Text, nullable=False),
    # 共用考察点：ADR-0002 判据② 的前提
    Column("shared", Integer, nullable=False, server_default="0"),
)

role_points = Table(
    "role_points",
    metadata,
    Column("role_id", ForeignKey("roles.id"), primary_key=True),
    Column("point_id", ForeignKey("knowledge_points.id"), primary_key=True),
)

knowledge_point_edges = Table(
    "knowledge_point_edges",
    metadata,
    Column("from_point_id", ForeignKey("knowledge_points.id"), primary_key=True),
    Column("to_point_id", ForeignKey("knowledge_points.id"), primary_key=True),
    Column("kind", String, primary_key=True),
)

# ---------------------------------------------------------------------------
# 题库侧
# ---------------------------------------------------------------------------
questions = Table(
    "questions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String, nullable=False),
    Column("stem", Text, nullable=False),
    Column("difficulty", Integer, nullable=False),
    Column("primary_point_id", ForeignKey("knowledge_points.id")),
    # 离线素材（聚类用）。**运行时逻辑只准读 criteria 表**（决策 49）
    Column("good_criteria", Text),
    Column("bad_criteria", Text),
    Column("answer_tier", String),
    Column("reference_answer", Text),
    Column("origin", String, nullable=False, server_default="generated"),
    # 非空 = 私有题集；空 = 公共题库。任何查询都必须过滤它（AGENTS.md §3.5）
    Column("owner_user_id", ForeignKey("users.id")),
    Column("visibility", String, nullable=False, server_default="public"),
    Column("created_at", String, nullable=False, server_default=_NOW),
)

question_points = Table(
    "question_points",
    metadata,
    Column("question_id", ForeignKey("questions.id"), primary_key=True),
    Column("point_id", ForeignKey("knowledge_points.id"), primary_key=True),
    Column("source", String, nullable=False, server_default="authored"),
)

question_flags = Table(
    "question_flags",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("question_id", ForeignKey("questions.id"), nullable=False),
    Column("kind", String, nullable=False),
    Column("detail", Text),
    Column("status", String, nullable=False, server_default="open"),
)

candidate_profiles = Table(
    "candidate_profiles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", ForeignKey("users.id"), nullable=False),
    Column("structured", JsonText, nullable=False),
    Column("source_note", Text),
    Column("created_at", String, nullable=False, server_default=_NOW),
)

# ---------------------------------------------------------------------------
# 作答侧
# ---------------------------------------------------------------------------
interviews = Table(
    "interviews",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", ForeignKey("users.id"), nullable=False),
    # interview / drill —— browse 不建面试行（决策 63 旁的收敛）
    Column("mode", String, nullable=False),
    Column("plan", JsonText),
    Column("status", String, nullable=False, server_default="active"),
    Column("quota_charged", Integer, nullable=False, server_default="0"),
    Column("report_body", JsonText),
    Column("report_summary", Text),
    Column("started_at", String, nullable=False, server_default=_NOW),
    Column("ended_at", String),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("interview_id", ForeignKey("interviews.id"), nullable=False),
    Column("question_id", ForeignKey("questions.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("status", String, nullable=False, server_default="active"),
    Column("max_rounds", Integer, nullable=False),
)

report_items = Table(
    "report_items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("interview_id", ForeignKey("interviews.id"), nullable=False),
    Column("session_id", ForeignKey("sessions.id")),
    Column("seq", Integer, nullable=False),
    Column("snap_stem", Text),
    Column("snap_question_kind", String),
    Column("snap_difficulty", Integer),
    Column("snap_point_name", String),
    Column("snap_criteria", JsonText),
)

attempts = Table(
    "attempts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("round_no", Integer, nullable=False),
    Column("is_followup", Integer, nullable=False, server_default="0"),
    Column("input_mode", String, nullable=False, server_default="text"),
    Column("stt_text", Text),
    Column("answer_text", Text),
    Column("feedback_text", Text),
    # 累积快照：criterion_id → 命中 / 未命中 / 未涉及（决策 28）
    Column("hits", JsonText),
)

evaluations = Table(
    "evaluations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False),
    Column("scores", JsonText),
    Column("total_score", Float),
    Column("review", Text),
    Column("status", String, nullable=False, server_default="ok"),
)

# ---------------------------------------------------------------------------
# 治理侧
# ---------------------------------------------------------------------------
user_favorites = Table(
    "user_favorites",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", ForeignKey("users.id"), nullable=False),
    Column("question_id", ForeignKey("questions.id"), nullable=False),
    Column("created_at", String, nullable=False, server_default=_NOW),
)

question_feedback = Table(
    "question_feedback",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("question_id", ForeignKey("questions.id"), nullable=False),
    Column("user_id", ForeignKey("users.id")),
    Column("kind", String, nullable=False),
    Column("detail", Text),
    Column("duplicate_question_ids", JsonText),
    Column("created_at", String, nullable=False, server_default=_NOW),
    # `0002_feedback_workflow.sql` 加的列（v1 的工单三态，§未决 12）
    Column("status", String, nullable=False, server_default="open"),
)

task_logs = Table(
    "task_logs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String, nullable=False),
    Column("payload", JsonText),
    Column("result", JsonText),
    Column("created_at", String, nullable=False, server_default=_NOW),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String, nullable=False),
    Column("payload", JsonText),
    Column("status", String, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="3"),
    Column("worker_id", String),
    Column("heartbeat_at", String),
    Column("progress", Float),
    Column("message", Text),
    Column("error", Text),
    Column("created_at", String, nullable=False, server_default=_NOW),
    Column("started_at", String),
    Column("finished_at", String),
)

question_point_stats = Table(
    "question_point_stats",
    metadata,
    Column("question_id", ForeignKey("questions.id"), primary_key=True),
    Column("point_id", ForeignKey("knowledge_points.id"), primary_key=True),
    # 键是 criterion_id（不是位置）—— 与 attempts.hits 同一个键（决策 28）
    Column("criterion_id", ForeignKey("criteria.id"), primary_key=True),
    Column("hit_count", Integer, nullable=False, server_default="0"),
    Column("miss_count", Integer, nullable=False, server_default="0"),
    Column("updated_at", String, nullable=False, server_default=_NOW),
)


class _Row:
    """让映射出来的类能 `Row(**kwargs)` 构造，也能 `repr` 出主键。

    mapper 风格不带 `__init__`（那是 Declarative 给的），这里补一个 ——
    它让"用关键字造一行"在测试、脚本与迁移工具里都可用。
    """

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {getattr(self, 'id', None)}>"

    def to_dict(self) -> dict[str, Any]:
        """这一行的全部列 → 普通 dict（JSON 列已经是 dict/list，可直接序列化）。

        给"导出我的数据"（决策 23）这类**要给人看/带走**的路径用：它必须导出
        全部字段，所以列清单**只有一个来源**（`CLASS_BY_TABLE` 里的 Table），
        不许在导出代码里手抄一遍 —— 手抄的那份必然会随加列而过期。
        """
        for table, cls in CLASS_BY_TABLE.items():
            if cls is type(self):
                return {c.name: getattr(self, c.name, None) for c in table.columns}
        return {}


class User(_Row):
    pass


class InviteCode(_Row):
    pass


class UserToken(_Row):
    pass


class QuotaLedger(_Row):
    pass


class Domain(_Row):
    pass


class Role(_Row):
    pass


class KnowledgePoint(_Row):
    pass


class Criterion(_Row):
    pass


class RolePoint(_Row):
    pass


class KnowledgePointEdge(_Row):
    pass


class Question(_Row):
    pass


class QuestionPoint(_Row):
    pass


class QuestionFlag(_Row):
    pass


class CandidateProfile(_Row):
    pass


class Interview(_Row):
    pass


class Session_(_Row):
    """`sessions` 表。类名带下划线，因为 `Session` 已被 SQLAlchemy 占用。"""


class ReportItem(_Row):
    pass


class Attempt(_Row):
    pass


class Evaluation(_Row):
    pass


class UserFavorite(_Row):
    pass


class QuestionFeedback(_Row):
    pass


class TaskLog(_Row):
    pass


class Job(_Row):
    pass


class QuestionPointStat(_Row):
    pass


# ---------------------------------------------------------------------------
# 映射
# ---------------------------------------------------------------------------
#: 表 → 类。`Session.one(表名, pk)` 也靠它（见 app/db/__init__.py）。
CLASS_BY_TABLE: dict[Table, type[_Row]] = {
    users: User,
    invite_codes: InviteCode,
    user_tokens: UserToken,
    quota_ledger: QuotaLedger,
    domains: Domain,
    roles: Role,
    knowledge_points: KnowledgePoint,
    criteria: Criterion,
    role_points: RolePoint,
    knowledge_point_edges: KnowledgePointEdge,
    questions: Question,
    question_points: QuestionPoint,
    question_flags: QuestionFlag,
    candidate_profiles: CandidateProfile,
    interviews: Interview,
    sessions: Session_,
    report_items: ReportItem,
    attempts: Attempt,
    evaluations: Evaluation,
    user_favorites: UserFavorite,
    question_feedback: QuestionFeedback,
    task_logs: TaskLog,
    jobs: Job,
    question_point_stats: QuestionPointStat,
}

for _table, _cls in CLASS_BY_TABLE.items():
    mapper_registry.map_imperatively(_cls, _table)
