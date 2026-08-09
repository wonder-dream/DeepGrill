import httpx
import pytest

from app.errors import LLMError, LLMJsonError
from app.llm.llm_client import LLMClient

MSG = [{"role": "user", "content": "hi"}]


def completion(content):
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def make_client(handler, backoff=None):
    """handler: httpx.Request -> httpx.Response；backoff 记录退避序列并禁用真实睡眠。"""
    record = []

    def backoff_recorder(attempt):
        record.append(attempt)
        return 0

    client = LLMClient(
        model="test-model",
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        backoff_func=backoff or backoff_recorder,
    )
    return client, record


# --- happy ---


def test_complete_returns_text():
    client, _ = make_client(lambda request: completion("你好，面试官"))
    assert client.complete(MSG) == "你好，面试官"


def test_max_tokens_default_8192():
    """长输出 JSON 截断修复：请求默认带 max_tokens=8192（docs/分批生成方案.md）。"""
    import json as _json

    seen = {}

    def handler(request):
        seen["body"] = _json.loads(request.content)
        return completion('{"ok": true}')

    client, _ = make_client(handler)
    client.complete(MSG, json_schema={})
    assert seen["body"]["max_tokens"] == 8192


def test_complete_returns_dict_with_schema():
    client, _ = make_client(lambda request: completion('{"a": 1}'))
    assert client.complete(MSG, json_schema={}) == {"a": 1}


@pytest.mark.parametrize(
    "content",
    ['```json\n{"a": 1}\n```', '```\n{"a": 1}\n```', '  ```json\n{"a": 1}\n```  '],
)
def test_complete_strips_json_fence(content):
    client, _ = make_client(lambda request: completion(content))
    assert client.complete(MSG, json_schema={}) == {"a": 1}


# --- edge ---


def test_empty_content_in_text_mode():
    client, _ = make_client(lambda request: completion(""))
    assert client.complete(MSG) == ""


def test_long_content_roundtrip():
    long_text = "x" * 100_000
    client, _ = make_client(lambda request: completion(long_text))
    assert client.complete(MSG) == long_text


def test_schema_mode_with_text_response_raises():
    client, _ = make_client(lambda request: completion("抱歉，无法输出 JSON"))
    with pytest.raises(LLMJsonError):
        client.complete(MSG, json_schema={})


def test_schema_mode_with_array_json_returns_list():
    """M8 契约是 JSON 数组：解析结果原样返回，类型校验由调用方负责。"""
    client, _ = make_client(lambda request: completion('[{"a": 1}]'))
    assert client.complete(MSG, json_schema={}) == [{"a": 1}]


@pytest.mark.parametrize(
    "content, expected",
    [
        ('[1,2,]', [1, 2]),  # 数组尾逗号
        ('{"a": 1,}', {"a": 1}),  # 对象尾逗号
        ('[{"a":1}, {"b":2},]', [{"a": 1}, {"b": 2}]),  # 嵌套数组尾逗号
        ('{"s": "包含,}"}', {"s": "包含,}"}),  # 字符串内 ,} 不受影响
        ('{"a": [1, 2,], "b": 3,}', {"a": [1, 2], "b": 3}),  # 多处尾逗号
    ],
)
def test_trailing_commas_tolerated(content, expected):
    """回归：LLM 尾逗号输出可解析（9 个解析失败源根因，2026-08-09）。"""
    client, _ = make_client(lambda request: completion(content))
    assert client.complete(MSG, json_schema={}) == expected


# --- fail ---


def test_5xx_retries_then_raises_with_backoff_sequence():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client, record = make_client(handler)
    with pytest.raises(LLMError):
        client.complete(MSG)
    assert len(calls) == 3
    assert record == [0, 1]  # 退避序列 1s -> 2s（attempt 0/1）


def test_success_after_transient_5xx():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(502, json={"error": {"message": "upstream"}})
        return completion("恢复了")

    client, _ = make_client(handler)
    assert client.complete(MSG) == "恢复了"
    assert len(calls) == 3


def test_401_raises_without_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    client, _ = make_client(handler)
    with pytest.raises(LLMError, match="http error 401"):
        client.complete(MSG)
    assert len(calls) == 1


def test_connection_timeout_retries_then_raises():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("connect timed out")

    client, _ = make_client(handler)
    with pytest.raises(LLMError):
        client.complete(MSG)
    assert len(calls) == 3


def test_invalid_json_raises_llm_json_error():
    client, _ = make_client(lambda request: completion("{broken"))
    with pytest.raises(LLMJsonError):
        client.complete(MSG, json_schema={})


def test_error_types_are_retryable_marked():
    assert LLMError.retryable is True
    assert LLMJsonError.retryable is True
