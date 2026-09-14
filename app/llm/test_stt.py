"""转写客户端与工厂的测试（决策 76：OpenAI 兼容的那一家适配器）。

为什么要有一份**真的 HTTP 客户端**的测试，而不是只测接口：`NoProviderSTT` 与
`FakeSTT` 永远不会失败，所以它们绿着不能说明语音这条路可用。真正会在生产上出的
问题都在边界上 —— 4xx 到底抛不抛、空转写会不会被当成"没作答"、超限的录音拦不拦、
上传的文件名对不对。这些只有对着一个**假的 HTTP 层**才测得出来。
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.deps import get_stt
from app.llm.stt import (
    APISTT,
    MAX_AUDIO_BYTES,
    FakeSTT,
    NoProviderSTT,
    STTError,
    STTUnavailable,
)

AUDIO = b"\x1a\x45\xdf\xa3" + b"x" * 64  # webm 的魔数 + 一点内容


def _client(handler, **kwargs) -> APISTT:
    return APISTT(
        api_key="test-key",
        base_url="https://asr.example/v1",
        model="TeleAI/TeleSpeechASR",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_it_returns_the_transcript() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": "volatile 保证可见性"})

    stt = _client(handler)
    result = stt.transcribe(AUDIO, content_type="audio/webm")
    assert result.text == "volatile 保证可见性"
    assert result.placeholder is False, "真识别结果的 placeholder 必须是 False"
    assert seen["url"] == "https://asr.example/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer test-key"
    assert b'name="model"' in seen["body"]  # type: ignore[operator]
    assert b"TeleAI/TeleSpeechASR" in seen["body"]  # type: ignore[operator]


def test_it_names_the_upload_by_container() -> None:
    """有些实现按扩展名挑解码器 —— 一律叫 `audio.webm` 会在换容器时静默识别失败。"""
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read())
        return httpx.Response(200, json={"text": "好"})

    _client(handler).transcribe(AUDIO, content_type="audio/mpeg; codecs=mp3")
    assert b'filename="answer.mp3"' in seen[0]


def test_an_unknown_container_falls_back_to_webm() -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read())
        return httpx.Response(200, json={"text": "好"})

    _client(handler).transcribe(AUDIO, content_type="")
    assert b'filename="answer.webm"' in seen[0]


# ---------------------------------------------------------------------------
# 失败路径：这一节才是它存在的理由
# ---------------------------------------------------------------------------
def test_an_empty_transcript_is_an_error_not_a_silent_answer() -> None:
    """**空转写不能当成"候选人没说话"往下走。**

    落库时 `answer_text` 有位兜底文案（"（候选人没有作答）"），所以一段空字符串
    会静默变成"他确实没答" —— 而真相是识别失败了。这两种情况对候选人的含义
    完全不同。
    """
    stt = _client(lambda request: httpx.Response(200, json={"text": "   "}))
    with pytest.raises(STTError, match="空文本"):
        stt.transcribe(AUDIO, content_type="audio/webm")


def test_a_4xx_is_not_retried() -> None:
    """4xx 是请求本身的问题（缺 key、模型名错）—— 重试三次只是把同一句话错三遍。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(STTError, match="401"):
        _client(handler).transcribe(AUDIO, content_type="audio/webm")
    assert calls["n"] == 1, "4xx 不该重试"


def test_a_5xx_is_retried_then_reported() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="busy")

    with pytest.raises(STTError, match="重试"):
        _client(handler, max_retries=2).transcribe(AUDIO, content_type="audio/webm")
    assert calls["n"] == 2


def test_an_oversized_recording_is_refused_before_the_request() -> None:
    """8MB 那道闸必须在**发请求之前**拦住 —— 否则它拦的是自己的内存。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"text": "不该走到这里"})

    stt = _client(handler)
    with pytest.raises(STTError, match="录音太大"):
        stt.transcribe(b"x" * (MAX_AUDIO_BYTES + 1), content_type="audio/webm")
    assert calls["n"] == 0


def test_empty_audio_is_refused() -> None:
    with pytest.raises(STTError, match="空的"):
        _client(lambda request: httpx.Response(200, json={"text": "x"})).transcribe(b"")


def test_a_non_json_body_is_reported_as_such() -> None:
    stt = _client(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(STTError, match="不是 JSON"):
        stt.transcribe(AUDIO, content_type="audio/webm")


# ---------------------------------------------------------------------------
# 工厂：配置决定拿到哪一个
# ---------------------------------------------------------------------------
def test_no_config_yet_means_a_loud_failure() -> None:
    stt = get_stt(Settings(stt_provider="none"))
    assert isinstance(stt, NoProviderSTT)
    with pytest.raises(STTUnavailable, match="没有配置 STT 供应商"):
        stt.transcribe(AUDIO)


def test_the_placeholder_still_enforces_the_size_limit() -> None:
    """占位实现也不该让"8MB 上限"这条约束失效 —— 它是端点的性质，不是供应商的。"""
    with pytest.raises(STTError, match="录音太大"):
        FakeSTT().transcribe(b"x" * (MAX_AUDIO_BYTES + 1))


def test_api_provider_needs_a_model_name() -> None:
    """模型名**必须**显式给：转写模型与对话模型不通用，回落成 `deepseek-flash` 只会 404。"""
    with pytest.raises(STTError, match="模型名"):
        get_stt(
            Settings(stt_provider="api", stt_api_key="k", stt_base_url="https://x/v1")
        )


def test_api_provider_falls_back_to_the_llm_credentials() -> None:
    """同一家服务商时少配两个变量 —— 但显式配的优先。"""
    from_llm = get_stt(
        Settings(
            llm_api_key="llm-key",
            llm_base_url="https://llm.example/v1",
            stt_provider="api",
            stt_model="FunAudioLLM/SenseVoiceSmall",
        )
    )
    assert isinstance(from_llm, APISTT)
    assert from_llm._client.headers["authorization"] == "Bearer llm-key"

    explicit = get_stt(
        Settings(
            llm_api_key="llm-key",
            stt_provider="api",
            stt_model="FunAudioLLM/SenseVoiceSmall",
            stt_api_key="stt-key",
            stt_base_url="https://asr.example/v1",
        )
    )
    assert isinstance(explicit, APISTT)
    assert explicit._client.headers["authorization"] == "Bearer stt-key"
