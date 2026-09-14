"""面试页 / 报告页的端到端测试（**走真 HTTP 流程**，LLM 用替身）。

这是 MVP 的验收测试：登录 → 挑题 → 开始追问 → 逐轮作答 → 收尾 → 看报告 → 看矩阵。
每一步都断言**页面上真的出现了那段东西**，而不是"函数返回了对象" —— 因为这一笔
要证明的就是"这条链能跑通"。

LLM 用 `tests/fakes.FakeLLM` 通过 `dependency_overrides` 注入：**不联网、不花钱**，
而流程本身与线上走的是同一条代码路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.account import repository as account_repository
from app.account import service as account
from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Domain, KnowledgePoint, Question, User
from app.deps import get_llm
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply, round_reply

INVITE = "PAGE-INVITE"

#: 夹具里所有测试用户共用这一个口令。算一次（scrypt 是刻意昂贵的）。
PASSWORD = "secret123"
_PASSWORD_HASH = hash_password(PASSWORD)


def _round(hits, followup="继续", finish=False):
    return round_reply(hits=hits, prose=followup, finish=finish)


def _eval(accuracy=80, completeness=80, clarity=80, depth=80, review="评语"):
    return FakeReply(
        data={
            "scores": {"accuracy": accuracy, "completeness": completeness,
                       "clarity": clarity, "depth": depth},
            "review": review,
        }
    )


def _summary(text="这场面试说明并发基础还需补。"):
    return FakeReply(data={"summary": text})


def _left(spent: int) -> str:
    """`/me` 上的额度读数 —— 从常量算（决策 71 标定过一次，别再写死）。"""
    from app.account import service as account

    return f"{account.DAILY_UNITS - spent} / {account.DAILY_UNITS}"

@pytest.fixture
def app(tmp_dir: Path):
    db = tmp_dir / "pages.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(User(id=2, email="page@local", username="page", password_hash=_PASSWORD_HASH, role="user"))
        account_repository.create_invite(s, INVITE)
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        for seq, text in enumerate(["可见性与有序性", "底层内存屏障", "不保证原子性"], start=1):
            s.add(Criterion(id=seq, point_id=1, seq=seq, text=text, shared=0))
        s.flush()
        s.add(
            Question(id=1, kind="knowledge", stem="说说 volatile 的作用", difficulty=3,
                     primary_point_id=1, origin="seed", visibility="public")
        )
        s.commit()

    # 抽题是按 id 倒序取最近的几道，所以这场面试的题序是 3 → 2 → 1；
    # 每个题会话都要经历"判定 → 收尾时判分 → 总结"，共 3 次 LLM 调用。
    application = create_app(Settings(database_path=db))
    application.state.test_db = db
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> None:
    r = client.post(
        "/login", data={"email": "page@local", "password": "secret123"}, follow_redirects=False
    )
    assert r.status_code == 302


def _start_drill(client: TestClient) -> str:
    r = client.post(
        "/interview/start",
        data={"mode": "drill", "question_id": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    return r.headers["location"]


# ---------------------------------------------------------------------------
# 页面存在与守卫
# ---------------------------------------------------------------------------
def test_bank_detail_offers_to_start_when_logged_in(client: TestClient) -> None:
    assert "登录后开始面试" in client.get("/bank/1").text
    _login(client)
    body = client.get("/bank/1").text
    assert "单题追问" in body and "模拟面试" in body


def test_interview_requires_login(client: TestClient) -> None:
    """未登录访问答题页 → **403 的错误页**。

    状态码必须是 403 而不是 200：第一版让异常处理器自己挑状态码，于是
    `Forbidden` 渲染出 **200 的错误页** —— 缓存、爬虫、前端分支全都会判断错，
    而这类错没人会肉眼发现（页面上文字是对的）。
    """
    r = client.get("/interview/1", follow_redirects=False)
    assert r.status_code == 403
    assert "请先登录" in r.text
    assert "volatile" not in r.text, "匿名不该看到任何题面内容"


def test_me_shows_matrix_with_untested_points_as_blank(client: TestClient) -> None:
    """**「没考过」显示为"没考过"，不是 0%** —— 这是产品要求，页面上也要对。"""
    _login(client)
    body = client.get("/me").text
    assert "掌握度矩阵" in body
    assert "没考过" in body
    assert "0%" not in body


# ---------------------------------------------------------------------------
# 跑通一整场单题追问
# ---------------------------------------------------------------------------
def test_full_drill_flow_through_http(app, client: TestClient) -> None:
    fake = FakeLLM().queue(
        _round([(1, "命中"), (2, "未命中"), (3, "未命中")], followup="那内存屏障呢？"),
        _round([(1, "未命中"), (2, "命中"), (3, "未命中")], finish=True),
        _eval(accuracy=80, completeness=70, clarity=90, depth=60, review="可见性答对了"),
        _summary("volatile 的可见性你答到了，内存屏障还需要补。"),
    )
    app.dependency_overrides[get_llm] = lambda: fake

    _login(client)
    location = _start_drill(client)

    # 答题页：题干、知识点、考察点都在
    body = client.get(location).text
    assert "说说 volatile 的作用" in body
    assert "volatile" in body
    assert "底层内存屏障" in body

    # 第 1 轮
    r = client.post(f"{location}/answer", data={"answer_text": "保证可见性"}, follow_redirects=False)
    assert r.status_code == 200
    assert "那内存屏障呢？" in r.text, "面试官的追问必须显示出来"

    # 第 2 轮 → 收尾 → 跳报告
    r = client.post(f"{location}/answer", data={"answer_text": "靠内存屏障"}, follow_redirects=False)
    assert r.status_code == 302
    report_url = r.headers["location"]
    assert report_url.startswith("/report/")

    # 报告页：六块里能显示的那几块
    report = client.get(report_url).text
    assert "面试报告" in report
    assert "逐题回顾" in report
    assert "可见性答对了" in report
    assert "掌握度变化" in report
    assert "volatile" in report
    assert "volatile 的可见性你答到了" in report
    assert "1 / 2" in report or "被考" in report

    # 个人状态页：矩阵里 volatile 已经有读数了
    me = client.get("/me").text
    assert "看报告" in me


def test_quota_is_charged_and_shown(app, client: TestClient) -> None:
    fake = FakeLLM().queue(
        _round([(1, "命中"), (2, "命中"), (3, "命中")], finish=True),
        _eval(), _summary(),
    )
    app.dependency_overrides[get_llm] = lambda: fake
    _login(client)
    _start_drill(client)
    assert _left(1) in client.get("/me").text, "单题追问扣 1 点"


def test_interview_mode_charges_six_and_walks_through_questions(app, client: TestClient) -> None:
    """模拟面试：多题 → 每题追问 → 最后一题答完才出报告。

    ⚠️ 三道路题**都必须挂考察点**：没有考察点的题永远满足不了收尾判据
    （"全部考察点已命中"无从判定），于是那道题会一直追问到 `max_rounds` ——
    这是引擎的正确行为，也让"抽 3 道题"这件事在生产上依赖每道题都有考察点定义。
    """
    from app.db.models import Question

    # 追加两道题时**必须挂上同一个知识点**：没有 primary_point_id 的题取不到考察点，
    # 于是模型每轮都在"无考察点"下作答（判分判不出命中、收尾判据也永远不满足）。
    # 这是引擎的正确行为，也是本夹具第一版的漏洞 —— 它让测试以为流程断了，
    # 其实断的是数据。
    with create_session_factory(create_db_engine(app.state.test_db))() as s:
        for qid in (2, 3):
            s.add(
                Question(id=qid, kind="knowledge", stem=f"第 {qid} 道题", difficulty=3,
                         primary_point_id=1, origin="seed", visibility="public")
            )
        s.commit()

    # 按**调用种类**排队而不是按顺序：一场多题面试里，LLM 被调用的次数取决于
    # 代码的收尾判据（一题可能问 1 轮也可能问 3 轮）。用顺序队列写测试等于在测试里
    # 重写一遍那个判据 —— 改判据就要改一堆测试，且失败信息指不出真因（实测踩过）。
    fake = FakeLLM()
    for _ in range(3 * 3):
        fake.on("round", _round([(1, "命中"), (2, "命中"), (3, "命中")], finish=True))
        fake.on("eval", _eval())
        fake.on("summary", _summary())
    app.dependency_overrides[get_llm] = lambda: fake
    _login(client)

    r = client.post("/interview/start", data={"mode": "interview"}, follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    assert _left(account.COST["interview"]) in client.get("/me").text, "模拟面试按 COST 扣点"

    # 第一题答完应当**跳到下一题**，而不是直接出报告
    r = client.post(f"{location}/answer", data={"answer_text": "a"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/interview/")
    assert r.headers["location"] != location

    # 一路答到最后一题 → 报告
    current = r.headers["location"]
    for _ in range(2):
        r = client.post(f"{current}/answer", data={"answer_text": "a"}, follow_redirects=False)
        assert r.status_code == 302
        current = r.headers["location"]
    assert current.startswith("/report/")

    report = client.get(current).text
    assert report.count("逐题回顾") == 1
    assert "第 3 题" in report


# ---------------------------------------------------------------------------
# 越权与降级
# ---------------------------------------------------------------------------
def test_cannot_open_someone_elses_interview(app, client: TestClient) -> None:
    """**改 URL 不能答别人的题**（`sessions` 自己没 user_id，归属靠 interviews join）。"""
    fake = FakeLLM().queue(_round([(1, "命中"), (2, "命中"), (3, "命中")]))
    app.dependency_overrides[get_llm] = lambda: fake
    _login(client)
    location = _start_drill(client)
    client.post("/logout")

    # 换一个用户登录，再去访问那个会话
    with create_session_factory(create_db_engine(app.state.test_db))() as s:
        s.add(User(id=3, email="other@local", username="o", password_hash=_PASSWORD_HASH, role="user"))
        s.commit()
    client.post("/login", data={"email": "other@local", "password": "secret123"})
    assert client.get(location).status_code == 404


def test_cannot_open_someone_elses_report(app, client: TestClient) -> None:
    fake = FakeLLM().queue(
        _round([(1, "命中"), (2, "命中"), (3, "命中")], finish=True),
        _eval(), _summary(),
    )
    app.dependency_overrides[get_llm] = lambda: fake
    _login(client)
    location = _start_drill(client)
    r = client.post(f"{location}/answer", data={"answer_text": "a"}, follow_redirects=False)
    report_url = r.headers["location"]
    client.post("/logout")

    with create_session_factory(create_db_engine(app.state.test_db))() as s:
        s.add(User(id=4, email="nosy@local", username="n", password_hash=_PASSWORD_HASH, role="user"))
        s.commit()
    client.post("/login", data={"email": "nosy@local", "password": "secret123"})
    assert client.get(report_url).status_code == 404


def test_llm_failure_is_visible_to_the_user_not_silent(app, client: TestClient) -> None:
    """**降级可以，静默不行**（AGENTS.md §3.1）：模型没接上时页面上必须有一句提示，
    而且这一轮仍然落库（用户可以继续答）。"""
    from app.llm import LLMCallError

    fake = FakeLLM().queue(FakeReply(error=LLMCallError("模型挂了")))
    app.dependency_overrides[get_llm] = lambda: fake
    _login(client)
    location = _start_drill(client)

    r = client.post(f"{location}/answer", data={"answer_text": "a"})
    assert r.status_code == 200
    assert "判定没成功" in r.text
    assert "未涉及" in r.text  # 本轮记为未涉及，页面上要看得见


def test_missing_api_key_degrades_instead_of_crashing(tmp_dir: Path) -> None:
    """没配 key 的机器上**应用照样起得来**，只是每次调用得到一条明确的失败。"""
    db = tmp_dir / "nokey.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(User(id=2, email="n@local", username="n", password_hash=_PASSWORD_HASH, role="user"))
        s.commit()
    app = create_app(Settings(database_path=db, llm_api_key=""))
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        c.post("/login", data={"email": "n@local", "password": "secret123"})
        assert c.get("/me").status_code == 200
