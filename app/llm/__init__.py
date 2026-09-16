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
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: 退避基数：第 N 次失败后等 `2**attempt` 秒（1s → 2s）。
_BACKOFF_BASE = 1.0

#: 保留的推理文本上限（字符）。推理不展示给用户，只用于排查，所以留最近一段就够；
#: 关键是它**有界**（§3.2）。实测一轮 `completion_tokens=1037` 里 934 是推理 ——
#: 一次请求若连着几次流式调用，不设上限就是几十 KB 的进程内垃圾。
REASONING_KEEP_CHARS = 8000


class LLMError(Exception):
    """LLM 层的失败基类。调用方**必须**区分下面两个子类。"""


class LLMCallError(LLMError):
    """连不上 / 超时 / 5xx（已重试到上限）。降级策略通常是"重试或换路"。"""


class LLMParseError(LLMError):
    """调用成功但拿不到可解析的 JSON。降级策略通常是"丢掉这次输出"。

    与 `LLMCallError` 分开的理由：调用失败可以原样重试，而"模型答了但答的不像
    JSON"重试同一个 prompt 往往还是不像 —— 两者的补救不同（行为规格 §1.8）。

    `raw` 是**模型实际说了什么**。§1.8 否决的是"原样重发同一个 prompt"，而
    **把这段坏输出贴回去、要求它只输出 json** 是另一回事 —— 那需要这段原文，
    所以它随异常一起带出来（`chat_json` 用它做一次修复重发）。
    """

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


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
# 「先说人话，再给 JSON」的两段式回复（面试官那一轮用它做流式）
# ---------------------------------------------------------------------------
#: 两段之间的分隔行。要求模型**原样**输出它 —— 它同时是两件事的判据：
#: 「哪些字节可以立刻流给用户」与「JSON 从哪儿开始」。
#:
#: 为什么不用"把 `followup` 放进 JSON 再流式解析半截 JSON"：那要求前端懂一半 JSON，
#: 而模型少打一个引号就会把结构漏给用户。一句话在前、结构在后，边界是一个固定的
#: 字符串 —— 错了就整轮降级，不会"漏一半"。
PROSE_JSON_MARKER = "===JSON==="

#: 分隔行必须**独占一行**（允许前后空白）。正文里恰好写出这几个字是可能的
#: （比如候选人的答案里），而"独占一行"这个约束让误判几乎不可能。
#: 这一个正则同时被同步路径（`split_prose_and_json`）与流式路径（`ProseFilter`）
#: 用 —— 两个地方各写一套"什么算分隔行"，就是两套会漂的规则。
_MARKER_RE = re.compile(
    r"(?:^|\n)[ \t]*" + re.escape(PROSE_JSON_MARKER) + r"[ \t]*(?:\n|$)"
)
#: 流式放行时要按住的尾巴长度：分隔行本身 + 可能的前导换行与空格。
_MARKER_KEEP = len(PROSE_JSON_MARKER) + 4


def split_prose_and_json(text: str) -> tuple[str, Any]:
    """把两段式回复拆成 `(要说的话, 判定结果)`。

    `text` 是模型的完整输出。分隔行缺失时抛 `LLMParseError` —— **不猜**：
    把整段当成人话、判定全记"未涉及"是一种静默降级（用户看到的下一问是真的，
    而命中判定是假的）。交给调用方既有的失败路径（页面明说"这一轮判定没成功"）。
    """
    m = _MARKER_RE.search(text)
    if m is None:
        raise LLMParseError(
            f"模型没有输出分隔行 {PROSE_JSON_MARKER}（两段式回复的契约）—— 无法把"
            f"「要说的话」与「判定结果」分开"
        )
    return text[: m.start()].strip(), loads_tolerant(text[m.end():])


