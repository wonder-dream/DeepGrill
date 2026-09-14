"""配置的读取行为 —— 尤其是**密钥从哪来**。

两个理由值得单独一个文件：

① **`.env` 支持是后加的**：第一版只有环境变量、没有 `env_file`，而文档里写着
   "v1 用 config.yaml + .env 两套来源" —— 读者会以为 `.env` 能用。实测确认它
   当时**不生效**，于是补上并在这里钉住。
② **`.env` 装的是 API key**：它必须被 git 挡住，而 `.env.example` 必须能进库。
   这条一旦失效，后果是一次真实的凭据泄露（v1 就把真实 key 打进过部署包，
   见 `docs/v1现状-20260913.md` §6-P4），所以它值得一条测试。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings


@pytest.fixture
def dotenv(tmp_dir: Path):
    """在**仓库根**临时放一个 .env，用完一定删掉。

    为什么必须在仓库根：`.env` 路径是相对仓库根解析的（与库路径、prompt 路径
    同一条纪律）。所以这条测试会短暂地碰仓库根的一个文件 —— 用 try/finally
    保证不留下垃圾（测试**不许污染被它检查的东西**）。
    """
    path = REPO_ROOT / ".env"
    existed = path.read_text(encoding="utf-8") if path.exists() else None
    if existed is not None:
        pytest.skip("仓库根已有一个 .env（本机真实配置）—— 跳过，绝不覆盖它")

    def write(text: str) -> None:
        path.write_text(text, encoding="utf-8")

    yield write
    path.unlink(missing_ok=True)


def test_defaults_when_nothing_is_configured() -> None:
    """没有任何配置时：**应用能起来**（key 为空），模型名是配置里的默认值。"""
    s = Settings()
    assert s.model_interviewer == "deepseek-v4.1-flash"
    assert isinstance(s.llm_api_key, str)
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
    (REPO_ROOT / ".env").unlink(missing_ok=True)
    assert Settings().model_interviewer == "deepseek-v4.1-flash"


def test_env_prefix_is_required(monkeypatch) -> None:
    """不带前缀的名字读不到 —— 前缀是"与机器上其他变量不撞名"的保证。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-no-prefix")
    monkeypatch.delenv("DEEPGRILL_LLM_API_KEY", raising=False)
    assert Settings().llm_api_key != "sk-no-prefix"


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
