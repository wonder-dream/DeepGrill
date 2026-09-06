class AppError(Exception):
    """全项目异常基类。"""


class ConfigError(AppError):
    """配置加载/校验错误。"""


class EnvVarMissing(ConfigError):
    """声明的环境变量未设置。"""

    def __init__(self, name: str):
        super().__init__(f"environment variable not set: {name}")
        self.name = name


class StorageError(AppError):
    """存储层错误。"""


class DuplicateSource(StorageError):
    """Source.source_hash UNIQUE 冲突（重复导入幂等标记，由调用方决定跳过）。"""

    def __init__(self, source_hash: str):
        super().__init__(f"duplicate source: {source_hash}")
        self.source_hash = source_hash


class LLMError(AppError):
    """LLM 调用错误（可重试）。"""

    retryable = True


class LLMJsonError(LLMError):
    """LLM 输出 JSON 解析错误（可重试）。"""


class CrawlerError(AppError):
    """采集错误。"""


class NowcoderError(CrawlerError):
    """牛客源错误。"""


class NowcoderAuthError(NowcoderError):
    """牛客登录态失效（cookie 过期/被登出），本日停用该源（不重试、不提示重登）。"""


class GitHubError(CrawlerError):
    """GitHub 源错误。"""


class ImportError(CrawlerError):
    """手动/简历导入错误。"""


class GenerationError(AppError):
    """题目生成错误。"""


class EmbedError(AppError):
    """embedding 计算错误（模型加载/推理失败，调用方降级）。"""


class JudgeError(AppError):
    """判分错误（可重试）。"""

    retryable = True


class ChainStateError(AppError):
    """追问链状态机非法调用（如 finished 后继续）。"""
