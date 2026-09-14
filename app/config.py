"""配置：**唯一读环境变量的地方**（ADR-0005 的基础设施层）。

模型名按用途命名、集中在配置里（决策 50）：代码只引用用途名
（`settings.model_interviewer`），不写死型号串 —— 换模型是改配置，不是改代码。

**顺序**（后者覆盖前者，pydantic-settings 的默认语义）：

```
类默认值  →  .env 文件  →  真实环境变量
```

于是 `.env` 能当"本机默认值"，而临时换一次只要在环境变量里给一下
（`$env:DEEPGRILL_LLM_API_KEY = "sk-…"`）—— 不必去改文件。

`.env` 的路径**相对仓库根解析**（不是 cwd），理由与库路径、prompt 路径相同：
从别的目录启动不该静默读到另一个配置（ADR-0010 记的同类入口）。

为什么要显式 `env_prefix`：v1 用 `config.yaml` + `.env` 两套来源、同一个键
在两边都出现过，于是"当前值到底是哪个"要靠读代码确认。v2 只有一个来源
（环境，含 `.env` 这一层），前缀让 `DEEPGRILL_*` 与机器上其他变量不会撞名。
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

    model_config = SettingsConfigDict(
        env_prefix="DEEPGRILL_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 库路径相对**仓库根**解析，不是相对 cwd —— 否则从别的目录启动会静默
    # 指向另一个库（ADR-0010 记的同类入口）。
    database_path: Path = REPO_ROOT / "data" / "interview.db"

    # 用途名 → 型号串（决策 50）。MVP 只用到面试官这一个用途；
    # 对话与判分共用它，是因为 MVP 不追求"判分用更强的模型"这个优化。
    #
    # 默认值是与 `.env.example` **对齐的**、且**实测可用**的那一组
    # （deepseek-flash + https://api.deepseek.com）—— 模板与代码默认值不一致时，
    # "照模板配"和"什么都不配"会得到两种行为，而那种分歧没有任何好处。
    model_interviewer: str = "deepseek-flash"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"

    #: 语音转写的供应商（决策 32：面试页默认语音）。
    #:
    #: `"none"`（默认）= **没有接** —— 每次调用都明确失败，页面让用户改用打字
    #: （降级可以，静默不行：绝不返回一段假转写去骗判分）。
    #: `"fake"` = 占位实现，返回一段**标着占位**的文字，只为让整条语音链路
    #: （录音 → 转写 → 判分 → 落库）在没有供应商时也能被走通。
    #: 接真实供应商时在这里加一个值 —— 模型名同样按用途命名（决策 50）。
    stt_provider: str = "none"

    #: 嵌入的供应商（ADR-0008：**嵌入走 API**，不跑本地模型）。
    #:
    #: `"none"`（默认）= 没接。**DeepSeek 不提供 embeddings**（调 `/v1/embeddings`
    #: 直接 404），所以"用哪一家"是一个还没定的决策 —— 没配就明确失败，
    #: 而不是拿一段假向量糊过去（那会让聚类看起来跑通了）。
    #: `"fake"` = 确定性的词袋哈希向量：让整条装配管道（分批 → 聚类 → 逐簇判断）
    #: 在没有供应商时也能被真的走一遍，而且结果可复现。**它不是语义嵌入**。
    #: `"api"` = OpenAI 兼容的 `/embeddings`（下面三项要配齐）。
    embedding_provider: str = "none"
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    # Cookie 的 Secure 属性。默认关（http 本机开发）；线上必须开 ——
    # 上线检查项，不是默认值。
    session_cookie_secure: bool = False

    #: 启动时是否**拒绝带着占位口令的库**（决策 58）。
    #:
    #: 默认 `False`：本机开发就是从一个占位 owner 开始的（`0001` 里那条 INSERT），
    #: 而"开发时也要先改口令"只会让人绕开这个检查 —— 那比没有检查更糟。
    #: **线上必须设成 `true`**：那时"上线前记得替换"不再是一句提醒，而是启动就拦。
    require_secure_db: bool = False

    #: 进程内限流（决策 66：继承 v1 的滑动窗口 + reserve/settle）。
    #:
    #: `ratelimit_requests` / `ratelimit_llm_requests` 分别按 **IP** 与 **用户** 限，
    #: 因为它们的用途不同：前者防扫描与单机灌流量，后者管面试官动作的成本
    #: （那是 v1 那条"4xx 释放额度"语义的落点）。细节见 `app/ratelimit.py`。
    ratelimit_enabled: bool = True
    ratelimit_requests: int = 120
    ratelimit_window_seconds: float = 60.0
    ratelimit_auth_requests: int = 10
    ratelimit_auth_window_seconds: float = 300.0
    ratelimit_llm_requests: int = 20

    #: 是否信任反向代理的转发头（`CF-Connecting-IP` / `X-Forwarded-For`）。
    #:
    #: **默认 false**：转发头是客户端能自己写的，信了它等于把"按 IP 限流"变成
    #: "按客户端随便填的字符串限流"。线上跑在 Cloudflare 后面时必须开。
    trust_proxy_headers: bool = False

    def resolved_database_path(self) -> Path:
        """把相对路径解析到仓库根下。"""
        p = self.database_path
        return p if p.is_absolute() else (REPO_ROOT / p)
