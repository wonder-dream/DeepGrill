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
