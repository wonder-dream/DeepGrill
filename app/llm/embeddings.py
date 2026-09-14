"""嵌入（ADR-0008：**走 API，不跑本地模型**）。

## 为什么是 API 而不是本地模型

ADR-0008 的约束是 2C2G 单机：本地嵌入模型要常驻几百 MB 到几 GB 内存，而它的收益
（省下每次几分钱）在这台机器上不成立。所以这一层与 `LLMClient` 一样是**同步的
HTTP 客户端**。

## 这一版只有接口 + 占位实现（与 STT 同一个模式）

**DeepSeek 没有 embeddings 接口** —— 调 `/v1/embeddings` 直接 404
（[issue #806](https://github.com/deepseek-ai/DeepSeek-V3/issues/806)），所以
"用哪个供应商"是一个**还没定的决策**，而不是能顺手写死的东西。于是：

| | |
|---|---|
| `Embeddings` | 一个方法：`embed(texts) -> list[list[float]]` |
| `NoProviderEmbeddings` | **默认实现**：任何调用都抛 `EmbeddingUnavailable` |
| `FakeEmbeddings` | `DEEPGRILL_EMBEDDING_PROVIDER=fake`：**确定性**的词袋哈希向量 |
| `APIEmbeddings` | OpenAI 兼容的 `/embeddings`（接真实供应商时配 base_url/key/model 即可） |

`FakeEmbeddings` 不是"随便返回点东西"：它把文本切成字符 n-gram 再哈希到固定维度
并 L2 归一化，于是**用词相近的文本向量也相近** —— 聚类、阈值、召回这些逻辑因此
能在没有供应商时被真的验证（而不是只测"调用次数对不对"）。

⚠️ 它仍然**不是**语义嵌入：同义不同词（"线程池"vs"线程复用"）不会被它拉到一起。
真跑全量装配之前必须接上真实供应商 —— 这条写在 `INDEX.md` 的"还没有"里。
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Sequence
from typing import Protocol

import httpx

#: 占位实现的维度。选 256 是因为它够大（碰撞少）又够小（测试里肉眼可读）。
FAKE_DIM = 256

#: 一次请求最多带多少条文本。供应商一般都有限制（按 token 计），分批是必须的。
DEFAULT_BATCH = 64

_BACKOFF_BASE = 1.0


class EmbeddingError(Exception):
    """嵌入层的失败基类。"""


class EmbeddingUnavailable(EmbeddingError):
    """**没有接供应商** —— 与"调用失败"不是一回事。

    与 `STTUnavailable` 分开的理由相同：前者的处置是"去配一下"，后者是"重试或降级"。
    """


class Embeddings(Protocol):
    """嵌入接口。`model` 要带在结果上 —— 换模型之后旧向量必须重算。"""

    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class NoProviderEmbeddings:
    """默认实现：没有配置供应商。**任何调用都明确失败。**"""

    model = "none"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise EmbeddingUnavailable(
            "嵌入还没接入（没有配置供应商）—— DeepSeek 不提供 embeddings，"
            "需要另配一家（见 .env.example 的 DEEPGRILL_EMBEDDING_*）"
        )


def _ngrams(text: str, n: int = 2) -> list[str]:
    cleaned = "".join(ch for ch in text.lower() if not ch.isspace())
    if len(cleaned) < n:
        return [cleaned] if cleaned else []
    return [cleaned[i : i + n] for i in range(len(cleaned) - n + 1)]


def fake_vector(text: str, *, dim: int = FAKE_DIM) -> list[float]:
    """确定性的词袋哈希向量（L2 归一化）。

    同一段文本永远得到同一个向量；用词重叠多的两段文本余弦相似度高。
    **确定性**是它能当测试替身的前提 —— 随机向量会让聚类结果每次不同，
    而那种测试只能断言"没崩"。
    """
    vector = [0.0] * dim
    grams = _ngrams(text)
    if not grams:
        return vector
    for gram in grams:
        digest = hashlib.sha1(gram.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        # 符号也由哈希决定：否则所有分量同号，任意两段文本的余弦都会被抬高
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


class FakeEmbeddings:
    """占位实现（`DEEPGRILL_EMBEDDING_PROVIDER=fake`）。

    它让"分批 → 聚类 → 逐簇判断 → 挂载"这条链在没有供应商时也能被真的走一遍，
    而且结果**可复现**（同样的输入，同样的聚类）。
    """

    def __init__(self, *, model: str = "fake-embedding", dim: int = FAKE_DIM) -> None:
        self.model = model
        self.dim = dim
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [fake_vector(t, dim=self.dim) for t in texts]


class APIEmbeddings:
    """OpenAI 兼容的 `/embeddings`。

    三条与 `LLMClient` 相同的纪律（它们来自同一组实测结论）：

    ① **屏蔽 SDK 重试、只用本层重试**：3 次、退避 1s → 2s
    ② **只对 5xx / 连接错误 / 超时重试，4xx 立即抛** —— 4xx 是请求本身的问题
    ③ **用量记在客户端上**（`usage_total`），由调用方取差值记进账本（决策 14）
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 3,
        batch_size: int = DEFAULT_BATCH,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise EmbeddingError("缺少嵌入 API key（设置 DEEPGRILL_EMBEDDING_API_KEY）")
        self.model = model
        self.batch_size = batch_size
        self._max_retries = max_retries
        self.usage_total: dict[str, int] = {"prompt_tokens": 0, "calls": 0}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            out.extend(self._embed_batch(list(texts[start : start + self.batch_size])))
        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        if not batch:
            return []
        payload = {"model": self.model, "input": batch}
        body = self._post_with_retry("/embeddings", payload)
        try:
            items = body["data"]
        except (KeyError, TypeError) as e:
            raise EmbeddingError(f"返回体形状不符合预期：{body!r:.200}") from e

        # 只认下标，不认顺序：返回体的顺序在协议里没被保证
        by_index: dict[int, list[float]] = {}
        for item in items:
            try:
                by_index[int(item["index"])] = [float(v) for v in item["embedding"]]
            except (KeyError, TypeError, ValueError) as e:
                raise EmbeddingError(f"返回体里有读不了的向量：{item!r:.200}") from e
        missing = [i for i in range(len(batch)) if i not in by_index]
        if missing:
            # 不静默：少一条向量会让"第 N 条文本对应哪个向量"整体错位
            raise EmbeddingError(f"返回体缺了 {len(missing)} 条向量（下标 {missing}）")

        usage = body.get("usage") or {}
        self.usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage_total["calls"] += 1
        return [by_index[i] for i in range(len(batch))]

    def _post_with_retry(self, path: str, payload: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self._max_retries):
            if attempt:
                time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
            try:
                response = self._client.post(path, json=payload)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last = e
                continue
            if 200 <= response.status_code < 300:
                return response.json()
            if 500 <= response.status_code < 600:
                last = EmbeddingError(f"服务端错误 {response.status_code}")
                continue
            raise EmbeddingError(
                f"请求被拒绝（{response.status_code}）：{response.text[:200]}"
            )
        raise EmbeddingError(f"重试 {self._max_retries} 次仍失败：{last}")
