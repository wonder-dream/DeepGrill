"""LLM 层的测试：重试纪律、JSON 容错、prompt 加载。

这些断言**全部来自 `docs/v1行为规格.md` §1**（那一组契约标为「继承」）——
v1 用真实调试换来的结论，v2 不重测，但要**有测试守着**，否则"继承"只是口号。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.llm import (
    LLMCallError,
    LLMClient,
    LLMParseError,
    loads_tolerant,
    strip_code_fence,
    strip_trailing_commas,
)
from app.llm import prompts as prompts_mod

#: 被拦下来的退避时长（由 `_no_real_backoff` 清空并填充）。
_SLEPT: list[float] = []


# ---------------------------------------------------------------------------
# 重试纪律（§1.2 / §1.3）
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_real_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """把退避的 sleep 换成记账（见 `_SLEPT`）。

    退避是**行为契约**（1s → 2s），不该因为"测试太慢"被删掉；也不该让测试真的
    睡 3 秒。所以拦下来并断言它被调用过 —— 这样"重试间隔"仍然是被测的性质。
    """
    _SLEPT.clear()
    monkeypatch.setattr("app.llm.time.sleep", _SLEPT.append)


def _client(handler, **kw) -> LLMClient:
    return LLMClient(
        api_key="k",
        base_url="https://example.invalid/v1",
        model="m",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def _ok_body(content: str = "你好") -> dict:
    return {
        "model": "m",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 9},
    }


def test_retries_server_errors_then_succeeds() -> None:
    """5xx 要重试。退避被 monkeypatch 掉，否则一条测试要等 3 秒。"""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json=_ok_body("成了"))

    with _client(handler) as c:
        reply = c.chat([{"role": "user", "content": "hi"}])
    assert reply.text == "成了"
    assert len(seen) == 3, "应当重试到第 3 次才成功"
    # 退避是 1s → 2s（§1.2）：两次重试各等一次
    assert _SLEPT == [1.0, 2.0]


def test_does_not_retry_4xx() -> None:
    """4xx 立即抛 —— 重试只是浪费配额与时间（§1.3）。"""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(400, text="bad request")

    with _client(handler) as c, pytest.raises(LLMCallError):
        c.chat([{"role": "user", "content": "hi"}])
    assert len(seen) == 1, "4xx 不该重试"


def test_retries_connection_errors() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=_ok_body())

    with _client(handler) as c:
        c.chat([{"role": "user", "content": "hi"}])
    assert len(seen) == 2


def test_reports_token_usage() -> None:
    """额度账本要记真实 token（决策 14），所以用量必须从返回体里取出来。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    with _client(handler) as c:
        reply = c.chat([{"role": "user", "content": "hi"}])
    assert (reply.prompt_tokens, reply.completion_tokens) == (7, 9)


def test_json_mode_sends_response_format_and_parses() -> None:
    """结构化输出用 response_format（§1.5），并顺手把 JSON 解析好。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body('{"分数": 88}'))

    with _client(handler) as c:
        data, _ = c.chat_json([{"role": "user", "content": "hi"}])
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert data == {"分数": 88}


def test_empty_choices_is_a_call_error() -> None:
    """v1 有一处「choices[0] 空列表裸 IndexError」的未修债，这里必须是显式错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with _client(handler) as c, pytest.raises(LLMCallError):
        c.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# 推理型模型的两种静默失败（真调之后才发现的）