class ProseFilter:
    """从流式输出里**只放行分隔行之前的内容**，并把它切成可显示的增量。

    它是"边收边显示"与"结构不能泄给用户"之间那一步。三个细节：

    ① **按住尾巴**：分隔行可能被切成几块（`===` / `JSON` / `===`），所以每次都把
       末尾 `_MARKER_KEEP` 个字符留在缓冲里 —— 那一段里不可能藏着一个**完整**的
       分隔行，放行是安全的。
    ② **一旦见到分隔行就永久闭嘴**：后面是 JSON，一个字节都不再放行。
    ③ `finish()` 在流结束时把按住的那一小段吐出来（否则最后几个字会丢）。
    """

    def __init__(self, marker: str = PROSE_JSON_MARKER) -> None:
        self.marker = marker
        self._buffer = ""
        self._done = False

    @property
    def done(self) -> bool:
        """已经见过分隔行（后面的内容都属于 JSON）。"""
        return self._done

    def feed(self, chunk: str) -> str:
        """喂一段增量，返回**这一段里可以显示的部分**（可能为空）。"""
        if self._done:
            return ""
        self._buffer += chunk
        m = _MARKER_RE.search(self._buffer)
        if m is not None:
            out = self._buffer[: m.start()]
            self._done = True
            self._buffer = ""
            return out
        if len(self._buffer) <= _MARKER_KEEP:
            return ""
        out, self._buffer = self._buffer[:-_MARKER_KEEP], self._buffer[-_MARKER_KEEP:]
        return out

    def finish(self) -> str:
        """流结束：把按住的那一小段放出来（除非已经见过分隔行）。

        ⚠️ **结尾那半截分隔行要丢掉**：`feed()` 每次按住末尾 `_MARKER_KEEP` 个字符
        防的是"分隔行被切成几块"，而流**被截断**时（网络断、`max_tokens` 到顶）那一段
        的结尾正好是分隔行的一个前缀 —— 放出去用户就会在面试官的话里看到 `===JS`
        （实测 probe18 的 `cut_stream`）。

        判据是"**结尾**是分隔行的前缀"，而不是"整段等于前缀"：按住的尾巴里通常前半
        是正文（`那我们说说内存屏障。\n\n===JS`）—— 只该砍掉最后那几个字符。
        也不能写成"以 `=` 结尾就丢"：正文里的 `=` 是正常的，只有"正好构成前缀"才可疑。
        """
        if self._done:
            self._buffer = ""
            return ""
        out, self._buffer = self._buffer, ""
        for n in range(min(len(out), len(self.marker) - 1), 0, -1):
            if self.marker.startswith(out[-n:]):
                return out[:-n]
        return out


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
            #: 没拿到 `usage` 的流式调用次数（服务端没给 include_usage 时）。
            #: **不静默**：账本少记了多少次是能看出来的（决策 14 的第二道安全网）。
            "missing_usage_calls": 0,
        }
        #: 流式调用里累计的推理文本。**不展示给用户**（基线：思考过程不展示），
        #: 但服务端留着用于排查 —— 与 `LLMReply.reasoning_text` 同一个用途。
        #: ⚠️ **只保留最近 `REASONING_KEEP_CHARS` 个字符**（见 `_keep_reasoning`）：
        #: 原来只 append、从不回收，一个请求里几次流式调用就能攒出几十 KB 的
        #: 进程内垃圾，而 §3.2 要求"进内存的东西必须有回收者"。
        self._reasoning_buffer: list[str] = []
        self._reasoning_kept = 0
        #: 见过的推理文本总字符数（含已经被回收的）—— 回收不等于没发生过。
        self.reasoning_chars = 0
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def _keep_reasoning(self, chunk: str) -> None:
        """留下这段推理文本，并保证缓冲区**有界**（AGENTS.md §3.2）。

        `reasoning_chars` 记的是**见过多少**（含已回收的），所以"回收"不会把
        事实改掉：要判断一次调用想得多少，读它而不是读缓冲区长度。
        """
        self.reasoning_chars += len(chunk)
        self._reasoning_buffer.append(chunk)
        self._reasoning_kept += len(chunk)
        while self._reasoning_kept > REASONING_KEEP_CHARS and len(self._reasoning_buffer) > 1:
            self._reasoning_kept -= len(self._reasoning_buffer.pop(0))

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
        # ⚠️ **默认 16384，不是 8192**：`deepseek-flash` 是推理模型，**思考也吃 max_tokens**，
        # 实测在三条离线路上撞过同一面墙（提候选 40 道题、逐簇归并、挂载 20 道题 + 131 个点
        # 的目录），症状统一是 reasoning_tokens 吃满、content 为空。8192 是在"输出不会太长"
        # 的假设下定的，而这个假设对推理模型不成立（API 实测接受 16384 与 32768）。
        max_tokens: int = 16384,
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
        elif not reply.text.strip():
            # **纯文本调用也会有这个失败**：这个模型先推理，而推理吃 `max_tokens` ——
            # 预算给少了就是"想完了没地方写"（content 空、reasoning_tokens 不为 0）。
            # 实测踩过：讲解生成写 `max_tokens=2048`，每次都拿到空内容，而症状看起来
            # 像"模型不肯说话"。所以这里与 json 那条路用同一句话说清原因。
            if reply.reasoning_tokens:
                raise LLMCallError(
                    f"模型把 {reply.reasoning_tokens} 个 token 全用在推理上、没有产出内容"
                    f"（finish_reason={reply.finish_reason or '未知'}）—— 需要调大 max_tokens"
                )
            raise LLMCallError(
                f"模型返回了空内容（finish_reason={reply.finish_reason or '未知'}）"
            )
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
        except LLMParseError as e:
            if reply.finish_reason == "length":
                raise LLMCallError(
                    f"输出被 max_tokens 截断、JSON 不完整（completion_tokens="
                    f"{reply.completion_tokens}）—— 这不是模型答错，是预算不够"
                ) from None
            # 带上模型**实际说了什么**：`chat_json` 的修复重发要用它。不带的话
            # 就只能原样重发同一个 prompt —— 而那正是 §1.8 否决的那种补救。
            raise LLMParseError(str(e), raw=reply.text) from None

    def chat_json(self, messages: Sequence[dict[str, str]], **kwargs: Any) -> tuple[Any, LLMReply]:
        """`chat(json_mode=True)` 的便利形式，返回 `(data, reply)`。

        ## 解析失败时做**一次修复重发**

        真跑踩到过：`json_mode=True` 也拦不住模型偶尔吐出不合法 JSON（实测在
        「简历 → 私有题集」那条链上，同一个输入第一次坏、第二次好）。而这条链的
        用户是**等在页面上的**，一次抖动就变成"出题没成功，稍后重试"。

        三种补救里选了第二种（行为规格 §1.8 只否决了第一种）：

        ① ~~原样重发同一个 prompt~~ —— §1.8 已否决：答得不像 JSON 的，重发往往
           还是不像，只是多花一次钱
        ② **把那段坏输出贴回去，要求它只输出 json**（换的是 prompt，不是重发）
        ③ 降级（上面那两条都失败时才走）

        `max_tokens` 截断**不走这条路**（那时 `finish_reason == "length"`，抛的是
        `LLMCallError`）：预算不够是调用方该调大的事，重发只会再截断一次。
        """
        try:
            reply = self.chat(messages, json_mode=True, **kwargs)
        except LLMParseError as first:
            if not first.raw:
                raise
            from app.llm.prompts import load

            instruction = load("llm/json_repair.md").render(error=str(first))
            repaired = [
                *messages,
                {"role": "assistant", "content": first.raw},
                {"role": "user", "content": instruction},
            ]
            logger.warning("JSON 解析失败，做一次修复重发：%s", first)
            reply = self.chat(repaired, json_mode=True, **kwargs)
        return reply.data, reply

    # -- 流式 --------------------------------------------------------------
    def stream(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> Iterator[str]:
        """流式调用，**逐段 yield 正文增量**（不含推理）。

        三条与 `chat()` 不同、必须说清楚的地方：

        ① **不能用 `response_format=json_object`**（那是"整段必须是 JSON"的要求）。
           面试官那一轮改成两段式（先说人话、分隔行、再给 json），所以走这条路的
           prompt 必须自己保证 json 部分可解析 —— `split_prose_and_json` 负责拆。

        ② **重试只在"一个字节都没放行"之前**。一旦 yield 过，消费者已经拿到了
           内容，重试会让同一句话出现两遍。所以 `_emitted` 之后遇到错误直接抛。

        ③ **用量靠 `stream_options.include_usage`**（服务端会在最后一个 chunk 里
           带上 `usage`）。拿不到就没有用量 —— 决策 14 的账本会少记这一次，而这
           **不静默**：`usage_total["calls"]` 照加，`missing_usage_calls` 记一笔。
        """
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            # 没有它，流式调用的 token 用量就不进账本（决策 14 的第二道安全网）
            "stream_options": {"include_usage": True},
        }
        last: Exception | None = None
        for attempt in range(self._max_retries):
            if attempt:
                time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
            emitted = False
            try:
                with self._client.stream("POST", "/chat/completions", json=payload) as r:
                    if r.status_code >= 500:
                        last = LLMCallError(f"服务端错误 {r.status_code}")
                        logger.warning("流式调用返回 %d（第 %d 次）", r.status_code, attempt + 1)
                        continue
                    if not (200 <= r.status_code < 300):
                        raise LLMCallError(f"请求被拒绝（{r.status_code}）：{r.text[:200]}")
                    for chunk in self._iter_stream(r):
                        emitted = True
                        yield chunk
                return
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if emitted:
                    # 已经放行过内容：重试会让同一句话出现两遍
                    raise LLMCallError(f"流式连接在输出中途断开：{e}") from e
                last = e
                logger.warning("流式连接失败（第 %d 次）：%s", attempt + 1, e)
                continue
            except httpx.HTTPError as e:
                if emitted:
                    raise LLMCallError(f"流式输出中断：{e}") from e
                last = e
                continue
        raise LLMCallError(f"重试 {self._max_retries} 次仍失败：{last}")

    def _iter_stream(self, response: httpx.Response) -> Iterator[str]:
        """把 SSE 的 `data:` 行拆成正文增量，并把用量记进 `usage_total`。

        OpenAI 兼容的形态：每行 `data: {...}`，结束是 `data: [DONE]`；增量在
        `choices[0].delta.content`，而**推理内容在 `delta.reasoning_content`** ——
        它不进正文（基线：思考过程不展示），但服务端要留着用于排查，所以照样累计。
        """
        usage_seen = False
        for line in response.iter_lines():
            # httpx 的 `iter_lines()` 给的是 **str**（它自己按响应编码解过），
            # 所以这里不需要再判 bytes —— 那一段永远走不到，而 mypy 会正确地
            # 把它标成 unreachable（一条"看起来在防御"的死代码）。
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                body = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("流式返回里有解析不了的一行：%r", data[:200])
                continue

            usage = body.get("usage") or {}
            if usage:
                usage_seen = True
                details = usage.get("completion_tokens_details") or {}
                self.usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                self.usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
                self.usage_total["reasoning_tokens"] += int(details.get("reasoning_tokens") or 0)

            choices = body.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if reasoning:
                self._keep_reasoning(reasoning)
            content = delta.get("content")
            if content:
                yield content

        self.usage_total["calls"] += 1
        if not usage_seen:
            # 不静默：账本少记一次是**能被发现**的一件事（观测页看得到这个计数）
            self.usage_total["missing_usage_calls"] += 1

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
