import json
import re
import time
from typing import Callable

from openai import APIConnectionError, APIStatusError, OpenAI

from ..errors import LLMError, LLMJsonError

_FENCE_OPEN = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE = re.compile(r"\s*```$")


class LLMClient:
    """统一 OpenAI 兼容调用（DeepSeek/Qwen/OpenAI 可换）：重试/超时/JSON 解析。

    - 指数退避重试 2 次（HTTP 5xx / 超时 / 连接错误），间隔 1s → 2s
    - 带 json_schema 时请求 response_format=json_object，返回解析后的 dict
    - 重试耗尽抛 LLMError；JSON 不可解析抛 LLMJsonError（均 retryable，降级由调用方决定）
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: float = 60,
        *,
        http_client=None,
        backoff_func: Callable[[int], float] | None = None,
    ):
        self._model = model
        self._timeout = timeout
        self._backoff = backoff_func or (lambda attempt: 2.0**attempt)
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,  # 重试由本层控制，屏蔽 SDK 内置重试
            http_client=http_client,
        )

    def complete(
        self,
        messages: list[dict],
        json_schema: dict | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> str | dict:
        """完成调用；max_tokens 默认 8192（防长输出 JSON 截断，见 docs/分批生成方案.md）。"""
        effective_timeout = self._timeout if timeout is None else timeout
        kwargs = {
            "model": self._model,
            "messages": messages,
            "timeout": effective_timeout,
            "max_tokens": 8192 if max_tokens is None else max_tokens,
        }
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        content = self._call_with_retry(
            lambda: self._client.chat.completions.create(**kwargs)
            .choices[0]
            .message.content
            or ""
        )
        if json_schema is not None:
            return self._parse_json(content)
        return content

    def _call_with_retry(self, func):
        last_exc = None
        for attempt in range(3):
            try:
                return func()
            except (APIConnectionError, APIStatusError) as e:
                if isinstance(e, APIStatusError) and e.status_code < 500:
                    raise LLMError(f"llm http error {e.status_code}: {e}") from e
                last_exc = e
                if attempt < 2:
                    time.sleep(self._backoff(attempt))
        raise LLMError(f"llm call failed after retries: {last_exc}") from last_exc

    def _parse_json(self, content: str):
        """剥离围栏后解析 JSON 并原样返回（dict/list/标量均可，类型校验由调用方负责）。"""
        text = _FENCE_OPEN.sub("", content.strip())
        text = _FENCE_CLOSE.sub("", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMJsonError(f"cannot parse llm json output: {e}") from e
