"""配置：**唯一读环境变量的地方**（ADR-0005 的基础设施层）。

模型名按用途命名、集中在配置里（决策 50）：代码只引用用途名
（`settings.model_interviewer`），不写死型号串 —— 换模型是改配置，不是改代码。

为什么要显式 `env_prefix`：v1 用 `config.yaml` + `.env` 两套来源，同一个键
在两边出现过，于是"当前值到底是哪个"要靠读代码确认。v2 只有一个来源即环境，
前缀让 `DEEPGRILL_*` 与机器上其他变量不会撞名。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根：本文件在 app/ 下，所以是上两级。
REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """全部运行期配置。

    有默认值的一项判据：**它必须能在开发机上直接跑起来**。
    没有默认值的一项判据：**猜错了会有真实后果**（如 API key）。
    """

    model_config = SettingsConfigDict(env_prefix="DEEPGRILL_", extra="ignore")

    # 库路径相对**仓库根**解析，不是相对 cwd —— 否则从别的目录启动会静默
    # 指向另一个库（ADR-0010 记的同类入口）。
    database_path: Path = REPO_ROOT / "data" / "interview.db"

    # 用途名 → 型号串（决策 50）。MVP 只用到面试官这一个用途；
    # 对话与判分共用它，是因为 MVP 不追求"判分用更强的模型"这个优化。
    model_interviewer: str = "deepseek-v4.1-flash"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"

    # Cookie 的 Secure 属性。默认关（http 本机开发）；线上必须开 ——
    # 上线检查项，不是默认值。
    session_cookie_secure: bool = False

    def resolved_database_path(self) -> Path:
        """把相对路径解析到仓库根下。"""
        p = self.database_path
        return p if p.is_absolute() else (REPO_ROOT / p)
