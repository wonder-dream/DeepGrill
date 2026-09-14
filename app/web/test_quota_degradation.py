"""额度耗尽后的**降级**（决策 13：耗尽后降级为纯题库模式，不做硬拒绝）。

这条决策的失败方式很隐蔽：把"额度不够"做成一个 403 页也很像"实现完了"，
但那样用户看到的是**一堵墙**，而决策要的是"面试官歇了，题照看"。所以这里的断言
都落在"页面上还能做什么"上：

· 耗尽时**首页 / 题库 / 题目详情**都还在，且都写着"纯题库模式"
· 耗尽时不该再摆出一个按下去必然失败的按钮（那是白跑一趟的往返）
· 但**直接 POST** `/interview/start` 仍要得到一页解释，而不是错误码
· 还剩 3 点时**不是**纯题库模式 —— 单题追问（1 点）仍然可用，
  而模拟面试（6 点）不可用。判据是"最便宜的那个动作"而不是"余量是否为 0"
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
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


# ---------------------------------------------------------------------------
# 纯函数的那一层（边界的答案只该在这里定义一次）
# ---------------------------------------------------------------------------
def test_library_only_is_about_the_cheapest_action() -> None:
    assert account.QuotaState(remaining=0, daily=20).library_only is True
    assert account.QuotaState(remaining=1, daily=20).library_only is False
    assert account.QuotaState(remaining=3, daily=20).library_only is False


def test_affords_answers_per_mode() -> None:
    state = account.QuotaState(remaining=3, daily=20)
    assert state.affords("browse") is True, "题库永远可用"
    assert state.affords("drill") is True
    assert state.affords("interview") is False, "3 点开不起 6 点的模拟面试"


def test_unknown_mode_is_an_input_error_not_false() -> None:
    """未知形态抛 `InvalidInput` 而不是返回 False —— "不支持"和"额度不够"是两件事，
    后端如果把它折成 False，用户会看到一句关于额度的错话。"""
    from app.errors import InvalidInput

    with pytest.raises(InvalidInput):
        account.QuotaState(remaining=99, daily=20).affords("telepathy")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "quota.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="q@local", username="q", password_hash=_HASH, role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile 的作用", difficulty=3,
                       primary_point_id=1, origin="seed", visibility="public"))
        s.commit()
    return path


def _quota_line(remaining: int) -> str:
    """首页那一行**带 `<strong>`** 的额度显示（上限从常量算 —— 决策 71）。"""
    return f"还剩 <strong>{remaining} / {account.DAILY_UNITS}</strong> 点"


def _left(remaining: int) -> str:
    """面试页与错误页那一行**不带标签**的额度显示（同一个上限，两种渲染）。"""
    return f"还剩 {remaining} / {account.DAILY_UNITS} 点"

@pytest.fixture
def app(db: Path):
    return create_app(Settings(database_path=db))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "q@local", "password": PASSWORD})
        yield c


def _spend(db: Path, units: int) -> None:
    """把这名用户今天的额度用掉 `units` 点（直接记账，不走 LLM）。"""
    with create_session_factory(create_db_engine(db))() as s:
        account_repository.add_usage(s, 2, day=account.today(), units=units)
        s.commit()


# ---------------------------------------------------------------------------
# 没耗尽时：正常的一面
# ---------------------------------------------------------------------------
def test_home_shows_the_remaining_points(client: TestClient) -> None:
    body = client.get("/").text
    assert _quota_line(account.DAILY_UNITS) in body, "一分没花时应当显示满额"
    assert "开始模拟面试（6 点）" in body
    assert "纯题库模式" not in body


def test_bank_detail_shows_both_buttons_when_rich(client: TestClient) -> None:
    body = client.get("/bank/1").text
    assert "单题追问（1 点）" in body and "模拟面试（6 点）" in body


def test_partial_quota_still_offers_the_cheap_action(client: TestClient, db: Path) -> None:
    """还剩 3 点：单题追问还能开，模拟面试开不起 —— 页面上只摆那个能用的按钮。

    ⚠️ 这条是"余量不为 0 就不是纯题库模式"的落点：如果实现把降级判据写成
    `remaining == 0`，这里会既没有按钮也没有解释（用户不知道发生了什么）。
    """
    _spend(db, account.DAILY_UNITS - 3)  # 只剩 3 点：够追问、不够面试
    body = client.get("/bank/1").text
    assert "单题追问（1 点）" in body
    assert "模拟面试（6 点）" not in body, "开不起的按钮不该摆出来"
    assert "纯题库模式" not in body, "还剩 3 点不算只剩题库"
    assert _left(3) in body


# ---------------------------------------------------------------------------
# 耗尽后：降级，不是墙
# ---------------------------------------------------------------------------
def test_home_degrades_to_library_mode(client: TestClient, db: Path) -> None:
    _spend(db, account.DAILY_UNITS)
    body = client.get("/").text
    assert "纯题库模式" in body
    assert "题库" in body and "/bank" in body
    assert "开始模拟面试（6 点）" not in body, "按下去必然失败的按钮不该还在"


def test_bank_list_says_library_mode(client: TestClient, db: Path) -> None:
    _spend(db, account.DAILY_UNITS)
    body = client.get("/bank").text
    assert "纯题库模式" in body
    assert "说说 volatile 的作用" in body, "题目照旧能看"


def test_bank_detail_still_readable_when_exhausted(client: TestClient, db: Path) -> None:
    """**决策 13 的实质**：耗尽只关掉面试官，不关掉题库。"""
    _spend(db, account.DAILY_UNITS)
    r = client.get("/bank/1")
    assert r.status_code == 200
    body = r.text
    assert "说说 volatile 的作用" in body
    assert "可见性" in body, "考察点照看"
    assert "纯题库模式" in body
    assert "单题追问（1 点）" not in body


def test_direct_start_post_explains_instead_of_walling_off(client: TestClient, db: Path) -> None:
    """直接 POST（另一个标签页里额度被用光、或手写请求）也要得到解释。

    它必须**不是**泛泛的错误页：页面上要写出"题库还在"与明天恢复。
    """
    _spend(db, account.DAILY_UNITS)
    r = client.post("/interview/start", data={"mode": "drill", "question_id": "1"})
    assert r.status_code == 400
    body = r.text
    assert "面试官今天歇了" in body
    assert "题库整个都还在" in body
    assert _left(0) in body
    assert "/bank" in body


def test_exhausted_user_leaves_no_half_built_interview(client: TestClient, db: Path) -> None:
    """**扣额度失败不能留下半场面试** —— 否则会话列表里会多一条永远问不完的记录。

    （额度是在 `start_drill` 里先扣后建的；这条断言钉住那个顺序。）
    """
    from sqlalchemy import select

    from app.db.models import Interview, Session_

    _spend(db, account.DAILY_UNITS)
    client.post("/interview/start", data={"mode": "drill", "question_id": "1"})

    with create_session_factory(create_db_engine(db))() as s:
        assert s.execute(select(Interview)).first() is None
        assert s.execute(select(Session_)).first() is None


# ---------------------------------------------------------------------------
# 非额度类失败仍然是原来那一页
# ---------------------------------------------------------------------------
def test_other_failures_do_not_claim_a_quota_problem(client: TestClient) -> None:
    """「没选题目」不是额度问题 —— 两页的文案不能混，否则用户会以为明天再来就好了。"""
    r = client.post("/interview/start", data={"mode": "drill"})
    assert r.status_code == 400
    assert "这场面试没能开始" in r.text
    assert "请选择一道题" in r.text
    assert "面试官今天歇了" not in r.text
    assert "纯题库模式" not in r.text


def test_exhausted_state_is_per_user(client: TestClient, db: Path) -> None:
    """额度是**每人每天**的 —— 一个人用完不该把别人也关了。"""
    _spend(db, account.DAILY_UNITS)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(User(id=5, email="m@local", username="m", password_hash=_HASH, role="user"))
        s.commit()
    with TestClient(client.app) as other:
        other.post("/login", data={"email": "m@local", "password": PASSWORD})
        assert "开始模拟面试（6 点）" in other.get("/").text


def test_quota_state_reads_the_ledger_not_the_calendar(db: Path) -> None:
    """额度按**当天**记账：昨天的用量不该出现在今天的余量里。

    这条不测日历，测的是"余量确实是从账本算出来的"—— 把账记到别的日子上，
    余量必须还是满的（否则 `remaining_units` 会变成一句常真/常假的废话）。
    """
    with create_session_factory(create_db_engine(db))() as s:
        account_repository.add_usage(s, 2, day="2000-01-01", units=20)
        s.commit()
        assert account.quota_state(s, 2).remaining == account.DAILY_UNITS
