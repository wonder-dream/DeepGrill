"""Web 请求体 schema（Pydantic v2）：类型强制 + 长度/枚举约束。

校验失败由 routes.py 的 RequestValidationError 处理器统一转 400 + 中文 detail，
保持前端 `body.detail` 字符串契约。字段省略（局部更新/默认值）不被校验。
"""
from pydantic import BaseModel, Field, field_validator

from ..models import SessionKind

ANSWER_MAX_LEN = 8000  # 回答长度上限：防超大输入刷 LLM 成本
UPLOAD_TYPES = ("auto", "direct", "facejing", "resume")


class CreateSessionBody(BaseModel):
    question_id: int = Field(gt=0)
    kind: str = "chain"

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, v: str) -> str:
        if v not in {k.value for k in SessionKind}:
            raise ValueError(f"kind 仅支持 {'/'.join(k.value for k in SessionKind)}")
        return v


class AnswerBody(BaseModel):
    answer: str = Field(min_length=2, max_length=ANSWER_MAX_LEN)

    @field_validator("answer")
    @classmethod
    def _answer_strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("回答过短")
        return v


class AdminQuestionUpdate(BaseModel):
    """管理员改题：全部可选（局部更新）；显式传 None 视为非法。"""

    stem: str | None = Field(default=None, min_length=6)
    tags: list[str] | None = None
    difficulty: int | None = Field(default=None, ge=1, le=5)
    reviewed: bool | None = None
    good_criteria: list[str] | None = None
    bad_criteria: list[str] | None = None

    @field_validator("stem")
    @classmethod
    def _stem_strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 6:
            raise ValueError("题干过短")
        return v

    @field_validator("good_criteria", "bad_criteria")
    @classmethod
    def _criteria_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("判分标准需为非空列表")
        return v


class UploadBody(BaseModel):
    filename: str = ""
    content: str = ""
    content_base64: str = ""
    type: str = "auto"
    count: int | None = None

    @field_validator("type")
    @classmethod
    def _type_valid(cls, v: str) -> str:
        if v not in UPLOAD_TYPES:
            raise ValueError(f"type 仅支持 {'/'.join(UPLOAD_TYPES)}")
        return v
