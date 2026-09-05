import json as _json
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import BLOB, CheckConstraint, Column, Text, UniqueConstraint
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
    resume = "resume"


class QuestionType(str, Enum):
    knowledge = "knowledge"
    design = "design"
    project = "project"


class SessionKind(str, Enum):
    open = "open"
    chain = "chain"


class SessionStatus(str, Enum):
    active = "active"
    finished = "finished"


class SubmissionKind(str, Enum):
    facejing = "facejing"
    resume = "resume"
    direct = "direct"


class SubmissionStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    removed = "removed"


class FeedbackCategory(str, Enum):
    wrong = "wrong"
    unclear = "unclear"
    duplicate = "duplicate"
    not_interview = "not_interview"
    other = "other"


class FeedbackStatus(str, Enum):
    open = "open"
    resolved = "resolved"
    dismissed = "dismissed"


class Source(SQLModel, table=True):
    """题目来源（爬取/导入管线）：只增不改，source_hash 幂等去重。"""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "type IN ('nowcoder', 'github', 'manual', 'resume')",
            name="ck_sources_type",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    type: SourceType
    url: Optional[str] = None
    title: str = ""
    cleaned_text: str = ""  # 生成管线的唯一输入（raw_text 已移除）
    fetched_at: datetime = Field(default_factory=datetime.now)
    source_hash: str = Field(unique=True, index=True)
    # 来源溯源（GitHub 源）：许可 SPDX / 作者(owner) / 仓库 URL；历史行与手动源可空
    license: Optional[str] = None
    author: Optional[str] = None
    repo_url: Optional[str] = None


class Question(SQLModel, table=True):
    """题目：判分/审核/推荐核心。tags 走 question_tags 关联；审核建议与进度入库。"""

    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint(
            "type IN ('knowledge', 'design', 'project')",
            name="ck_questions_type",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="sources.id")  # 来源只增不改，无级联
    type: QuestionType
    stem: str
    difficulty: int = Field(default=1)
    good_criteria: list[str] = Field(default_factory=list, sa_column=Column(JSONUtf8))
    bad_criteria: list[str] = Field(default_factory=list, sa_column=Column(JSONUtf8))
    # 审核域（人工审核流程）：建议快照 + 进度，审核完成后一并清空
    suggested_category: str = ""
    suggested_tags: list[str] = Field(default_factory=list, sa_column=Column(JSONUtf8))
    suggested_difficulty: int = Field(default=1)
    suggested_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None  # NULL=未审核；非空=已审时间
    selected_at: Optional[datetime] = None  # 被选为今日待做的时刻（历史按此分组）
    embedding: Optional[bytes] = None  # bge-m3 向量（float32 BLOB），去重/检索用
    created_at: datetime = Field(default_factory=datetime.now)


class TagCategory(SQLModel, table=True):
    """标签分类词表：种子（is_custom=0）与自定义均可管理；非空删除由业务层 409 拦截。

    roles（空=全岗位通用）与 lang（仅 backend 语言方向）驱动岗位×语言推荐。
    """

    __tablename__ = "tag_categories"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    is_custom: int = Field(default=0)  # 0=种子（代码初始），1=自定义
    roles: list[str] = Field(default_factory=list, sa_column=Column(JSONUtf8))  # 岗位集合，空=全岗位
    lang: Optional[str] = None  # 语言方向（java/python/go/C/C++），仅 backend 岗位
    created_at: datetime = Field(default_factory=datetime.now)


class Tag(SQLModel, table=True):
    """词表标签：归属分类；删除时 DB 级联清 question_tags 关联。"""

    __tablename__ = "tags"

    id: Optional[int] = Field(default=None, primary_key=True)
    category_id: int = Field(foreign_key="tag_categories.id", ondelete="CASCADE", index=True)
    name: str = Field(unique=True, index=True)  # 全局唯一（检索/统计按名）
    is_custom: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)


class QuestionTag(SQLModel, table=True):
    """题目-标签多对多：复合主键防重复关联；删题/删标签 DB 级联清。"""

    __tablename__ = "question_tags"

    question_id: int = Field(foreign_key="questions.id", ondelete="CASCADE", primary_key=True)
    tag_id: int = Field(foreign_key="tags.id", ondelete="CASCADE", primary_key=True)


