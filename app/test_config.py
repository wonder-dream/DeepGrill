"""配置的读取行为 —— 尤其是**密钥从哪来**。

两个理由值得单独一个文件：

① **`.env` 支持是后加的**：第一版只有环境变量、没有 `env_file`，而文档里写着
   "v1 用 config.yaml + .env 两套来源" —— 读者会以为 `.env` 能用。实测确认它
   当时**不生效**，于是补上并在这里钉住。
② **`.env` 装的是 API key**：它必须被 git 挡住，而 `.env.example` 必须能进库。
   这条一旦失效，后果是一次真实的凭据泄露（v1 就把真实 key 打进过部署包，
   见 `docs/v1现状-20260913.md` §6-P4），所以它值得一条测试。

⚠️ **这些测试不碰仓库根那个 `.env`**。开发者本机配好 key 之后它会长期存在，
而第一版的做法是"发现它存在就 skip" —— 于是三项 `.env` 行为在**唯一真正用它
的机器上从来没被验过**（实测：3 skipped）。现在改成把 `env_file` 指到临时文件，
测试与真实配置互不干扰，也不再 skip。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings

#: 本机配了 key 之后，仓库根会长期存在一个 .env。测试必须**显式选择**读哪个文件，
#: 否则"默认值"这类断言会读到开发者的真实配置（实测踩过：配好 key 之后
#: `test_defaults_when_nothing_is_configured` 立刻开始失败）。
_ENV_KEYS = (
    "DEEPGRILL_LLM_API_KEY",
    "DEEPGRILL_LLM_BASE_URL",
    "DEEPGRILL_MODEL_INTERVIEWER",
    "DEEPGRILL_DATABASE_PATH",
    "DEEPGRILL_SESSION_COOKIE_SECURE",
)


@pytest.fixture
def isolated(monkeypatch):
    """不读任何 .env、也不读真实环境变量 —— 只测代码里的默认值。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    return monkeypatch


@pytest.fixture
def dotenv(tmp_dir: Path, monkeypatch):
    """把 `env_file` 指到临时目录里的一个 .env，返回一个"写配置"的函数。

    为什么不写到仓库根：那是开发者的真实配置，测试**不许碰它**。
    而 `env_file` 是 `model_config` 里的一项，monkeypatch 它即可 —— 于是
    "读 .env"这条代码路径被真实验证，且与本机配置完全隔离。
    """
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_dir / ".env"
    monkeypatch.setitem(Settings.model_config, "env_file", path)

    def write(text: str) -> None:
        path.write_text(text, encoding="utf-8")

    return write


def test_defaults_when_nothing_is_configured(isolated) -> None:
    """没有任何配置时：**应用能起来**（key 为空），模型名是配置里的默认值。"""
    s = Settings()
    assert s.model_interviewer == "deepseek-flash"
    assert s.llm_api_key == ""
    assert s.resolved_database_path().is_absolute()


def test_dotenv_is_loaded(dotenv) -> None:
    """`.env` 里的值必须生效 —— 第一版没有 `env_file`，写了也不读。"""
    dotenv("DEEPGRILL_LLM_API_KEY=sk-from-dotenv\nDEEPGRILL_SESSION_COOKIE_SECURE=true\n")
    s = Settings()
    assert s.llm_api_key == "sk-from-dotenv"
    assert s.session_cookie_secure is True


def test_real_environment_overrides_dotenv(dotenv, monkeypatch) -> None:
    """**环境变量优先于 `.env`**：临时换一次不该去改文件。"""
    dotenv("DEEPGRILL_MODEL_INTERVIEWER=model-from-dotenv\n")
    assert Settings().model_interviewer == "model-from-dotenv"

    monkeypatch.setenv("DEEPGRILL_MODEL_INTERVIEWER", "model-from-env")
    assert Settings().model_interviewer == "model-from-env"


def test_missing_dotenv_falls_back_to_defaults(dotenv) -> None:
    """`.env` 不存在是**正常状态**（干净克隆就是这样），不该报错。"""
    assert Settings().model_interviewer == "deepseek-flash"


def test_env_prefix_is_required(dotenv, monkeypatch) -> None:
    """不带前缀的名字读不到 —— 前缀是"与机器上其他变量不撞名"的保证。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-no-prefix")
    assert Settings().llm_api_key != "sk-no-prefix"


def test_dotenv_path_is_anchored_to_the_repo_root() -> None:
    """`.env` 的路径必须**相对仓库根**，不是相对 cwd。

    与库路径、prompt 路径同一条纪律：从别的目录启动不该静默读到另一个配置。
    （实测过同类入口：`migrations` 的默认库路径、`prompts` 的加载路径。）
    """
    assert Settings.model_config.get("env_file") == REPO_ROOT / ".env"


def test_dotenv_is_gitignored_but_the_template_is_not() -> None:
    """**安全底线**：`.env`（装密钥）必须被忽略，`.env.example`（模板）必须能进库。

    `git check-ignore` 的退出码：0 = 被忽略，1 = 没被忽略。
    """
    def ignored(name: str) -> bool:
        return subprocess.run(
            ["git", "check-ignore", name], cwd=REPO_ROOT, capture_output=True
        ).returncode == 0

    assert ignored(".env"), "`.env` 没被 gitignore —— 一次误 `git add` 就是凭据泄露"
    assert ignored(".env.local")
    assert not ignored(".env.example"), "模板要能进库，否则别人不知道要配什么"
