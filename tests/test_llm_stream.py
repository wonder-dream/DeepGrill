"""流式与「两段式回复」的测试（ADR-0004 的 SSE / 决策 32 的面试官发言）。

两个被测对象，各有各的失败方式：

| | 失败了会怎样 |
|---|---|
| `ProseFilter` | 面试官的话**丢字**（增量的边界正好落在分隔行中间），或者把 JSON 结构漏给用户看 |
| `LLMClient.stream` | 重试把同一句话发两遍；或者用量不进账本（决策 14 的第二道安全网少记） |

`ProseFilter` 的重点是**它必须对着任意切法都成立**：模型的一个 chunk 不会照顾
我们的分隔行。所以下面的测试按 1 / 2 / 3 / 5 / 7 字符切一遍 —— 那是穷举级别
的覆盖，而它很便宜。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.llm import (
    PROSE_JSON_MARKER,
    LLMCallError,
    LLMClient,
    ProseFilter,
    split_prose_and_json,
)

PROSE = "先说可见性，再说内存屏障。"
PAYLOAD = {"hits": [{"criterion_id": 1, "status": "命中"}], "should_finish": False}
FULL = f"{PROSE}\n\n{PROSE_JSON_MARKER}\n{json.dumps(PAYLOAD, ensure_ascii=False)}"


# ---------------------------------------------------------------------------
# ProseFilter：只放行分隔行之前的内容
# ---------------------------------------------------------------------------
def _drain(chunks: list[str]) -> str:
    f = ProseFilter()
    out = "".join(f.feed(c) for c in chunks)
    return out + f.finish()


#: ⚠️ 断言用 `strip()` 比，因为**流式显示与落库的那份不是逐字节相同的**：
#: 流式那份可能带着分隔行前面的空行（增量已经在半路放行出去了，收不回来），
#: 而落库那份是 `split_prose_and_json` 的结果 —— 它 `strip()` 过。
#: 差别只有首尾空白：用户看不出，而"要不要为了几个空白去缓存整段"不值得。
@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 10, 64])
def test_prose_filter_survives_any_chunking(size: int) -> None:
    """**任何切法**都要得到同一句话，且除了首尾空白一个字符都不多。"""
    chunks = [FULL[i : i + size] for i in range(0, len(FULL), size)]
    assert _drain(chunks).strip() == PROSE


def test_prose_filter_never_emits_the_marker_or_json() -> None:
    for size in (1, 3, 7, 11):
        chunks = [FULL[i : i + size] for i in range(0, len(FULL), size)]
        out = _drain(chunks)
        assert PROSE_JSON_MARKER not in out
        assert "criterion_id" not in out


def test_prose_filter_handles_one_shot() -> None:
    assert _drain([FULL]).strip() == PROSE


def test_prose_filter_handles_a_marker_only_reply() -> None:
    """模型只给了分隔行（没有正文、也没写完 JSON）：不崩，散文为空。"""
    assert _drain([f"{PROSE_JSON_MARKER}\n{{}}"]) == ""


def test_prose_filter_ignores_the_marker_inside_a_sentence() -> None:
    """**分隔行必须独占一行** —— 不然候选人的答案里出现这几个字就会截断发言。"""
    text = f"他说了 {PROSE_JSON_MARKER} 这几个字符，但那是题目里的。\n{PROSE_JSON_MARKER}\n{{}}"
    out = _drain([text])
    assert PROSE_JSON_MARKER in out, "句中的那几个字属于正文"
    assert out.strip() == f"他说了 {PROSE_JSON_MARKER} 这几个字符，但那是题目里的。"


def test_prose_filter_marks_done_after_the_marker() -> None:
    f = ProseFilter()
    f.feed(FULL)
    assert f.done is True
    assert f.feed("后面的东西再也不放行") == ""
    assert f.finish() == ""


# ---------------------------------------------------------------------------
# split_prose_and_json：同步路径用同一个边界
# ---------------------------------------------------------------------------
def test_split_returns_prose_and_payload() -> None:
    prose, data = split_prose_and_json(FULL)
    assert prose == PROSE
    assert data == PAYLOAD


def test_split_tolerates_a_fence_around_the_json() -> None:
    text = f"{PROSE}\n{PROSE_JSON_MARKER}\n```json\n{json.dumps(PAYLOAD)}\n```"
    assert split_prose_and_json(text)[1] == PAYLOAD


def test_split_without_the_marker_is_a_loud_failure() -> None:
    """**不猜**：没有分隔行 → 抛解析错误，交给调用方走"这一轮判定没成功"的降级。"""
    from app.llm import LLMParseError

    with pytest.raises(LLMParseError) as e:
        split_prose_and_json("我就随便说一句。")
    assert PROSE_JSON_MARKER in str(e.value)


def test_split_and_filter_agree_on_the_same_boundary() -> None:
    """两条路（同步落库、流式显示）必须切在**同一处** —— 否则同一份输出会得到
    两种结果（用户看到的与报告里存的不是同一句话）。差别只允许在首尾空白上。"""
    prose, _ = split_prose_and_json(FULL)
    assert _drain([FULL]).strip() == prose


# ---------------------------------------------------------------------------
# LLMClient.stream
# ---------------------------------------------------------------------------
def _sse_body(chunks: list[str], *, usage: dict | None = None) -> str:
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    if usage is not None:
        lines.append("data: " + json.dumps({"choices": [], "usage": usage}))
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def _stream_client(handler, **kw) -> LLMClient:
    return LLMClient(
        api_key="k",
        base_url="https://example.invalid/v1",
        model="m",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def test_stream_yields_content_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse_body(["那", "内存", "屏障呢？"]))

    client = _stream_client(handler)
    assert list(client.stream([{"role": "user", "content": "x"}])) == ["那", "内存", "屏障呢？"]


def test_stream_hides_reasoning_and_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """推理内容**不进正文**（基线：思考过程不展示），但用量照样进账本。"""
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "我想想"}}]}) + "\n\n"
            "data: " + json.dumps({"choices": [{"delta": {"content": "可见性"}}]}) + "\n\n"
            "data: " + json.dumps({
                "choices": [],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22,
                          "completion_tokens_details": {"reasoning_tokens": 5}},
            }) + "\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body)

    client = _stream_client(handler)
    out = list(client.stream([{"role": "user", "content": "x"}]))
    assert out == ["可见性"]
    assert "我想想" not in "".join(out)
    assert client.usage_total["prompt_tokens"] == 11
    assert client.usage_total["completion_tokens"] == 22
    assert client.usage_total["reasoning_tokens"] == 5
    assert client.usage_total["calls"] == 1


def test_stream_without_usage_is_counted_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端没给 usage 时**记一笔**（`missing_usage_calls`）—— 账本少记不能是静默的。"""
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse_body(["a"]))

    client = _stream_client(handler)
    list(client.stream([{"role": "user", "content": "x"}]))
    assert client.usage_total["missing_usage_calls"] == 1