class Session(SQLModel, table=True):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('open', 'chain')",
            name="ck_sessions_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'finished')",
            name="ck_sessions_status",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(foreign_key="questions.id", ondelete="CASCADE")
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    kind: SessionKind
    status: SessionStatus = SessionStatus.active
    started_at: datetime = Field(default_factory=datetime.now)
    ended_at: Optional[datetime] = None

    attempts: list["Attempt"] = Relationship(
        back_populates="session",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "passive_deletes": True},
    )
    judgments: list["Judgment"] = Relationship(
        back_populates="session",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "passive_deletes": True},
    )


class Attempt(SQLModel, table=True):
    __tablename__ = "attempts"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="sessions.id", ondelete="CASCADE")
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
    session_id: int = Field(foreign_key="sessions.id", ondelete="CASCADE")
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


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)  # 登录标识（邮箱验证码注册）
    username: str = ""  # 显示名（注册时自动取邮箱前缀；保留旧列兼容迁移）
    password_hash: str
    role: str = "user"  # owner（管理员，可上传/题库管理/审核）| user
    focus: Optional[str] = None  # 求职岗位（frontend/backend/ai_app/qa/ai_infra，空=未设置）
    focus_lang: Optional[str] = None  # 后端语言方向（java/python/go/C/C++，仅 focus=backend）
    created_at: datetime = Field(default_factory=datetime.now)


class UserToken(SQLModel, table=True):
    __tablename__ = "user_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    token_hash: str = Field(unique=True)
    expires_at: Optional[datetime] = None  # 过期时间（30 天）
    created_at: datetime = Field(default_factory=datetime.now)


class UserPick(SQLModel, table=True):
    """每用户选题记录：今日题 = 该用户 picked_at 属今天的记录（每用户池单轨）。"""

    __tablename__ = "user_picks"
    __table_args__ = (
        UniqueConstraint("user_id", "question_id", name="uq_user_picks"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    question_id: int = Field(foreign_key="questions.id", ondelete="CASCADE")
    picked_at: datetime = Field(default_factory=datetime.now)


class UserFavorite(SQLModel, table=True):
    """用户收藏：同用户同题唯一（幂等收藏），删题/删用户级联清。"""

    __tablename__ = "user_favorites"
    __table_args__ = (UniqueConstraint("user_id", "question_id", name="uq_user_favorites"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    question_id: int = Field(foreign_key="questions.id", ondelete="CASCADE")
    created_at: datetime = Field(default_factory=datetime.now)


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_name: str
    status: str = "success"
    fetched_count: int = 0
    generated_count: int = 0
    error: str = ""
    ran_at: datetime = Field(default_factory=datetime.now)


class KnowledgeChunk(SQLModel, table=True):
    """知识库块（RAG）：八股文/资料切块 + bge-m3 向量，判分/追问/复习卷注入用。"""

    __tablename__ = "knowledge_chunks"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = ""  # 来源文档标题
    content: str = ""
    source_hash: str = Field(unique=True, index=True)  # 内容 hash，幂等去重
    embedding: Optional[bytes] = None  # bge-m3 向量（float32 BLOB）
    created_at: datetime = Field(default_factory=datetime.now)


class KnowledgeMeta(SQLModel, table=True):
    """知识库版本号（单行 id=1）：导入成功 version+1，检索索引据此自动重建。"""

    __tablename__ = "knowledge_meta"

    id: Optional[int] = Field(default=None, primary_key=True)
    version: int = Field(default=0)


class Submission(SQLModel, table=True):
    """UGC 用户提交原稿：保存授权记录与溯源；题目质量由 Question 审核门禁把关。"""

    __tablename__ = "submissions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('facejing', 'resume', 'direct')",
            name="ck_submissions_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'removed')",
            name="ck_submissions_status",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    kind: SubmissionKind
    title: str = ""
    content: str = ""
    consent: bool = False
    status: SubmissionStatus = SubmissionStatus.pending
    source_id: Optional[int] = Field(default=None, foreign_key="sources.id")
    error: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class QuestionFeedback(SQLModel, table=True):
    """用户对公开题目的质量反馈（非版权举报）：wrong/unclear/duplicate/not_interview/other。"""

    __tablename__ = "question_feedback"
    __table_args__ = (
        CheckConstraint(
            "category IN ('wrong', 'unclear', 'duplicate', 'not_interview', 'other')",
            name="ck_question_feedback_category",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')",
            name="ck_question_feedback_status",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(foreign_key="questions.id", ondelete="CASCADE", index=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    category: FeedbackCategory
    duplicate_question_ids: list[int] = Field(
        default_factory=list, sa_column=Column(JSONUtf8)
    )
    comment: str = ""
    status: FeedbackStatus = FeedbackStatus.open
    created_at: datetime = Field(default_factory=datetime.now)
    resolved_at: Optional[datetime] = None
