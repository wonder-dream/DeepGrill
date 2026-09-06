import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .errors import ConfigError, EnvVarMissing

# GitHub 源默认许可白名单：允许存储/改写/再分发的宽松许可
DEFAULT_ALLOWED_LICENSES = [
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "Unlicense",
    "CC0-1.0",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class LLMConfig(_FrozenModel):
    base_url: str
    api_key_env: str
    generate_model: str
    judge_model: str


class NowcoderConfig(_FrozenModel):
    cookie_env: str
    request_interval: float
    retries: int


class DailyConfig(_FrozenModel):
    max_new_questions: int
    knowledge_limit: int
    design_limit: int
    project_limit: int
    chain_max_rounds: int
    schedule: str = "08:00"


class GitHubRepo(_FrozenModel):
    """单个 GitHub 源配置：repo 必填；expected_license/manual_license 可选。"""

    repo: str
    expected_license: str | None = None
    manual_license: str | None = None


class SourcesConfig(_FrozenModel):
    # github_repos 支持字符串简写（"owner/repo"）或对象（GitHubRepo）；validator 归一化
    github_repos: list[GitHubRepo] = []
    github_require_license: bool = True
    github_allowed_licenses: list[str] = DEFAULT_ALLOWED_LICENSES
    # 牛客采集源开关：公开/生产默认关闭，仅本地个人学习场景显式开启
    nowcoder_enabled: bool = False

    @field_validator("github_repos", mode="before")
    @classmethod
    def _coerce_github_repos(cls, v):
        """兼容旧配置：纯字符串 "owner/repo" 归一化为 GitHubRepo 对象。"""
        if v is None:
            return []
        out = []
        for item in v:
            if isinstance(item, str):
                out.append({"repo": item})
            elif isinstance(item, dict):
                out.append(item)
            else:
                raise ValueError(f"invalid github_repos item: {item!r}")
        return out


class UgcConfig(_FrozenModel):
    enabled: bool = True
    max_per_user_per_day: int = 10
    max_content_bytes: int = 20 * 1024 * 1024
    require_consent: bool = True


class FeedbackConfig(_FrozenModel):
    enabled: bool = True
    duplicate_candidate_min_sim: float = 0.78  # 含边界
    duplicate_max_select: int = 3


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    llm: LLMConfig
    nowcoder: NowcoderConfig
    daily: DailyConfig
    sources: SourcesConfig
    ugc: UgcConfig = UgcConfig()
    feedback: FeedbackConfig = FeedbackConfig()


def load_config(path: Path) -> AppConfig:
    """加载 config.yaml（含同目录 .env 的密钥），pydantic 校验后返回。

    - 密钥只声明环境变量名，值从 os.environ 读取；.env 文件兜底，真实环境变量优先
    - 任何配置错误抛 ConfigError（env 未设置抛 EnvVarMissing），fail fast
    """
    load_dotenv(path.parent / ".env")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}") from e
    except UnicodeDecodeError as e:
        raise ConfigError(f"config file must be utf-8 encoded: {path}") from e
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid yaml in {path}: {e}") from e
    if data is None:
        data = {}
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e)) from e
    secret_value(config.llm.api_key_env)
    # 牛客 cookie 可选：缺失/过期时 nowcoder 源运行时降级停用（其余源不受影响）
    return config


def secret_value(env_name: str) -> str:
    """读取声明的环境变量值（须先经 load_config 加载 .env）；未设置/空串抛 EnvVarMissing。"""
    value = os.environ.get(env_name)
    if not value:
        raise EnvVarMissing(env_name)
    return value


def _format_validation_error(e: ValidationError) -> str:
    details = "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
    )
    return f"invalid config: {details}"