def test_stream_retries_before_any_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """**一个字节都没发出去之前**可以重试（连接失败是常见的）。"""
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text=_sse_body(["好了"]))

    client = _stream_client(handler)
    assert list(client.stream([{"role": "user", "content": "x"}])) == ["好了"]
    assert calls["n"] == 2


def test_stream_does_not_retry_after_output_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """已经放行过内容之后**不许重试** —— 否则同一句话会出现两遍。

    构造方式：让"响应体"在读第二块时炸（httpx 的 `iter_lines` 中途抛错）。
    """
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)

    class Exploding(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"\\u7b2c\\u4e00\\u53e5"}}]}\n\n'
            raise httpx.ReadError("断了")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Exploding())

    client = _stream_client(handler)
    seen: list[str] = []
    with pytest.raises(LLMCallError):
        for chunk in client.stream([{"role": "user", "content": "x"}]):
            seen.append(chunk)
    assert seen == ["第一句"], "拿到的那一段不能被重试冲掉"


def test_stream_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    client = _stream_client(handler)
    with pytest.raises(LLMCallError):
        list(client.stream([{"role": "user", "content": "x"}]))
    assert calls["n"] == 1, "4xx 是请求本身的问题（行为规格 §1.3）"


def test_stream_sends_include_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求体里必须带 `stream_options.include_usage` —— 没有它用量永远进不了账本。"""
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=_sse_body(["a"]))

    client = _stream_client(handler)
    list(client.stream([{"role": "user", "content": "x"}]))
    assert seen["stream"] is True
    assert seen["stream_options"] == {"include_usage": True}
    assert "response_format" not in seen, (
        "两段式回复不是纯 JSON —— 不能要求 json_object（那是整段必须 JSON）"
    )


def test_reasoning_is_kept_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """推理文本**必须有界**（AGENTS.md §3.2：进内存的东西要有回收者）。

    原来只 `append` 从不回收：一个请求里几次流式调用就能攒出几十 KB 的进程内垃圾
    （实测一轮 1037 个 completion token 里 934 是推理）。这里灌 5 倍上限的推理，
    断言留下的不超过上限、而**见过的总量照实记**（回收不等于没发生过）。
    """
    from app.llm import REASONING_KEEP_CHARS

    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)
    chunk = "推理" * 50  # 100 字
    rounds = (REASONING_KEEP_CHARS // len(chunk)) * 5

    def handler(request: httpx.Request) -> httpx.Response:
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": chunk}}]})
            for _ in range(rounds)
        ]
        lines.append("data: [DONE]")
        return httpx.Response(200, text="\n\n".join(lines) + "\n\n")

    client = _stream_client(handler)
    assert list(client.stream([{"role": "user", "content": "x"}])) == []
    kept = sum(len(c) for c in client._reasoning_buffer)
    assert kept <= REASONING_KEEP_CHARS + len(chunk), f"缓冲区没回收，留着 {kept} 字"
    assert client.reasoning_chars == len(chunk) * rounds, "见过的总量要照实记（回收 ≠ 没发生）"
