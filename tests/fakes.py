"""测试替身。**住 tests/ 而不是 app/**（ADR-0010）。

判据是"它有几种失效方式"：替身住 `app/` 时，它多出一条"必须与真 client 同步"
的义务，失效方式是"真实调用路径与 fake 路径静默分叉"（只有线上才暴露）；
住 `tests/` 时，失效方式是**所有依赖它的测试一起变红** —— 响亮。

`FakeLLM` 的接口与 `app.llm.LLMClient` 一致，因为它要被领域测试当真的用：
`chat()` / `chat_json()` / `close()`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.llm import LLMError, LLMReply, loads_tolerant

#: 按 prompt 正文里的一句特征分辨"这次调用是在干什么"。
#: 它跟着 prompt 文件走 —— 改 prompt 时这两句特征必须还在（否则按种类分派失效，
#: 而且会以"队列空了"的形式响亮失败，不会静默给错数据）。
_KIND_MARKERS = (
    ("round", "逐条判定命中情况"),
    ("eval", "四维分"),
    ("summary", "综合成一段人话"),
)


def _kind_of(messages: list[dict[str, str]]) -> str:
    text = " ".join(m.get("content", "") for m in messages)
    for kind, marker in _KIND_MARKERS:
        if marker in text:
            return kind
    return "unknown"


@dataclass
class FakeReply:
    """一条预录响应。

    两种形态：给 `text`（原样返回，可用来测解析失败），或给 `data`（自动
    `json.dumps`，用来喂结构化输出）。
    """

    text: str | None = None
    data: Any = None
    model: str = "fake-model"
    prompt_tokens: int = 10
    completion_tokens: int = 20
    error: Exception | None = None

    def resolve(self, *, json_mode: bool) -> LLMReply:
        if self.error is not None:
            raise self.error
        import json

        if self.data is not None:
            text = json.dumps(self.data, ensure_ascii=False)
        elif self.text is not None:
            text = self.text
        else:
            raise AssertionError("FakeReply 必须给 text / data / error 之一")
        return LLMReply(
            text=text,
            data=loads_tolerant(text) if json_mode else None,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


@dataclass
class FakeLLM:
    """预录响应队列。

    调用按顺序消费；**队列空了就抛 `LLMError`**，而不是返回空字符串 ——
    "测试没准备这一步"必须响亮，而空回复会让失败发生在很远的地方。
    """

    replies: list[FakeReply] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = "fake-model"
    #: 按"调用种类"分派：`{"round": [reply, ...]}`。见 `on()`。
    by_kind: dict[str, list[FakeReply]] = field(default_factory=dict)

    def queue(self, *replies: FakeReply | Any) -> FakeLLM:
        for r in replies:
            self.replies.append(r if isinstance(r, FakeReply) else FakeReply(data=r))
        return self

    def on(self, kind: str, *replies: FakeReply | Any) -> FakeLLM:
        """按**调用种类**排队，而不是按调用顺序。

        为什么需要它：一场多题面试里，LLM 的调用顺序取决于**代码的收尾判据**
        （一道题可能问 1 轮也可能问 3 轮）。用"顺序队列"写测试，就等于在测试里
        重写一遍那个判据 —— 改一次判据要改一堆测试，而且失败信息完全指不出真因
        （实测为了一次 `200 != 302` 排查了四轮）。

        种类由 prompt 正文里的一句特征判断（见 `_kind_of`），所以它跟着 prompt
        文件走，不跟着调用顺序走。
        """
        bucket = self.by_kind.setdefault(kind, [])
        for r in replies:
            bucket.append(r if isinstance(r, FakeReply) else FakeReply(data=r))
        return self

    def queue_text(self, *texts: str) -> FakeLLM:
        return self.queue(*[FakeReply(text=t) for t in texts])

    def chat(
        self,
        messages: list[dict[str, str]] | tuple[dict[str, str], ...],
        *,
        json_mode: bool = False,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> LLMReply:
        self.calls.append(
            {
                "messages": list(messages),
                "json_mode": json_mode,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model": model or self.model,
            }
        )
        bucket = self.by_kind.get(_kind_of(messages))
        if bucket:
            return bucket.pop(0).resolve(json_mode=json_mode)
        if not self.replies:
            raise LLMError(
                f"FakeLLM 的响应队列空了（第 {len(self.calls)} 次调用）—— "
                f"测试没准备这一步"
            )
        return self.replies.pop(0).resolve(json_mode=json_mode)

    def chat_json(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> tuple[Any, LLMReply]:
        reply = self.chat(messages, json_mode=True, **kwargs)
        return reply.data, reply

    def close(self) -> None:
        pass

    # -- 断言辅助 ----------------------------------------------------------
    def last_prompt(self) -> str:
        """最后一次调用里 user 消息的正文 —— 用来断言 prompt 渲染结果。"""
        if not self.calls:
            raise AssertionError("还没有任何调用")
        for m in reversed(self.calls[-1]["messages"]):
            if m.get("role") == "user":
                return m.get("content", "")
        raise AssertionError("最后一次调用里没有 user 消息")
