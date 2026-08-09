import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import ConfigError, EnvVarMissing


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


class NotificationConfig(_FrozenModel):
    enabled: bool = True


class SourcesConfig(_FrozenModel):
    github_repos: list[str] = []


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    llm: LLMConfig
    nowcoder: NowcoderConfig
    daily: DailyConfig
    notification: NotificationConfig
    sources: SourcesConfig


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
