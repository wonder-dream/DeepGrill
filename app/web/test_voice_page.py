"""语音输入（决策 32、33）的测试 —— 界面 + **接口**（没有真供应商）。

这一笔只做接口（真接哪家 STT 还没定），所以测试的重心不在"识别得准不准"，而在
**失败时会不会骗人**：

· 没有供应商 → 明确失败、**不落这一轮**（绝不拿一段假转写去骗判分）
· 占位实现（`STT_PROVIDER=fake`）→ 转写照常进判分，但页面上标注"这是占位"
· **原始录音不落库**：这是决策 33 的隐私面，所以用一个独特的字节序列去库里找它
· 转写**直接进判分**（决策 33）：没有"先确认再提交"的中间步骤
· 失败**不中断**这场面试：页面照常渲染，打字框还在
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Attempt, Criterion, Domain, Interview, KnowledgePoint, Question, User
from app.deps import get_llm, get_stt
from app.llm.stt import MAX_AUDIO_BYTES, PLACEHOLDER_TRANSCRIPT, STTError, Transcript
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply, round_reply

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)

#: 一段"音频"：内容随意，但**字节序列独特** —— 便于事后在库里找它（见 `_db_contains`）。
AUDIO = b"\xde\xad\xbe\xef" * 512


class StubSTT:
    """真的会"转写"的替身：把一个固定句子当成识别结果。

    它与 `app.llm.stt.FakeSTT` 的区别：`FakeSTT` 返回**标着占位**的文字（它在产品
    里代表"用占位实现顶一下"），这里返回的是**像人说的话** —— 用来测"转写正常时
    这条链怎么走"。两个都要有：只有前者就测不到"正常"那一半。
    """

    text = "我理解 volatile 保证可见性，但不保证原子性。"

    def transcribe(self, audio: bytes, *, content_type: str = "") -> Transcript:
        del content_type
        self.seen = audio
        return Transcript(text=self.text)


class BoomSTT:
    """识别失败（**不是**"没接供应商"）—— 两种情况页面上说的话不一样。"""

    def transcribe(self, audio: bytes, *, content_type: str = "") -> Transcript:
        del audio, content_type
        raise STTError("供应商返回 502")


def _round(hits=None, followup="继续", finish=False) -> FakeReply:
    return round_reply(hits=hits or [(1, "命中")], prose=followup, finish=finish)


def _eval_reply() -> FakeReply:
    return FakeReply(
        data={
            "scores": {"accuracy": 80, "completeness": 80, "clarity": 80, "depth": 80},
            "review": "还行",
        }
    )


def _left(spent: int) -> str:
    """额度读数 —— 从常量算（决策 71 标定过一次，别再写死）。"""
    from app.account import service as account

    return f"{account.DAILY_UNITS - spent} / {account.DAILY_UNITS}"

@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "voice.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="v@local", username="v", password_hash=_HASH, role="user"))
        s.add(User(id=3, email="other@local", username="o", password_hash=_HASH, role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile", difficulty=3,
                       primary_point_id=1, origin="seed", visibility="public"))
        s.commit()
    return path


def _app(db: Path, *, stt_provider: str = "none"):
    application = create_app(Settings(database_path=db, stt_provider=stt_provider))
    application.state.test_db = db
    return application


@pytest.fixture
def app(db: Path):
    return _app(db)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "v@local", "password": PASSWORD})
        yield c


def _start_drill(client: TestClient) -> str:
    r = client.post("/interview/start", data={"mode": "drill", "question_id": "1"},
                    follow_redirects=False)
    assert r.status_code == 302
    return r.headers["location"]


def _post_voice(client: TestClient, location: str):
    return client.post(f"{location}/voice", files={"audio": ("answer.webm", AUDIO, "audio/webm")})


def _attempts(db: Path) -> list[Attempt]:
    with create_session_factory(create_db_engine(db))() as s:
        return list(s.execute(select(Attempt).order_by(Attempt.round_no)).scalars().all())


def _db_contains(db: Path, needle: bytes) -> bool:
    """这段字节有没有被写进库文件 —— "原始录音不落库"这条就靠它钉住。"""
    return needle in db.read_bytes()


# ---------------------------------------------------------------------------
# 面试页上看得见的东西
# ---------------------------------------------------------------------------
def test_page_offers_voice_by_default_with_a_text_fallback(client: TestClient) -> None:
    """决策 32：**默认语音、可随时切回打字**。"""
    location = _start_drill(client)
    body = client.get(location).text
    assert "语音作答（默认）" in body
    assert "打字作答" in body
    assert 'value="voice" checked' in body
    assert f'action="{location}/voice"' in body
    assert f'action="{location}/answer"' in body
    assert "/static/interview.js" in body


def test_page_is_usable_without_javascript(client: TestClient) -> None:
    """两个面板**都不在 HTML 里被 hidden** —— 否则没 JS 的浏览器就完全答不了题。

    （JS 起来之后才按模式收起一个；这一步不能反过来写。）
    """
    body = client.get(_start_drill(client)).text
    assert 'id="text-pane" hidden' not in body
    assert 'id="voice-pane" hidden' not in body
    assert 'action="/interview/1/answer"' in body, "打字这条路必须始终在 HTML 里"


# ---------------------------------------------------------------------------
# 转写正常时
# ---------------------------------------------------------------------------
def test_voice_round_transcribes_and_judges_in_one_step(app, client: TestClient, db: Path) -> None:
    """决策 33：**不设确认环节** —— 一次请求里转写 + 判分，没有中间页。"""
    stt = StubSTT()
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(_round(followup="那内存屏障呢？"))
    app.dependency_overrides[get_stt] = lambda: stt

    location = _start_drill(client)
    r = _post_voice(client, location)
    assert r.status_code == 200
    assert "那内存屏障呢？" in r.text, "转写直接进了判分，面试官的话就出来了"
    assert stt.seen == AUDIO, "服务端确实把整段录音交给了转写"

    attempts = _attempts(db)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.input_mode == "voice"
    assert attempt.stt_text == StubSTT.text, "stt_text 存**转写原文**"
    assert attempt.answer_text == StubSTT.text, "不设确认环节 → 两列通常相同"
    assert attempt.hits == {"1": "命中"}, "语音轮次与打字轮次在判分上完全一样"


def test_voice_round_shows_a_voice_tag_in_the_transcript(app, client: TestClient, db: Path) -> None:
    """页面上要标注哪一轮是语音 —— 判分时清晰度的标准不一样（ADR-0009）。"""
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(_round())
    app.dependency_overrides[get_stt] = StubSTT
    location = _start_drill(client)
    body = _post_voice(client, location).text
    assert '<span class="tag">语音</span>' in body, "这一轮要标出它是语音作答"
    assert StubSTT.text in body


def test_clarity_hint_switches_to_voice(app, client: TestClient, db: Path) -> None:
    """语音轮的清晰度看的是"说得清不清楚"，不是排版（ADR-0009 的原话）。

    这条走的是真正的分岔点：`_input_mode()` 读**上一轮**的模式，而判分 prompt
    因此换一段说明。
    """
    from app.interview import service as interview

    fake = FakeLLM().queue(_round(finish=True))
    app.dependency_overrides[get_llm] = lambda: fake
    app.dependency_overrides[get_stt] = StubSTT
    location = _start_drill(client)
    _post_voice(client, location)
    session_id = int(location.rsplit("/", 1)[1])

    with create_session_factory(create_db_engine(db))() as s:
        assert interview._input_mode(s, session_id) == "voice"
        ts = interview.get_session_row(s, session_id, 2)
        fake.on("eval", _eval_reply())
        interview.evaluate_session(s, ts=ts, llm=fake)
        assert "语音作答" in fake.last_prompt()


# ---------------------------------------------------------------------------
# 原始录音不落库（决策 33）
# ---------------------------------------------------------------------------
def test_raw_audio_is_never_written_anywhere(app, client: TestClient, db: Path) -> None:
    """**不保存原始录音** —— 用一个独特的字节序列去库里找它。

    这是这条链上唯一的隐私面，也是"不保存"这句话唯一能被机器验证的形式。
    """
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(_round())
    app.dependency_overrides[get_stt] = StubSTT
    location = _start_drill(client)
    _post_voice(client, location)

    assert not _db_contains(db, AUDIO), "库文件里出现了原始录音的字节"
    assert not _db_contains(db, AUDIO[:64]), "连一小段都不该有"
    # 但转写出来的文字在（否则"没保存"就变成了"什么都没存"）
    assert _attempts(db)[0].stt_text == StubSTT.text


# ---------------------------------------------------------------------------
# 没有供应商：明确失败，且不落这一轮
# ---------------------------------------------------------------------------
def test_no_provider_fails_loudly_and_records_nothing(client: TestClient, db: Path) -> None:
    location = _start_drill(client)  # 默认 stt_provider = none
    r = _post_voice(client, location)
    assert r.status_code == 400
    assert "语音转写还没接入" in r.text
    assert "这一轮没有被记录" in r.text
    assert _attempts(db) == [], "转写没有发生，就不该有一条「候选人的回答」"
    assert not _db_contains(db, AUDIO)


def test_failure_does_not_break_the_interview(client: TestClient, db: Path) -> None:
    """失败页照常渲染这一题，打字框还在 —— 用户可以直接改用打字继续。"""
    location = _start_drill(client)
    body = _post_voice(client, location).text
    assert "说说 volatile" in body
    assert f'action="{location}/answer"' in body


def test_provider_failure_and_missing_provider_say_different_things(
    app, client: TestClient, db: Path
) -> None:
    """「没接供应商」与「识别失败」是两件事 —— 页面上说的话不该一样。

    （前者的处置是"去配一下"，后者是"再录一次"。）

    ⚠️ 而**供应商的异常原文不上页面**：`BoomSTT` 那句"供应商返回 502"是探针，
    它只许进日志 —— 真实的 `STTError` 原文里有供应商返回体截断与
    `DEEPGRILL_STT_API_KEY` 这类环境变量名（见 `interview_page._transcribe`）。
    """
    app.dependency_overrides[get_stt] = BoomSTT
    location = _start_drill(client)
    r = _post_voice(client, location)
    assert r.status_code == 400
    assert "这一轮没听清" in r.text
    assert "供应商返回 502" not in r.text, "异常原文漏给了用户"
    assert "语音转写还没接入" not in r.text
    assert _attempts(db) == []


# ---------------------------------------------------------------------------
# 占位实现：能走通，但明说自己是占位
# ---------------------------------------------------------------------------
def test_placeholder_provider_runs_the_whole_chain_but_says_so(db: Path) -> None:
    """`DEEPGRILL_STT_PROVIDER=fake`：整条链真的走通，而页面**标注这是占位**。

    这是"能开发"与"不骗人"之间的那一步：占位转写会落库、会进判分（于是
    `input_mode='voice'` 这条路径可测），但它一眼就能看出不是识别结果。
    """
    app = _app(db, stt_provider="fake")
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(_round())
    with TestClient(app) as client:
        client.post("/login", data={"email": "v@local", "password": PASSWORD})
        location = _start_drill(client)
        body = _post_voice(client, location).text

    assert "占位转写" in body
    # 页面上的**提示**（不只是那段占位文字本身）—— 少了它，一次占位转写就会
    # 静默地看起来像真的识别结果
    assert "不是真的识别结果" in body
    attempts = _attempts(db)
    assert len(attempts) == 1
    assert attempts[0].input_mode == "voice"
    assert attempts[0].stt_text == PLACEHOLDER_TRANSCRIPT


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
def test_empty_recording_is_refused(client: TestClient, db: Path) -> None:
    location = _start_drill(client)
    r = client.post(f"{location}/voice", files={"audio": ("a.webm", b"", "audio/webm")})
    assert r.status_code == 400
    assert "录音是空的" in r.text
    assert _attempts(db) == []


def test_oversized_recording_is_refused(client: TestClient, db: Path) -> None:
    location = _start_drill(client)
    # 用同一段**独特**的字节撑到超限（不能拿一串 0 去库里找：SQLite 的页填充本来
    # 就是 0，那种断言会永远"命中"，等于没测）
    big = AUDIO * (MAX_AUDIO_BYTES // len(AUDIO) + 2)
    assert len(big) > MAX_AUDIO_BYTES
    r = client.post(f"{location}/voice", files={"audio": ("a.webm", big, "audio/webm")})
    assert r.status_code == 400
    assert "录音太大" in r.text
    assert _attempts(db) == []
    assert not _db_contains(db, AUDIO), "被拒的那段也不该留下字节"


def test_voice_requires_login(app, db: Path) -> None:
    with TestClient(app) as c:
        r = c.post("/interview/1/voice", files={"audio": ("a.webm", AUDIO, "audio/webm")})
        assert r.status_code == 403


def test_cannot_answer_someone_elses_session(app, client: TestClient, db: Path) -> None:
    """改 URL 不能往别人的会话里灌语音（归属靠 interviews join）。"""
    app.dependency_overrides[get_stt] = StubSTT
    location = _start_drill(client)
    client.post("/logout")
    client.post("/login", data={"email": "other@local", "password": PASSWORD})
    r = _post_voice(client, location)
    assert r.status_code == 404
    assert _attempts(db) == []


def test_voice_round_does_not_charge_quota_again(app, client: TestClient, db: Path) -> None:
    """额度在**开始**那一次扣（决策 13）—— 语音本身不再扣一遍。"""
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(_round())
    app.dependency_overrides[get_stt] = StubSTT
    location = _start_drill(client)
    _post_voice(client, location)
    assert f"还剩 <strong>{_left(1)}</strong> 点" in client.get("/").text


def test_interview_row_is_untouched_by_voice(app, client: TestClient, db: Path) -> None:
    """语音只影响 `attempts` 两列，不该动 `interviews`（额度与计划都在那儿）。"""
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(_round())
    app.dependency_overrides[get_stt] = StubSTT
    location = _start_drill(client)
    with create_session_factory(create_db_engine(db))() as s:
        before = s.execute(select(Interview)).scalars().one().to_dict()
    _post_voice(client, location)
    with create_session_factory(create_db_engine(db))() as s:
        assert s.execute(select(Interview)).scalars().one().to_dict() == before
