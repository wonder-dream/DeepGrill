"""语音转写（STT）—— 接口 + 「没接供应商就明确失败」（ADR-0009 / 决策 32、33）。

决策 32：面试页**可语音回答，默认语音、可随时切回打字**（打字框允许反复修改措辞，
而真实面试考的是临场组织语言）。
决策 33：**不设确认环节**（转写直接进判分）、**不保存原始录音**、保留口语特征。

## 这一版：接口 + 三种实现

| | |
|---|---|
| `SpeechToText` | 一个方法：`transcribe(audio, *, content_type) -> Transcript` |
| `NoProviderSTT` | **默认实现**：任何调用都抛 `STTUnavailable` |
| `FakeSTT` | `DEEPGRILL_STT_PROVIDER=fake` 时启用：返回**明确标着占位**的转写 |
| `APISTT` | `DEEPGRILL_STT_PROVIDER=api` 时启用：**OpenAI 兼容的 `/audio/transcriptions`** |

`NoProviderSTT` 照 `_MissingKeyLLM` 的先例：**绝不返回一段假转写去骗判分**。
判分拿到的是什么，用户就该看到什么 —— 这是"降级可以，静默不行"（AGENTS.md §3.1）
在语音这条链上的落点。占位实现（`FakeSTT`）之所以还留着，是为了让整条链路
（`input_mode='voice'` / `stt_text` / 面试页录音）能在没有供应商时被真的走一遍，
而它的返回值一眼就能看出不是识别结果。

## 真实供应商为什么是"一家适配器"而不是"一家实现"

`/audio/transcriptions`（multipart：`file` + `model`）是 OpenAI 定下的形状，而国内几家
（硅基流动的 `FunAudioLLM/SenseVoiceSmall`、`TeleAI/TeleSpeechASR` 等）也照它实现 ——
见 [SiliconFlow 的接口文档](https://docs.siliconflow.com/cn/api-reference/audio/create-audio-transcriptions)。
所以换供应商是**改三个配置值**（base_url / model / key），不是改代码；写死某一家的
请求体形状会把"选哪家"这个部署决定固化进代码里。

## 不保存原始录音（决策 33）

`transcribe` 收到的是**内存里的 bytes**：没有任何一处把它写进库或磁盘。
这是这条链上唯一的隐私面，也是"不保存"能被测试钉住的原因（库里只有文字）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx

#: 一段录音的大小上限。它是**拒绝的边界**，不是"够用就行"的猜测：面试页的录音
#: 是几十秒到两分钟的量级，8MB 已经远超合理值，而它同时是"别让人用这个端点
#: 往内存里灌东西"的那道闸。
#:
#: ⚠️ **它同时是反代的配置依据**：nginx 默认 `client_max_body_size 1m`，不改就会在
#: 生产上把超过 1MB 的语音回答挡成 413 —— 而开发机上用的是 uvicorn，看不到这层。
MAX_AUDIO_BYTES = 8 * 1024 * 1024

#: 退避基数（秒）。与 `APIEmbeddings` 同一组实测结论。
_BACKOFF_BASE = 1.0

#: 容器扩展名 → 上传时的文件名。有些实现会按扩展名挑解码器，所以不能一律叫 audio.webm。
_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}


class STTError(Exception):
    """转写层的失败基类。"""


class STTUnavailable(STTError):
    """**没有接供应商** —— 与"识别失败"不是一回事。

    分开的理由与 `LLMCallError` / `LLMParseError` 相同：前者的处置是"叫人去配"，
    后者的处置是"让用户重说一遍"。折成一种错误，页面只能说一句含糊的话。
    """


@dataclass(frozen=True)
class Transcript:
    """一次转写的结果。

    `placeholder=True` 表示这是**占位实现**产出的文字，不是真的识别结果 ——
    页面必须把它显示出来（面试页的那句提示），否则一次占位转写会静默地变成
    "候选人的回答"。
    """

    text: str
    placeholder: bool = False


class SpeechToText(Protocol):
    """转写接口。**同步**，与 `LLMClient` 同理（ADR-0005 的基础设施层）。"""

    def transcribe(self, audio: bytes, *, content_type: str = "") -> Transcript: ...


class NoProviderSTT:
    """默认实现：没有配置供应商。**任何调用都明确失败。**"""

    def transcribe(self, audio: bytes, *, content_type: str = "") -> Transcript:
        del audio, content_type
        raise STTUnavailable(
            "语音转写还没接入（没有配置 STT 供应商）—— 这一轮请用打字作答"
        )


#: 占位转写的内容。它必须**一眼看出不是识别结果** —— 这段文字会被真的存进
#: `attempts.answer_text` 并送进判分，所以措辞不能像一句人话。
PLACEHOLDER_TRANSCRIPT = "（占位转写：fake STT 没有真的识别这段录音）"


class FakeSTT:
    """占位实现（`DEEPGRILL_STT_PROVIDER=fake`）。返回一段标着占位的文字。

    它存在的唯一理由：让"录音 → 转写 → 判分 → 落库"这条链在没有供应商时也能被
    开发和测试真的走一遍。它**不假装**自己识别出了什么。
    """

    def transcribe(self, audio: bytes, *, content_type: str = "") -> Transcript:
        del content_type
        # 大小仍然算一遍：占位实现也不该让"8MB 上限"这条约束失效
        if len(audio) > MAX_AUDIO_BYTES:
            raise STTError(f"录音太大（{len(audio)} 字节，上限 {MAX_AUDIO_BYTES}）")
        return Transcript(text=PLACEHOLDER_TRANSCRIPT, placeholder=True)


class APISTT:
    """OpenAI 兼容的 `/audio/transcriptions`（`DEEPGRILL_STT_PROVIDER=api`）。

    三条与 `LLMClient` / `APIEmbeddings` 相同的纪律（同一组实测结论）：

    ① **屏蔽 SDK 重试、只用本层重试**：3 次、退避 1s → 2s
    ② **只对 5xx / 连接错误 / 超时重试，4xx 立即抛** —— 4xx 是请求本身的问题
       （缺 key、模型名错、格式不支持），重试三次只是把同一句错误说三遍
    ③ **识别失败就是失败**：它**不返回空转写**。空字符串会静默地变成"候选人没作答"
       （`attempts.answer_text` 的兜底文案），那比报错更糟 —— 用户会以为自己的话被听进去了

    `transcribe` 是同步的（与 `LLMClient` 同理），**原始录音只活在这次调用的内存里**。
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
            raise STTError("缺少转写 API key（设置 DEEPGRILL_STT_API_KEY）")
        if not model:
            raise STTError("缺少转写模型名（设置 DEEPGRILL_STT_MODEL，如 FunAudioLLM/SenseVoiceSmall）")
        self.model = model
        self._max_retries = max_retries
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def transcribe(self, audio: bytes, *, content_type: str = "") -> Transcript:
        if not audio:
            raise STTError("录音是空的")
        if len(audio) > MAX_AUDIO_BYTES:
            raise STTError(f"录音太大（{len(audio)} 字节，上限 {MAX_AUDIO_BYTES}）")
        body = self._post_with_retry(audio, content_type=content_type)
        text = str(body.get("text") or "").strip()
        if not text:
            # 空转写不能当成"候选人没说话"往下走 —— 那会让一次失败静默成一次作答
            raise STTError("转写返回了空文本")
        return Transcript(text=text)

    def _post_with_retry(self, audio: bytes, *, content_type: str) -> dict:
        subtype = (content_type or "").split(";")[0].strip().lower()
        suffix = _EXTENSIONS.get(subtype, "webm")
        files = {"file": (f"answer.{suffix}", audio, subtype or "audio/webm")}
        data = {"model": self.model}
        last: Exception | None = None
        for attempt in range(self._max_retries):
            if attempt:
                time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
            try:
                response = self._client.post(
                    "/audio/transcriptions", files=files, data=data
                )
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last = e
                continue
            if 200 <= response.status_code < 300:
                try:
                    parsed = response.json()
                except ValueError as e:  # 200 但返回体不是 JSON：也说清是哪一步
                    raise STTError(f"转写返回体不是 JSON：{response.text[:200]}") from e
                if not isinstance(parsed, dict):
                    raise STTError(f"转写返回体形状不符合预期：{parsed!r:.200}")
                return parsed
            if 500 <= response.status_code < 600:
                last = STTError(f"服务端错误 {response.status_code}")
                continue
            raise STTError(
                f"转写请求被拒绝（{response.status_code}）：{response.text[:200]}"
            )
        raise STTError(f"重试 {self._max_retries} 次仍失败：{last}")