# ---------------------------------------------------------------------------
def _body(content: str, *, finish: str = "stop", reasoning: str = "", reasoning_tokens: int = 0):
    return {
        "model": "m",
        "choices": [{"message": {"role": "assistant", "content": content,
                                "reasoning_content": reasoning}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


def test_truncated_json_is_reported_as_a_budget_problem_not_a_parse_problem() -> None:
    """**关键区分**：`finish_reason=length` 时的解析失败是**预算不够**，不是模型答错。

    实测确认：这个模型会先输出 `reasoning_content`，而**推理也吃 `max_tokens`**
    （467 个完成 token 里 394 个是推理）。预算太小就会截断出半个 JSON。
    若不区分，调用方只看到"解析失败"，会得出"模型不行"的错误结论 ——
    而真正的处置是调大 max_tokens。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body('{"hits": [{"criterion_id": 1,', finish="length"))

    with _client(handler) as c, pytest.raises(LLMCallError) as e:
        c.chat_json([{"role": "user", "content": "hi"}])
    assert "截断" in str(e.value)
    assert "预算" in str(e.value)


def test_truncated_but_parseable_output_is_still_accepted() -> None:
    """截断但**恰好收尾**的输出照常接受 —— 答案本来就完整，不该因为 finish_reason 报错。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body('{"ok": true}', finish="length"))

    with _client(handler) as c:
        data, _ = c.chat_json([{"role": "user", "content": "hi"}])
    assert data == {"ok": True}


def test_empty_content_with_reasoning_is_a_budget_problem() -> None:
    """内容全空、token 全花在推理上 → 不是"模型没答"，是"还没轮到它答"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_body("", finish="length", reasoning="想了很多但没写出来", reasoning_tokens=40)
        )

    with _client(handler) as c, pytest.raises(LLMCallError) as e:
        c.chat_json([{"role": "user", "content": "hi"}])
    assert "推理" in str(e.value)
    assert "max_tokens" in str(e.value)


def test_plain_empty_content_is_also_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body(""))

    with _client(handler) as c, pytest.raises(LLMCallError):
        c.chat_json([{"role": "user", "content": "hi"}])


def test_finish_reason_and_reasoning_are_exposed() -> None:
    """这两个字段必须能被调用方看到 —— 否则上面那些区分无从判断。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body('{"a": 1}', reasoning="想了想", reasoning_tokens=5))

    with _client(handler) as c:
        reply = c.chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert reply.finish_reason == "stop"
    assert reply.reasoning_text == "想了想"
    assert reply.reasoning_tokens == 5


def test_usage_is_accumulated_across_calls() -> None:
    """**决策 14 的第二道安全网**：客户端累计用量，调用处取差值记账。

    为什么是累计而不是"最近一次"：一次 HTTP 请求里可能调用多次模型
    （判定 + 判分 + 总结），"最近一次"只能记到最后一次 —— 实测就这么漏了两次。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body('{"a": 1}'))

    with _client(handler) as c:
        c.chat([{"role": "user", "content": "hi"}])
        first = dict(c.usage_total)
        c.chat([{"role": "user", "content": "hi"}])
        second = dict(c.usage_total)

    assert first["calls"] == 1
    assert second["calls"] == 2
    assert second["prompt_tokens"] == first["prompt_tokens"] * 2
    assert second["completion_tokens"] == first["completion_tokens"] * 2
    assert second["reasoning_tokens"] == first["reasoning_tokens"] * 2


# ---------------------------------------------------------------------------
# JSON 容错（§1.6 / §1.7 / §1.8）
# ---------------------------------------------------------------------------
def test_trailing_commas_are_tolerated() -> None:
    assert loads_tolerant('{"a": 1,}') == {"a": 1}
    assert loads_tolerant("[1, 2,]") == [1, 2]


def test_trailing_comma_stripper_does_not_touch_strings() -> None:
    """**关键性质**：字符串字面量里的逗号与转义引号不受影响（§1.6）。

    朴素的正则 `re.sub(r",\\s*([}\\]])", ...)` 会把 `",}"` 一起改掉 ——
    v1 特意用手写状态机避开它，这条测试就是那个决定的守门人。
    """
    src = '{"note": "结尾是 ,}", "q": "he said \\"hi,\\" then left"}'
    assert strip_trailing_commas(src) == src
    assert loads_tolerant(src) == {"note": "结尾是 ,}", "q": 'he said "hi," then left'}


def test_code_fence_is_stripped() -> None:
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'
    assert loads_tolerant('```json\n{"a": 1,}\n```') == {"a": 1}


def test_parse_failure_is_a_distinct_error() -> None:
    """调用失败与解析失败是两种错误，调用方据此选择不同降级（§1.8）。"""
    with pytest.raises(LLMParseError):
        loads_tolerant("这不是 JSON")
    assert not issubclass(LLMParseError, LLMCallError)
    assert issubclass(LLMParseError, Exception)


# ---------------------------------------------------------------------------
# prompt 从文件读（ADR-0007 / ADR-0010）
# ---------------------------------------------------------------------------
def test_prompt_is_loaded_relative_to_repo_root_not_cwd(monkeypatch, tmp_dir: Path) -> None:
    """**回归测试**：从别的工作目录读 prompt 也必须成功。

    第一版若用相对 cwd 的路径，从 `tools/` 里跑就会**静默读到空**（§3.1 的入口）。
    这里把 cwd 换到一个无关目录，验证仍然读得到。
    """
    prompts_mod.clear_cache()
    monkeypatch.chdir(tmp_dir)
    prompt = prompts_mod.load("interviewer/score_round.md")
    assert "考察点" in prompt.text


def test_missing_prompt_raises_instead_of_returning_empty() -> None:
    """读不到必须响亮 —— 空 prompt 会让模型自由发挥，而失败现场离这里很远。"""
    with pytest.raises(prompts_mod.PromptNotFound):
        prompts_mod.load("interviewer/没有这个文件.md")


def test_prompt_name_cannot_escape_the_prompts_dir() -> None:
    with pytest.raises(prompts_mod.PromptNotFound):
        prompts_mod.resolve("../../etc/passwd")


def test_every_prompt_mentions_json() -> None:
    """**回归测试**：所有 prompt 里必须出现 "json" 这个词。

    服务端对 `response_format={"type":"json_object"}` 的硬要求是**prompt 正文里
    得有 json 这个词**，否则直接 400：
    `Prompt must contain the word 'json' in some form to use 'response_format'`。

    这类错只有**真调一次**才会暴露，而且表现是"报告总结每次都静默降级成确定性
    文案"——看起来像模型不听话，其实是请求根本没被接受。实测踩过：
    `report_summary.md` 写的是"综合成一段人话"，通篇没有 json。

    只查"有没有这个词"，不查它出现在哪 —— 这是防 400 的最低要求，
    再严就会开始猜措辞。
    """
    prompts_dir = prompts_mod.PROMPTS_DIR
    checked = 0
    for path in sorted(prompts_dir.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert "json" in text.lower(), (
            f"{path.relative_to(prompts_dir.parent)} 里没有 'json' 这个词 —— "
            f"用 response_format=json_object 调它会被服务端 400 拒绝"
        )
        checked += 1
    assert checked >= 3, f"只找到 {checked} 个 prompt，目录结构可能变了"


def test_unfilled_placeholder_raises() -> None:
    """未填充的占位符送给模型等于让它猜 —— 不许静默。"""
    from app.llm import LLMError, Prompt

    p = Prompt(name="x.md", text="题目：{{stem}}，考察点：{{criteria}}")
    assert p.render(stem="volatile", criteria="可见性") == "题目：volatile，考察点：可见性"
    with pytest.raises(LLMError):
        p.render(stem="volatile")


def test_rendered_prompt_keeps_literal_json_braces() -> None:
    """prompt 正文里的 JSON 示例不该被当成模板语法（字面替换，不是模板引擎）。"""
    from app.llm import Prompt

    p = Prompt(name="x.md", text='返回 {"命中": true} 这样的 JSON，题干：{{stem}}')
    assert '{"命中": true}' in p.render(stem="s")
