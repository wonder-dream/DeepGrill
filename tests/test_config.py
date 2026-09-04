import os

import pytest
import yaml

from app.config import load_config, secret_value
from app.errors import ConfigError, EnvVarMissing

VALID_YAML = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "LLM_API_KEY",
        "generate_model": "deepseek-chat",
        "judge_model": "deepseek-reasoner",
    },
    "nowcoder": {
        "cookie_env": "NOWCODER_COOKIE",
        "request_interval": 1.5,
        "retries": 3,
    },
    "daily": {
        "max_new_questions": 36,
        "knowledge_limit": 5,
        "design_limit": 2,
        "project_limit": 1,
        "chain_max_rounds": 20,
        "schedule": "08:00",
    },
    "notification": {"enabled": True},
    "sources": {"github_repos": ["a/b"], "nowcoder_enabled": True},
}


def write_yaml(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("NOWCODER_COOKIE", "cookie-test")


# --- happy ---


def test_full_config_maps_correctly(tmp_path, env):
    path = write_yaml(tmp_path, VALID_YAML)
    cfg = load_config(path)

    assert cfg.llm.base_url == "https://api.deepseek.com/v1"
    assert cfg.llm.api_key_env == "LLM_API_KEY"
    assert cfg.llm.generate_model == "deepseek-chat"
    assert cfg.llm.judge_model == "deepseek-reasoner"
    assert cfg.nowcoder.cookie_env == "NOWCODER_COOKIE"
    assert cfg.nowcoder.request_interval == 1.5
    assert cfg.nowcoder.retries == 3
    assert cfg.daily.max_new_questions == 36
    assert cfg.daily.knowledge_limit == 5
    assert cfg.daily.design_limit == 2
    assert cfg.daily.project_limit == 1
    assert cfg.daily.chain_max_rounds == 20
    assert cfg.daily.schedule == "08:00"
    assert cfg.notification.enabled is True
    assert [r.repo for r in cfg.sources.github_repos] == ["a/b"]
    assert cfg.sources.github_repos[0].manual_license is None
    assert cfg.sources.github_repos[0].expected_license is None
    assert cfg.sources.nowcoder_enabled is True


def test_sources_license_policy_and_object_form(tmp_path, env):
    data = {
        **VALID_YAML,
        "sources": {
            "nowcoder_enabled": False,
            "github_require_license": True,
            "github_allowed_licenses": ["MIT", "Apache-2.0"],
            "github_repos": [
                "owner/plain",
                {"repo": "owner/manual", "manual_license": "with-author-permission"},
                {"repo": "owner/expected", "expected_license": "Apache-2.0"},
            ],
        },
    }
    cfg = load_config(write_yaml(tmp_path, data))

    assert cfg.sources.github_require_license is True
    assert cfg.sources.github_allowed_licenses == ["MIT", "Apache-2.0"]
    assert [r.repo for r in cfg.sources.github_repos] == [
        "owner/plain",
        "owner/manual",
        "owner/expected",
    ]
    assert cfg.sources.github_repos[1].manual_license == "with-author-permission"
    assert cfg.sources.github_repos[2].expected_license == "Apache-2.0"


def test_github_require_license_defaults_true(tmp_path, env):
    data = {**VALID_YAML, "sources": {"github_repos": ["a/b"]}}
    cfg = load_config(write_yaml(tmp_path, data))
    assert cfg.sources.github_require_license is True
    assert "MIT" in cfg.sources.github_allowed_licenses


def test_secrets_read_from_dotenv_file(tmp_path, monkeypatch):
    write_yaml(tmp_path, VALID_YAML)
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=sk-from-dotenv\nNOWCODER_COOKIE=cookie-from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("NOWCODER_COOKIE", raising=False)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.llm.api_key_env == "LLM_API_KEY"
    assert cfg.nowcoder.cookie_env == "NOWCODER_COOKIE"
    assert os.environ["LLM_API_KEY"] == "sk-from-dotenv"
    assert os.environ["NOWCODER_COOKIE"] == "cookie-from-dotenv"


# --- edge ---


def test_optional_fields_use_defaults(tmp_path, env):
    data = {
        **VALID_YAML,
        "daily": {**VALID_YAML["daily"], "schedule": "10:30"},
        "notification": {},
        "sources": {"github_repos": []},
    }
    del data["daily"]["schedule"]
    cfg = load_config(write_yaml(tmp_path, data))

    assert cfg.daily.schedule == "08:00"
    assert cfg.notification.enabled is True
    assert cfg.sources.github_repos == []
    assert cfg.sources.nowcoder_enabled is False


def test_extra_fields_ignored(tmp_path, env):
    data = {**VALID_YAML, "future_field": 1, "llm": {**VALID_YAML["llm"], "extra": "x"}}
    cfg = load_config(write_yaml(tmp_path, data))
    assert cfg.llm.generate_model == "deepseek-chat"


def test_environment_variable_takes_precedence_over_dotenv(tmp_path, monkeypatch):
    write_yaml(tmp_path, VALID_YAML)
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=sk-from-dotenv\nNOWCODER_COOKIE=cookie-from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
    monkeypatch.setenv("NOWCODER_COOKIE", "cookie-from-env")
    load_config(tmp_path / "config.yaml")
    assert os.environ["LLM_API_KEY"] == "sk-from-env"
    assert os.environ["NOWCODER_COOKIE"] == "cookie-from-env"


# --- fail ---


def test_file_not_found_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_invalid_yaml_raises_config_error(tmp_path, env):
    path = tmp_path / "config.yaml"
    path.write_text("llm: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid yaml"):
        load_config(path)


def test_non_utf8_encoding_raises_config_error(tmp_path, env):
    path = tmp_path / "config.yaml"
    path.write_bytes("llm:\n  base_url: 中文注释".encode("gbk"))
    with pytest.raises(ConfigError, match="utf-8"):
        load_config(path)


def test_type_error_raises_config_error(tmp_path, env):
    data = {**VALID_YAML, "daily": {**VALID_YAML["daily"], "knowledge_limit": "many"}}
    with pytest.raises(ConfigError, match="knowledge_limit"):
        load_config(write_yaml(tmp_path, data))


def test_missing_required_field_raises_config_error(tmp_path, env):
    data = {k: v for k, v in VALID_YAML.items() if k != "llm"}
    with pytest.raises(ConfigError, match="llm"):
        load_config(write_yaml(tmp_path, data))


def test_empty_yaml_file_raises_config_error(tmp_path, env):
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_api_key_env_raises_env_var_missing(tmp_path, monkeypatch):
    write_yaml(tmp_path, VALID_YAML)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("NOWCODER_COOKIE", "cookie-test")
    with pytest.raises(EnvVarMissing) as exc:
        load_config(tmp_path / "config.yaml")
    assert exc.value.name == "LLM_API_KEY"


def test_empty_env_value_counts_as_missing(tmp_path, monkeypatch):
    write_yaml(tmp_path, VALID_YAML)
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("NOWCODER_COOKIE", "cookie-test")
    with pytest.raises(EnvVarMissing, match="LLM_API_KEY"):
        load_config(tmp_path / "config.yaml")


def test_missing_cookie_env_allowed(tmp_path, monkeypatch):
    """牛客 cookie 可选（服务器无 cookie 也能启动，源运行时降级停用）。"""
    write_yaml(tmp_path, VALID_YAML)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.delenv("NOWCODER_COOKIE", raising=False)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.nowcoder.cookie_env == "NOWCODER_COOKIE"


# --- secret_value ---


def test_secret_value_returns_env_value(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    assert secret_value("LLM_API_KEY") == "sk-test"


def test_secret_value_missing_raises(monkeypatch):
    monkeypatch.delenv("NOWCODER_COOKIE", raising=False)
    with pytest.raises(EnvVarMissing) as exc:
        secret_value("NOWCODER_COOKIE")
    assert exc.value.name == "NOWCODER_COOKIE"
