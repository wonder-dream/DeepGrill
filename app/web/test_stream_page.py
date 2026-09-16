"""SSE 流式（ADR-0004 给面试页的第三件事）的测试。

流式最容易出的错不是"没流起来"，而是**流错了东西**：

· 把 JSON（命中判定）也漏给用户看 —— 那些结构不该出现在对话里
· 把面试官的话**丢字**（增量切在分隔行中间时最容易丢尾巴）
· 流完了但库里没落 —— 浏览器跳过去会看到上一轮的状态（"答了却像没答"）
· 流断了却假装成功 —— 用户以为答完了，其实那一轮没进库
· 判定失败时屏幕上什么都不发生（流式路径下用户正盯着那一块空白）

`tests.fakes.FakeLLM.stream()` 把回复**切成 7 个字符一段**，而分隔行是 10 个字符 ——
所以它必然被切开。这正是 `ProseFilter` 存在的全部理由。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Attempt, Criterion, Domain, KnowledgePoint, Question, User
from app.deps import get_llm, get_stt
from app.llm import PROSE_JSON_MARKER
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply, round_reply

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)
AUDIO = b"\xde\xad\xbe\xef" * 128


class StubSTT:
    text = "我理解 volatile 保证可见性。"

    def transcribe(self, audio: bytes, *, content_type: str = ""):
        from app.llm.stt import Transcript

        del content_type
        self.seen = audio
        return Transcript(text=self.text)


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "sse.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="s@local", username="s", password_hash=_HASH, role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile", difficulty=3,
                       primary_point_id=1, origin="seed", visibility="public"))
        s.commit()
    return path


@pytest.fixture
def app(db: Path):
    # ⚠️ **显式钉住 `stt_provider`**：不写的话它会从开发者的 `.env` 里读 ——
    # 而"本地把语音配上了"会让"没配供应商时明确失败"这条测试**反过来红**。
    # 测试不该依赖跑它的人机器上有什么（实测：把 `.env` 的 STT 配成 api 那天，
    # 这条测试立刻红了，而代码一行没错）。
    application = create_app(Settings(database_path=db, stt_provider="none"))
    application.state.test_db = db
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "s@local", "password": PASSWORD})
        yield c


def _start(client: TestClient) -> str:
    r = client.post("/interview/start", data={"mode": "drill", "question_id": "1"},
                    follow_redirects=False)
    assert r.status_code == 302
    return r.headers["location"]


def frames(body: str) -> list[tuple[str, dict]]:
    """把 SSE 正文拆成 `[(事件名, payload)]` —— 顺便证明帧格式是合规的。

    解析失败时**当场报错**，不返回空列表：一个"解析不出任何帧"的测试如果静默返回
    空列表，那么"流是空的"这条 bug 会伪装成"断言不成立"（看起来像测试写错了）。
    """
    out: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data += line[len("data:"):].strip()
        assert event, f"这一帧没有事件名：{block!r}"
        out.append((event, json.loads(data)))
    return out


def prose_of(body: str) -> str:
    return "".join(p.get("delta", "") for e, p in frames(body) if e == "prose")


def _attempts(db: Path) -> list[Attempt]:
    with create_session_factory(create_db_engine(db))() as s:
        return list(s.execute(select(Attempt).order_by(Attempt.round_no)).scalars().all())


# ---------------------------------------------------------------------------
# 打字那条路
# ---------------------------------------------------------------------------
def test_stream_sends_prose_then_done(app, client: TestClient, db: Path) -> None:
    fake = FakeLLM().queue(round_reply(hits=[(1, "命中")], prose="那内存屏障呢？", finish=False))
    app.dependency_overrides[get_llm] = lambda: fake
    location = _start(client)

    r = client.post(f"{location}/answer/stream", data={"answer_text": "保证可见性"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert fake.streamed, "应该走的是流式那条路（chat 不算）"

    events = frames(r.text)
    assert events[0][0] == "prose"
    assert "那内存屏障呢？" in prose_of(r.text)
    assert events[-1][0] == "done"
    assert events[-1][1]["redirect"] == location, "这一题还没问完 → 回到同一页"


def test_stream_never_leaks_the_judgement_json(app, client: TestClient, db: Path) -> None:
    """**结构不能漏给用户**：命中判定是内部的，对话里只该出现那句话。"""
    prose = "好，那你说说内存屏障。"
    fake = FakeLLM().queue(round_reply(hits=[(1, "命中")], prose=prose))
    app.dependency_overrides[get_llm] = lambda: fake
    location = _start(client)

    body = client.post(f"{location}/answer/stream", data={"answer_text": "a"}).text
    assert PROSE_JSON_MARKER not in body
    assert "criterion_id" not in body and "hits" not in body
    assert prose_of(body).strip() == prose, "一个字的 JSON 都不该混进来"


def test_stream_does_not_lose_the_last_characters(app, client: TestClient, db: Path) -> None:
    """**增量会被切在分隔行中间**（替身按 7 字符切），所以末尾几个字最容易丢。

    这条断言的是"拼起来的散文 == 模型原话"，一个字符都不能少。
    """
    prose = "先说说可见性，再说内存屏障，最后说说原子性。"
    fake = FakeLLM().queue(round_reply(hits=[(1, "命中")], prose=prose))
    app.dependency_overrides[get_llm] = lambda: fake
    location = _start(client)

    body = client.post(f"{location}/answer/stream", data={"answer_text": "a"}).text
    assert prose_of(body).strip() == prose
    assert "原子性。" in body, "最后那几个字最容易被缓冲吞掉"


def test_stream_persists_the_round_before_done(app, client: TestClient, db: Path) -> None:
    """**done 之前必须落库**：浏览器收到 done 就跳转，而那次渲染是另一个请求。

    （第一版把提交交给请求依赖的收尾——那发生在响应**结束之后**，于是跳过去看到
    的是上一轮的状态：答了却像没答。）
    """
    fake = FakeLLM().queue(round_reply(hits=[(1, "命中")], prose="继续"))
    app.dependency_overrides[get_llm] = lambda: fake
    location = _start(client)
    client.post(f"{location}/answer/stream", data={"answer_text": "保证可见性"})

    # 用**另一个连接**读（不是这个请求的会话）—— 这才是浏览器跳过去时看到的东西
    attempts = _attempts(db)
    assert len(attempts) == 1
    assert attempts[0].answer_text == "保证可见性"
    assert attempts[0].feedback_text == "继续", "面试官的话也要落库（报告与历史都读它）"
    assert attempts[0].hits == {"1": "命中"}


def test_stream_redirects_to_the_report_after_the_last_question(
    app, client: TestClient, db: Path
) -> None:
    fake = FakeLLM()
    fake.on("round", round_reply(hits=[(1, "命中")], prose="行", finish=True))
    fake.on("eval", FakeReply(data={
        "scores": {"accuracy": 80, "completeness": 80, "clarity": 80, "depth": 80},
        "review": "还行",
    }))
    fake.on("summary", FakeReply(data={"summary": "还行。"}))
    app.dependency_overrides[get_llm] = lambda: fake
    location = _start(client)

    body = client.post(f"{location}/answer/stream", data={"answer_text": "a"}).text
    done = [p for e, p in frames(body) if e == "done"][0]
    assert done["redirect"].startswith("/report/"), "最后一题答完 → 去报告"


def test_stream_reports_model_failure_instead_of_silence(
    app, client: TestClient, db: Path
) -> None:
    """判定失败时**屏幕上必须有那句话** —— 流式路径下用户正盯着一块空白。

    （非流式那条路不需要：整页重渲染会把提示带出来。）
    """
    from app.llm import LLMCallError

    fake = FakeLLM().queue(FakeReply(error=LLMCallError("模型挂了")))
    app.dependency_overrides[get_llm] = lambda: fake
    location = _start(client)

    body = client.post(f"{location}/answer/stream", data={"answer_text": "a"}).text
    assert "面试官这轮没接上" in prose_of(body)
    done = [p for e, p in frames(body) if e == "done"][0]
    assert done["llm_failed"] is True
    assert _attempts(db)[0].hits == {"1": "未涉及"}, "失败也要落库，且记为未涉及"


def test_stream_without_the_marker_degrades_visibly(app, client: TestClient, db: Path) -> None:
    """模型忘了分隔行 → 整轮降级（**不猜**），而页面上看得到那句话。"""
    fake = FakeLLM().queue(FakeReply(text="我就随便说一句，没有分隔行。"))
    app.dependency_overrides[get_llm] = lambda: fake
    location = _start(client)

    body = client.post(f"{location}/answer/stream", data={"answer_text": "a"}).text
    assert "面试官这轮没接上" in prose_of(body)
    assert [p for e, p in frames(body) if e == "done"][0]["llm_failed"] is True


def test_stream_error_frame_keeps_internals_out_of_the_browser(
    app, client: TestClient, db: Path
) -> None:
    """流中途炸掉时，帧里只许有**人话**。

    实测（probe5，没配 key 的实例）：浏览器收到的帧是
    `{"message": "AttributeError: '_MissingKeyLLM' object has no attribute 'stream'"}` ——
    内部类型名与属性名都漏给了用户。`except Exception` 那句 `f"{type(e).__name__}: {e}"`
    是这类泄漏的通用形状；原因与栈该进日志。
    """

    class Exploding:
        """任何调用都炸，且错误文案里带着"内部细节"。"""

        def stream(self, messages, **kwargs):
            raise RuntimeError("内部细节：/srv/secret/path.py 第 42 行")

        def chat(self, messages, **kwargs):
            raise RuntimeError("内部细节：/srv/secret/path.py 第 42 行")

    app.dependency_overrides[get_llm] = Exploding
    location = _start(client)

    body = client.post(f"{location}/answer/stream", data={"answer_text": "a"}).text
    events = frames(body)
    assert events[-1][0] == "error", f"应当以 error 帧收尾：{events}"
    message = events[-1][1]["message"]
    assert "内部细节" not in message and "RuntimeError" not in message, (
        f"帧里漏了内部细节：{message!r}"
    )
    assert "服务端" in message, "但也不能什么都不说（§3.1：降级不静默）"


def test_stream_without_an_api_key_degrades_instead_of_crashing(
    db: Path,
) -> None:
    """没配 key 时流式那条路要**走降级**，不是崩成 AttributeError。

    实测（probe5 / BUGREPORT 1.7）：`_MissingKeyLLM` 只实现了 `chat`/`chat_json`，
    而面试页的流式路直接调 `llm.stream(...)` —— 抛出的 `AttributeError` 不是
    `LLMError`，于是 `run_round` 的降级分支抓不到，最后变成一帧内部异常原文。
    这里用的是**真的替身**（不 override），因为要测的正是它。
    """
    application = create_app(Settings(database_path=db, stt_provider="none", llm_api_key=""))
    with TestClient(application) as c:
        c.post("/login", data={"email": "s@local", "password": PASSWORD})
        location = _start(c)
        body = c.post(f"{location}/answer/stream", data={"answer_text": "a"}).text

    events = frames(body)
    assert "AttributeError" not in body
    assert events[-1][0] == "done", f"应当是「没接上」的正常降级，而不是错误帧：{events}"
    assert "面试官这轮没接上" in prose_of(body)
    assert events[-1][1]["llm_failed"] is True
    attempts = _attempts(db)
    assert len(attempts) == 1, "失败的那一轮仍然要落库（§3.1）"
    assert attempts[0].llm_error, "失败原因要可查询（迁移 0006）"


def test_the_stream_response_carries_the_abort_finalizer(
    app, client: TestClient, db: Path
) -> None:
    """**回归测试**：收尾挂在响应的 `background=` 上 —— 那才是真实断开时会跑的那条路。

    为什么不能只靠生成器的 `finally`：`StreamingResponse` 把同步生成器包成异步生成器
    （`starlette/concurrency.py::iterate_in_threadpool`），**那个包装没有 try/finally** ——
    客户端断开时底层生成器根本不会被关闭，它的 `finally` 等的是 GC（实测：断开之后
    `attempts` 一直是 0）。background 则一定会跑：uvicorn 用 ASGI `spec_version 2.3`，
    Starlette 走"任务组 + `listen_for_disconnect`"那条分支，断开 → 取消流任务 →
    任务组正常退出 → `await self.background()`。

    这里直接把那个 background 调起来（就是断开时 Starlette 会做的事），断言这一轮
    被补落库；再调一次，断言**幂等**（只有一行）。
    """
    import asyncio

    from app.db.models import Attempt, User
    from app.web.interview_page import _stream_response

    fake = FakeLLM().queue(round_reply(hits=[(1, "命中")], prose="继续说说。"))
    location = _start(client)
    session_id = int(location.rsplit("/", 1)[1])
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        me = s.get(User, 2)
        assert me is not None

    response = _stream_response(
        engine=engine, llm=fake, me=me, session_id=session_id, answer_text="保证可见性"
    )
    assert response.background is not None, "收尾必须挂在 background 上（断开时只有它会跑）"

    asyncio.run(response.background())          # ← 客户端断开时 Starlette 做的事
    asyncio.run(response.background())          # 幂等：再来一次也只有一行

    with create_session_factory(engine)() as s:
        rows = s.execute(select(Attempt).where(Attempt.session_id == session_id)).scalars().all()
        assert len(rows) == 1, f"断开的那一轮必须落一条 attempts，实际 {len(rows)} 条"
        assert "断了" in (rows[0].llm_error or "")
    engine.dispose()


def test_a_client_disconnect_still_records_the_round_and_the_cost(
    app, client: TestClient, db: Path
) -> None:
    """客户端中途关掉页面时，这一轮**必须留下痕迹**（§3.1），已知的 token 也要记账。

    实测（probe16 真模型 18.1s / probe19 假模型）：收到第一段 prose 之后断开 →
    `attempts` 为空、`tokens_used` 一点没动。断开时生成器被 `GeneratorExit` 关掉，
    而**它不是 `Exception`** —— 所以收尾必须挂在 `finally` 上，而且要做两件事：
      · 这一轮以降级形态落库（全部「未涉及」+ `llm_error` 写明断开）
      · 把**已经知道的** token 记进账本
        （按 OpenAI 的约定 usage 在最后一个 chunk 才到，中途断开时通常是 0 ——
         那半个洞补不上，所以这里用一个"调用时就报用量"的替身来钉住记账那一半）

    这里不走 TestClient 发请求，而是**直接对着生成器**：拿到第一帧之后 `close()`
    ——那就是"浏览器把连接关了"在服务端看到的东西（deterministic，不靠时序）。
    """
    from app.account import repository as account_repository
    from app.account import service as account
    from app.db.models import Attempt, User
    from app.web.interview_page import _round_stream

    class Metered(FakeLLM):
        def __init__(self, **kw) -> None:
            super().__init__(**kw)
            self.usage_total = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "calls": 0,
                "missing_usage_calls": 0,
            }

        def chat(self, *args, **kwargs):
            reply = super().chat(*args, **kwargs)
            self.usage_total["prompt_tokens"] += 700
            self.usage_total["completion_tokens"] += 300
            self.usage_total["calls"] += 1
            return reply

    fake = Metered().queue(round_reply(hits=[(1, "命中")], prose="继续说说。"))
    location = _start(client)
    session_id = int(location.rsplit("/", 1)[1])
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        me = s.get(User, 2)
        assert me is not None

    stream = _round_stream(
        engine=engine, llm=fake, me=me, session_id=session_id, answer_text="保证可见性"
    )
    first = next(stream)
    assert "event: prose" in first, f"第一帧应当是面试官的话：{first!r}"
    stream.close()  # ← 等价于客户端断开连接

    with create_session_factory(engine)() as s:
        rows = s.execute(select(Attempt).where(Attempt.session_id == session_id)).scalars().all()
        assert len(rows) == 1, "断开的那一轮必须留下一条 attempts（否则『答了却像没答』）"
        assert rows[0].answer_text == "保证可见性", "候选人的话要留住"
        assert rows[0].llm_error, "失败原因要可查询（迁移 0006）"
        assert "断了" in rows[0].llm_error
        assert set(rows[0].hits.values()) == {"未涉及"}, "没有判定结果 → 全部记「未涉及」"
        ledger = account_repository.quota_row(s, 2, account.today())
    assert ledger is not None and ledger.tokens_used == 1000, (
        "断开之后已经知道的 token 也要记账（模型已经生成过内容了）"
    )
    engine.dispose()


def test_stream_requires_login(app, db: Path) -> None:
    with TestClient(app) as c:
        r = c.post("/interview/1/answer/stream", data={"answer_text": "a"})
        assert r.status_code == 403


def test_cannot_stream_into_someone_elses_session(app, client: TestClient, db: Path) -> None:
    """越权校验必须在**流开始之前**（流一开始状态码就定死了）。"""
    fake = FakeLLM().queue(round_reply(hits=[(1, "命中")], prose="继续"))
    app.dependency_overrides[get_llm] = lambda: fake
    location = _start(client)
    client.post("/logout")
    with create_session_factory(create_db_engine(db))() as s:
        s.add(User(id=3, email="o@local", username="o", password_hash=_HASH, role="user"))
        s.commit()
    client.post("/login", data={"email": "o@local", "password": PASSWORD})
    assert client.post(f"{location}/answer/stream", data={"answer_text": "a"}).status_code == 404
    assert _attempts(db) == []


# ---------------------------------------------------------------------------
# 语音那条路
# ---------------------------------------------------------------------------
def test_voice_stream_sends_prose_and_tags_the_round(app, client: TestClient, db: Path) -> None:
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(
        round_reply(hits=[(1, "命中")], prose="继续说。")
    )
    app.dependency_overrides[get_stt] = StubSTT
    location = _start(client)

    r = client.post(f"{location}/voice/stream",
                    files={"audio": ("a.webm", AUDIO, "audio/webm")})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert prose_of(r.text).strip() == "继续说。"
    done = [p for e, p in frames(r.text) if e == "done"][0]
    assert done["redirect"] == location

    attempt = _attempts(db)[0]
    assert attempt.input_mode == "voice"
    assert attempt.stt_text == StubSTT.text


def test_voice_stream_without_provider_returns_json_not_a_page(
    client: TestClient, db: Path
) -> None:
    """环境类失败回 **JSON**：消费者是 JS，它把消息写进状态行就够了。

    （回一页 HTML 给一个期待 SSE 的消费者，前端只会拿到一屏乱码般的文本。）
    """
    location = _start(client)
    r = client.post(f"{location}/voice/stream",
                    files={"audio": ("a.webm", AUDIO, "audio/webm")})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/json")
    assert "语音转写还没接入" in r.json()["error"]
    assert r.json()["kind"] == "stt_unavailable"
    assert _attempts(db) == []


def test_voice_stream_refuses_an_empty_recording(client: TestClient, db: Path) -> None:
    location = _start(client)
    r = client.post(f"{location}/voice/stream", files={"audio": ("a.webm", b"", "audio/webm")})
    assert r.status_code == 400
    assert "录音是空的" in r.json()["error"]
    assert _attempts(db) == []


def test_voice_stream_never_stores_the_audio(app, client: TestClient, db: Path) -> None:
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(round_reply(hits=[(1, "命中")]))
    app.dependency_overrides[get_stt] = StubSTT
    location = _start(client)
    client.post(f"{location}/voice/stream", files={"audio": ("a.webm", AUDIO, "audio/webm")})
    assert AUDIO not in db.read_bytes()
    assert _attempts(db)[0].stt_text == StubSTT.text


# ---------------------------------------------------------------------------
# 页面接线
# ---------------------------------------------------------------------------
def test_template_points_both_forms_at_the_stream_endpoints(client: TestClient) -> None:
    """两条表单都带上 `data-stream-action` —— 没有 JS 时用 `action` 那条整页路。"""
    location = _start(client)
    body = client.get(location).text
    assert f'data-stream-action="{location}/answer/stream"' in body
    assert f'data-stream-action="{location}/voice/stream"' in body
    assert f'action="{location}/answer"' in body
    assert f'action="{location}/voice"' in body
    assert 'id="live-card" hidden' in body, "流式区初始是隐藏的（没有 JS 时它不该空占一块）"


def test_page_without_javascript_still_submits_to_the_plain_endpoints(
    app, client: TestClient, db: Path
) -> None:
    """没有 JS 的那条路必须仍然工作（渐进增强的底座，也是端到端测试走的路）。"""
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(
        round_reply(hits=[(1, "命中")], prose="那内存屏障呢？")
    )
    location = _start(client)
    r = client.post(f"{location}/answer", data={"answer_text": "保证可见性"})
    assert r.status_code == 200
    assert "那内存屏障呢？" in r.text
    assert _attempts(db)[0].answer_text == "保证可见性"
