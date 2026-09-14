"""语音转写（STT）—— 接口 + 「没接供应商就明确失败」（ADR-0009 / 决策 32、33）。

决策 32：面试页**可语音回答，默认语音、可随时切回打字**（打字框允许反复修改措辞，
而真实面试考的是临场组织语言）。
决策 33：**不设确认环节**（转写直接进判分）、**不保存原始录音**、保留口语特征。

## 这一版只做接口

真正接哪家 STT 还没定（成本可忽略 —— 基线的结论是"贵的是 TTS，不是 STT"），
所以这里给的是两样东西：

| | |
|---|---|
| `SpeechToText` | 一个方法：`transcribe(audio, *, content_type) -> Transcript` |
| `NoProviderSTT` | **默认实现**：任何调用都抛 `STTUnavailable` |
| `FakeSTT` | `DEEPGRILL_STT_PROVIDER=fake` 时启用：返回**明确标着占位**的转写 |

`NoProviderSTT` 照 `_MissingKeyLLM` 的先例：**绝不返回一段假转写去骗判分**。
判分拿到的是什么，用户就该看到什么 —— 这是"降级可以，静默不行"（AGENTS.md §3.1）
在语音这条链上的落点。占位实现（`FakeSTT`）之所以还留着，是为了让整条链路
（`input_mode='voice'` / `stt_text` / 面试页录音）能在没有供应商时被真的走一遍，
而它的返回值一眼就能看出不是识别结果。

## 不保存原始录音（决策 33）

`transcribe` 收到的是**内存里的 bytes**：没有任何一处把它写进库或磁盘。
这是这条链上唯一的隐私面，也是"不保存"能被测试钉住的原因（库里只有文字）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

#: 一段录音的大小上限。它是**拒绝的边界**，不是"够用就行"的猜测：面试页的录音
#: 是几十秒到两分钟的量级，8MB 已经远超合理值，而它同时是"别让人用这个端点
#: 往内存里灌东西"的那道闸。
MAX_AUDIO_BYTES = 8 * 1024 * 1024


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
