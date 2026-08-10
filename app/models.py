import json as _json
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import BLOB, CheckConstraint, Column, Text
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, Relationship, SQLModel


class JSONUtf8(TypeDecorator):
    """JSON 列统一类型：中文不转义存储（ensure_ascii=False）。

    否则库里存 \\uXXXX（如 "缓存" → "\\u7f13\\u5b58"），
    cast(tags, String).like 等 SQL 文本匹配对中文永远匹配不上（历史遗留 bug）。
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return _json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return _json.loads(value)


class SourceType(str, Enum):
    nowcoder = "nowcoder"
    github = "github"
    manual = "manual"
    social = "social"
    resume = "resume"


class QuestionType(str, Enum):
    knowledge = "knowledge"
    design = "design"
    project = "project"


class QuestionStatus(str, Enum):
    pending = "pending"
    today = "today"
    done = "done"
    skipped = "skipped"


class SessionKind(str, Enum):
    open = "open"
    design = "design"
    chain = "chain"


class SessionStatus(str, Enum):
    active = "active"
    finished = "finished"


class Source(SQLModel, table=True):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "type IN ('nowcoder', 'github', 'manual', 'social', 'resume')",
            name="ck_sources_type",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    type: SourceType
    url: Optional[str] = None
    title: str = ""
    raw_text: str = ""
    cleaned_text: str = ""
    fetched_at: datetime = Field(default_factory=datetime.now)
    source_hash: str = Field(unique=True, index=True)


class Question(SQLModel, table=True):
    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint(
            "type IN ('knowledge', 'design', 'project')",
            name="ck_questions_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'today', 'done', 'skipped')",
            name="ck_questions_status",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="sources.id")
    type: QuestionType
    stem: str
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSONUtf8))
    difficulty: int = Field(default=1)
    good_criteria: list[str] = Field(default_factory=list, sa_column=Column(JSONUtf8))
    bad_criteria: list[str] = Field(default_factory=list, sa_column=Column(JSONUtf8))
    status: QuestionStatus = QuestionStatus.pending
    selected_at: Optional[datetime] = None  # 被选为今日待做的时刻（历史按此分组）
    embedding: Optional[bytes] = None  # bge-m3 向量（float32 BLOB），去重/检索用
    created_at: datetime = Field(default_factory=datetime.now)


class Session(SQLModel, table=True):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('open', 'design', 'chain')",
            name="ck_sessions_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'finished')",
            name="ck_sessions_status",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(foreign_key="questions.id")
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    kind: SessionKind
    status: SessionStatus = SessionStatus.active
    started_at: datetime = Field(default_factory=datetime.now)
    ended_at: Optional[datetime] = None

    attempts: list["Attempt"] = Relationship(
        back_populates="session",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    judgments: list["Judgment"] = Relationship(
        back_populates="session",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Attempt(SQLModel, table=True):
    __tablename__ = "attempts"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="sessions.id")
    round_no: int
    is_followup: bool = False
    answer_text: str
    feedback_text: str = ""
    level: Optional[int] = None  # 深挖追问层级（L1-L5，M11 深挖协议）
    quality: Optional[str] = None  # 本轮回答质量（correct|partial|wrong|unsure，供判分参考）

    session: Session = Relationship(back_populates="attempts")


class Judgment(SQLModel, table=True):
    __tablename__ = "judgments"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="sessions.id")
    scores: dict = Field(default_factory=dict, sa_column=Column(JSONUtf8))
    total_score: Optional[int] = None
    review: str = ""
    reference_answer: str = ""
    weak_tags: list[str] = Field(default_factory=list, sa_column=Column(JSONUtf8))
    model: str = ""
    created_at: datetime = Field(default_factory=datetime.now)

    session: Session = Relationship(back_populates="judgments")

    @property
    def status(self) -> str:
        """判分状态（ok/failed），存于 scores JSON（M02 决策，无独立列）。"""
        if isinstance(self.scores, dict):
            return self.scores.get("status", "ok")
        return "ok"


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_name: str
    status: str = "success"
    fetched_count: int = 0
    generated_count: int = 0
    error: str = ""
    ran_at: datetime = Field(default_factory=datetime.now)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    role: str = "user"  # owner（管理员，可上传/管理题库）| user
    created_at: datetime = Field(default_factory=datetime.now)


class UserToken(SQLModel, table=True):
    __tablename__ = "user_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(unique=True)
    expires_at: Optional[datetime] = None  # 过期时间（30 天）；旧数据由迁移回填
    created_at: datetime = Field(default_factory=datetime.now)


class UserPick(SQLModel, table=True):
    """每用户今日选题（替代 Question.status=today 的全局语义，按天自然隔离）。"""

    __tablename__ = "user_picks"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    question_id: int = Field(foreign_key="questions.id")
    picked_at: datetime = Field(default_factory=datetime.now)


