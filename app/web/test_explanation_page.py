"""讲解的页面通路（决策 67）。

页面这一侧要证明三件事：

· **按需生成**：源码里没有讲解 → 用户点一下才生成（不预生成、不进审核队列）
· **可见性照旧**：别人的私有题连讲解都拿不到（题目行来自 `service.detail()`）
· **失败可见**（§3.1）：模型挂了要在页面上说一句，而不是静默地什么都不显示
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Domain, Explanation, KnowledgePoint, Question, User
from app.deps import get_llm
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


def _quota_line(client) -> str:
    """页面上的额度行 —— 数字从常量算（决策 71 标定过一次，别再写死）。"""
    from app.account import service as account

    return f"还剩 <strong>{account.DAILY_UNITS} / {account.DAILY_UNITS}</strong> 点"

@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "explain-page.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="e@local", username="e", password_hash=_HASH, role="user"))
        s.add(User(id=3, email="other@local", username="o", password_hash=_HASH, role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile 的作用", difficulty=3,
                       primary_point_id=1, origin="seed", visibility="public"))
        s.add(Question(id=2, kind="design", stem="别人的私有题", difficulty=3,
                       origin="generated", owner_user_id=3, visibility="private"))
        s.commit()
    return path


@pytest.fixture
def app(db: Path):
    application = create_app(Settings(database_path=db))
    application.state.test_db = db
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "e@local", "password": PASSWORD})
        yield c


def _explanations(db: Path) -> list[Explanation]:
    with create_session_factory(create_db_engine(db))() as s:
        return list(s.execute(select(Explanation)).scalars().all())


def test_detail_offers_generation_when_no_explanation(app, client: TestClient) -> None:
    """没有讲解时给一个按钮 —— 而不是自动生成（那等于预生成）。"""
    body = client.get("/bank/1").text
    assert "讲解" in body
    assert "生成讲解" in body
    assert 'action="/bank/1/explain"' in body
    assert _explanations(app.state.test_db) == [], "只看页面不该产生任何生成"


def test_generating_then_reading_it_back(app, client: TestClient, db: Path) -> None:
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue_text("volatile 讲的是可见性。")

    r = client.post("/bank/1/explain", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/bank/1?explain=ok"

    body = client.get("/bank/1").text
    assert "volatile 讲的是可见性。" in body
    assert "生成讲解" not in body, "已经有讲解了，就不该再摆生成按钮"
    assert len(_explanations(db)) == 1


class CountingLLM(FakeLLM):
    """会**累计用量**的替身（`FakeLLM` 本身不带 `usage_total`）。

    记账读的是**差值**（请求开始/结束各取一次），所以替身必须在调用**过程中**把
    用量加上去 —— 一次性设好初值的话，差值恒为 0，那条断言就永远"通过"而什么都没测。
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def chat(self, *args, **kwargs):
        reply = super().chat(*args, **kwargs)
        self.usage_total["prompt_tokens"] += 100
        self.usage_total["completion_tokens"] += 50
        self.usage_total["calls"] += 1
        return reply


def test_generation_is_not_charged_in_quota_points(app, client: TestClient, db: Path) -> None:
    """它属于**题库那一边**（基线的额度列里没有它）—— 不扣点。

    但 token 要进账本（决策 14），否则"钱花在哪"答不出来。
    """
    from app.account import service as account

    app.dependency_overrides[get_llm] = lambda: CountingLLM().queue_text("讲解正文")
    client.post("/bank/1/explain", follow_redirects=False)

    assert _quota_line(client) in client.get("/").text, "题库这条线不扣点"
    with create_session_factory(create_db_engine(db))() as s:
        row = account.repository.quota_row(s, 2, account.today())
        assert row is not None and row.tokens_used == 150, "这次调用的 token 要进账本"


def test_model_failure_is_visible_on_the_page(app, client: TestClient, db: Path) -> None:
    """模型挂了 → 页面上有一句"讲解没生成出来"，而且**什么都没落库**（§3.1）。"""
    from app.llm import LLMCallError

    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(
        FakeReply(error=LLMCallError("模型挂了"))
    )
    r = client.post("/bank/1/explain")
    assert r.status_code == 502
    assert "讲解没生成出来" in r.text
    assert "模型挂了" in r.text
    assert _explanations(db) == []


def test_cannot_explain_someone_elses_private_question(
    app, client: TestClient, db: Path
) -> None:
    """别人的私有题连讲解都拿不到 —— 与"不存在"同一种回应（不泄露存在性）。"""
    r = client.post("/bank/2/explain")
    assert r.status_code == 404
    assert _explanations(db) == []


def test_anonymous_sees_a_login_hint_and_cannot_generate(app) -> None:
    with TestClient(app) as c:
        body = c.get("/bank/1").text
        assert "登录" in body and "生成讲解" not in body
        r = c.post("/bank/1/explain", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"


def test_explanation_shows_up_for_everyone_once_cached(app, client: TestClient, db: Path) -> None:
    """缓存是**共享**的：一个人生成过，所有人打开都直接读到（这正是它省钱的方式）。"""
    app.dependency_overrides[get_llm] = lambda: FakeLLM().queue_text("共享的讲解")
    client.post("/bank/1/explain", follow_redirects=False)
    client.post("/logout")

    with TestClient(app) as anonymous:
        assert "共享的讲解" in anonymous.get("/bank/1").text


def test_reference_answer_and_explanation_are_separate_sections(
    app, client: TestClient, db: Path
) -> None:
    """参考答案是**预生成**的，讲解是**按需**的 —— 两件事，两块地方（决策 12/67）。"""
    with create_session_factory(create_db_engine(db))() as s:
        s.get(Question, 1).reference_answer = "可见性、有序性，不保证原子性。"
        s.commit()

    body = client.get("/bank/1").text
    assert "参考答案" in body and "可见性、有序性" in body
    assert "讲解" in body and "生成讲解" in body
