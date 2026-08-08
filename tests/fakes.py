import math
import random


class FakeLLM:
    """预录响应队列：每次调用弹出下一条；可注入异常序列模拟失败。

    接口与 LLMClient.complete 鸭子一致（生产代码无需分支），供全项目测试复用。
    """

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def complete(self, messages, json_schema=None, timeout=None) -> str | dict:
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSource:
    """假采集源：返回预置 Source 列表或抛出预置异常（SourceProvider 协议）。"""

    def __init__(self, name="fake", sources=None, error=None):
        self.name = name
        self._sources = list(sources or [])
        self.error = error

    def collect(self):
        if self.error is not None:
            raise self.error
        return self._sources


class FakeEmbedder:
    """假 embedding：预置 stem → 归一化向量；未预置给确定性伪随机向量；可注入异常。

    接口与 Embedder.encode 鸭子一致（生产代码无需分支）。
    """

    def __init__(self, vectors: dict[str, list[float]] | None = None, error=None):
        self.vectors = vectors or {}
        self.error = error
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.error is not None:
            raise self.error
        out = []
        for t in texts:
            if t in self.vectors:
                out.append(self.vectors[t])
            else:
                rng = random.Random(t)
                v = [rng.uniform(-1, 1) for _ in range(64)]  # 高维避免随机向量误撞相似度
                norm = math.sqrt(sum(x * x for x in v))
                out.append([x / norm for x in v])
        return out
