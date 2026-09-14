"""LLM 调用层（ADR-0005 的基础设施层）。

**同步，不是 async** —— 这是刻意的：业务层（service / 编排）是同步函数，靠
FastAPI 把同步端点丢进线程池（AGENTS.md §3.3 的反面用法就是这么来的）。
若这里改成 `AsyncClient`，整条调用链都要跟着变 async，而"事件循环里做同步 DB
查询"那条禁令会立刻变成新问题。

契约全部继承 v1（`docs/v1行为规格.md` §1，标为「继承」）—— 那些结论是真实调试
换来的，重写不重测：

| 契约 | 为什么 |
|---|---|
| 屏蔽 SDK 内置重试，只用本层重试 | v1 实测 SDK 重试与外层叠加，实际次数不可预测 |
| 重试 3 次，退避 1s → 2s | 够用且总等待可控 |
| 只对 5xx / 连接错误 / 超时重试，**4xx 立即抛** | 4xx 是请求本身的问题，重试只是浪费配额 |
| 默认 `max_tokens=8192` | 长 JSON 输出被截断会导致解析失败 |
| 结构化输出用 `response_format={"type":"json_object"}` | 提高 JSON 遵从度 |
| 容忍 JSON 尾逗号，且**不得破坏字符串字面量里的逗号与转义引号** | v1 诊断出 9 个解析失败源，根因同一个 |
| 剥离 markdown 代码围栏后再解析 | LLM 常见的包裹行为 |
| **调用失败与解析失败是两种错误** | 二者降级策略不同，调用方要能区分 |
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: 退避基数：第 N 次失败后等 `2**attempt` 秒（1s → 2s）。
_BACKOFF_BASE = 1.0


class LLMError(Exception):
    """LLM 层的失败基类。调用方**必须**区分下面两个子类。"""


class LLMCallError(LLMError):
    """连不上 / 超时 / 5xx（已重试到上限）。降级策略通常是"重试或换路"。"""


class LLMParseError(LLMError):
    """调用成功但拿不到可解析的 JSON。降级策略通常是"丢掉这次输出"。

    与 `LLMCallError` 分开的理由：调用失败可以原样重试，而"模型答了但答的不像
    JSON"重试同一个 prompt 往往还是不像 —— 两者的补救不同（行为规格 §1.8）。
    """


@dataclass
class Prompt:
    """一个 prompt 文件。

    `name` 是相对 `prompts/` 的路径（如 `interviewer/score_round.md`）。文件里用
    `{{变量}}` 占位，`render()` 做**字面替换**（不是模板引擎）—— prompt 正文里
    出现 `{{` 之外的花括号（JSON 示例几乎必然有）不该被当成语法。
    """

    name: str
    text: str
    defaults: dict[str, str] = field(default_factory=dict)

    def render(self, **values: Any) -> str:
        merged = {**self.defaults, **{k: str(v) for k, v in values.items()}}
        out = self.text
        for key, value in merged.items():
            out = out.replace("{{" + key + "}}", value)
        missing = _placeholders(out)
        if missing:
            # 不静默：未填充的占位符送给模型等于让它猜（AGENTS.md §3.1）
            raise LLMError(f"prompt {self.name} 有未填充的占位符：{sorted(missing)}")
        return out


def _placeholders(text: str) -> set[str]:
    import re

    return set(re.findall(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", text))


# ---------------------------------------------------------------------------
# JSON 容错（继承 v1 的 `_strip_trailing_commas`）
# ---------------------------------------------------------------------------
def strip_trailing_commas(text: str) -> str:
    """去掉 `[1,2,]` / `{"a":1,}` 这类尾逗号。

    **必须用状态机**，不能 `re.sub(r",\\s*([}\\]])", r"\\1", text)` —— 后者会把
    字符串字面量里的 `",}"` 一起改掉。v1 的实现就是手写状态机，并特意注明
    "字符串内逗号与转义引号不受影响"；那条性质有测试守着
    （`app/llm/test_client.py`）。
    """
    out: list[str] = []
    in_string = False
    escaped = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == ",":
            # 往后看：只有空白之后紧跟 } 或 ] 时才是尾逗号
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # 丢掉这个逗号
                continue

        out.append(ch)
        i += 1
    return "".join(out)


def strip_code_fence(text: str) -> str:
    """剥掉 ```` ```json ... ``` ```` 围栏（LLM 常见的包裹行为）。"""
    s = text.strip()
    if not s.startswith("```"):
        return s
    first_newline = s.find("\n")
    if first_newline == -1:
        return s.strip("`").strip()
    body = s[first_newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")]
    return body.strip()


def loads_tolerant(text: str) -> Any:
    """尽最大努力把模型输出解析成 JSON。

    顺序：剥围栏 → 先按原样解析 → 失败才去尾逗号再试。
    **先原样**是有意的：绝大多数输出本来就是合法 JSON，状态机不该被无谓地跑一遍
    （跑错的机会也就多一次）。
    """
    cleaned = strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(strip_trailing_commas(cleaned))
    except json.JSONDecodeError as e:
        raise LLMParseError(f"输出不是可解析的 JSON：{e}") from e


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------
@dataclass
class LLMReply:
    """一次调用的结果。`text` 是模型原文，`data` 是（可选）解析后的 JSON。"""

    text: str
    data: Any = None
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 服务端给的结束原因。**必须留着它**：`"length"` 表示输出被 `max_tokens`
    #: 截断 —— 那会让 JSON 不完整，而"不完整的 JSON"如果被当成"模型没答好"，
    #: 表现出来的是一次静默降级，看不出真因（实测确认这个模型会先"想"再答，
    #: 推理也吃 max_tokens 配额）。
    finish_reason: str = ""
    #: 推理型模型先输出的思考内容（不进 content）。它占的 token 已计入
    #: `completion_tokens`，而且**推理本身也吃 `max_tokens`**。
    reasoning_text: str = ""
    reasoning_tokens: int = 0


class LLMClient:
    """OpenAI 兼容的 chat completions 客户端。

    `transport` 是可注入的 —— 测试用 `httpx.MockTransport` 造 4xx / 5xx / 连接错误，
    不需要起服务、也不需要 monkeypatch。
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            # 不静默：没有 key 就明确失败，而不是发一个注定 401 的请求
            raise LLMError("缺少 LLM api_key（设置 DEEPGRILL_LLM_API_KEY）")
        self.model = model
        self._max_retries = max_retries
        #: 这个客户端**累计**用了多少 token。调用处（HTTP 请求边界）读它并把差值
        #: 记进额度账本 —— 决策 14 要"能回答钱花在哪"，而一次请求里可能发生多次
        #: 调用（判定 + 判分 + 总结），逐处手记必然漏。
        self.usage_total: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "calls": 0,
        }
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 主入口 ------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        json_mode: bool = False,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> LLMReply:
        """发一次调用。**内部已重试**，抛出的都是最终失败。

        ⚠️ **用 `json_mode=True` 时，prompt 正文里必须出现 "json" 这个词** ——
        这是服务端对 `response_format={"type":"json_object"}` 的硬要求，不满足会
        返回 **400**（我们把它当"请求本身的问题"、不重试、直接抛）。
        实测踩过：`report_summary.md` 写的是"综合成一段人话"、通篇没有 json，
        于是报告总结**每次都静默降级**成确定性文案。
        `tests/test_llm.py::test_json_mode_prompts_mention_json` 守着这条。
        """
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        body = self._post_with_retry("/chat/completions", payload)
        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            # v1 的"choices[0] 空列表裸 IndexError"就是这一处 —— 那时它是未修的债
            raise LLMCallError(f"返回体形状不符合预期：{body!r:.200}") from e

        text = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        finish_reason = str(choice.get("finish_reason") or "")

        # 决策 14 的第二道安全网：把这次调用的真实用量累计到客户端上。
        # 调用处（HTTP 请求边界）读差值记账 —— 一次请求里可能发生多次调用
        # （判定 + 判分 + 总结），逐处手记必然漏（实测就漏了后两次）。
        self.usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        self.usage_total["reasoning_tokens"] += int(details.get("reasoning_tokens") or 0)
        self.usage_total["calls"] += 1

        reply = LLMReply(
            text=text,
            model=body.get("model", payload["model"]),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=finish_reason,
            reasoning_text=reasoning,
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        )
        if json_mode:
            reply.data = self._parse_json_reply(reply)
        return reply

    def _parse_json_reply(self, reply: LLMReply) -> Any:
        """解析 `json_mode` 的输出，并**先把两种"看起来很安静"的失败挑出来**。

        实测发现的两种（表面上都像"模型答得不好"，真因却在请求侧）：

        ① **被 `max_tokens` 截断**：输出是半个 JSON，解析必然失败。若不区分，调用方
           只看到"解析失败"，会以为模型不行 —— 而其实是预算不够。
           注意：截断但**恰好能解析**时照常返回，不报错（答案本来就完整）。
        ② **内容全空、token 全花在推理上**：这个模型先输出 `reasoning_content`，
           而**推理也吃 `max_tokens`**（实测：467 个完成 token 里 394 个是推理）。
           预算太小时会出现 `content=""` 而 `reasoning_tokens>0` —— 那不是"模型没答"，
           是"还没轮到它答"。
        """
        if not reply.text.strip():
            if reply.reasoning_tokens:
                raise LLMCallError(
                    f"模型把 {reply.reasoning_tokens} 个 token 全用在推理上、没有产出内容"
                    f"（finish_reason={reply.finish_reason or '未知'}）—— 需要调大 max_tokens"
                )
            raise LLMCallError(
                f"模型返回了空内容（finish_reason={reply.finish_reason or '未知'}）"
            )

        try:
            return loads_tolerant(reply.text)
        except LLMParseError:
            if reply.finish_reason == "length":
                raise LLMCallError(
                    f"输出被 max_tokens 截断、JSON 不完整（completion_tokens="
                    f"{reply.completion_tokens}）—— 这不是模型答错，是预算不够"
                ) from None
            raise

    def chat_json(self, messages: Sequence[dict[str, str]], **kwargs: Any) -> tuple[Any, LLMReply]:
        """`chat(json_mode=True)` 的便利形式，返回 `(data, reply)`。"""
        reply = self.chat(messages, json_mode=True, **kwargs)
        return reply.data, reply

    # -- 重试 --------------------------------------------------------------
    def _post_with_retry(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._max_retries):
            if attempt:
                time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
            try:
                r = self._client.post(path, json=payload)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last = e
                logger.warning("LLM 连接失败（第 %d 次）：%s", attempt + 1, e)
                continue

            if 200 <= r.status_code < 300:
                return r.json()

            if 500 <= r.status_code < 600:
                last = LLMCallError(f"服务端错误 {r.status_code}")
                logger.warning("LLM 返回 %d（第 %d 次）", r.status_code, attempt + 1)
                continue

            # 4xx：请求本身的问题，重试只会浪费配额与时间（行为规格 §1.3）
            raise LLMCallError(f"请求被拒绝（{r.status_code}）：{r.text[:200]}")

        raise LLMCallError(f"重试 {self._max_retries} 次仍失败：{last}")
