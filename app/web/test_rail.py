"""侧边栏与状态栏（决策 96）—— 一级入口、动态项、以及"什么时候不该出现"。

这三件事领域测试都看不见，而它们各自的失效方式都是**静默**的：

· 一级入口漏一个用户不会报错，只会觉得"没地方可去"；
· 「继续面试」指到一个已经答完的会话 = 点进去挨一句"这道题已经答完了"；
· 状态栏的额度点如果取自别处（比如写死 `DAILY_UNITS`），额度耗尽时它会撒谎。

`rail` 那份快照由 `deps.get_current_user` 产生、经 `render()` 注入模板 —— 所以这里
断言的是**渲染出来的 HTML**，不是某个函数返回值。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Interview, Question, Session_, User
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


def _seed(path: Path) -> None:
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="rail@local", username="rail", password_hash=_HASH, role="user"))
        s.add(Question(id=1, kind="knowledge", stem="公共题一", difficulty=3,
                       origin="seed", visibility="public"))
        s.flush()
        # 一场**答到一半**的面试：interviews.status=active 且有一道没答完的题会话
        s.add(Interview(id=10, user_id=2, mode="interview", status="active"))
        s.flush()
        s.add(Session_(id=100, interview_id=10, question_id=1, seq=1,
                       status="active", max_rounds=3))
        # 一场已经收尾的面试：它**不该**出现在「继续面试」里
        s.add(Interview(id=11, user_id=2, mode="drill", status="finished",
                        report_body="报告正文"))
        s.flush()
        s.add(Session_(id=101, interview_id=11, question_id=1, seq=1,
                       status="finished", max_rounds=3))
        s.commit()


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "rail.db"
    migrate(path)
    _seed(path)
    return path


@pytest.fixture
def app(db: Path):
    return create_app(Settings(database_path=db))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def member(client: TestClient):
    client.post("/login", data={"email": "rail@local", "password": PASSWORD})
    return client


# ---------------------------------------------------------------------------
# 一级入口
# ---------------------------------------------------------------------------
def test_sidebar_lists_every_first_class_entry_for_a_member(member: TestClient) -> None:
    """普通用户的侧边栏 = 题库 / 收藏 / 私有题 / 我的（决策 96）。

    它此前只有两个项，而收藏与私有题集**早就存在**、只是埋在 `/me` 里 —— 所以这条
    测的是"暴露"，不是新功能。
    """
    html = member.get("/").text
    for href in ('href="/bank"', 'href="/me/favorites"', 'href="/bank?mine=1"', 'href="/me"'):
        assert href in html, f"侧边栏少了 {href}"


def test_anonymous_sidebar_keeps_a_single_login_entry(client: TestClient) -> None:
    """未登录只给「题库」与「登录/注册」—— 收藏/私有题/我的对匿名没有意义。"""
    html = client.get("/").text
    assert 'href="/login"' in html
    assert "登录/注册" in html
    for href in ('href="/me/favorites"', 'href="/bank?mine=1"'):
        assert href not in html, f"匿名不该看到 {href}"


def test_the_bank_pill_does_not_light_up_for_the_private_set(member: TestClient) -> None:
    """`/bank?mine=1` 与 `/bank` 是两个入口 —— 只用 startswith 会让两个同时高亮。"""
    private = member.get("/bank?mine=1").text
    assert private.count('class="pill active"') == 1, "同一时刻只该有一个入口是高亮的"
    public = member.get("/bank").text
    assert public.count('class="pill active"') == 1


# ---------------------------------------------------------------------------
# 「继续面试」：只在真有未完成的会话时出现
# ---------------------------------------------------------------------------
def test_resume_entry_points_at_the_unfinished_session(member: TestClient) -> None:
    html = member.get("/").text
    assert 'href="/interview/100"' in html, "没有指向那道没答完的题会话"
    assert "继续面试" in html


def test_an_interview_without_a_pending_session_is_not_resumable(member: TestClient, db: Path) -> None:
    """把所有题会话都收尾 → 入口消失。

    这是这条动态项唯一会咬人的地方：`interviews.status` 还是 `active`，但题都答完了，
    于是"继续"会跳进一页立刻告诉你"这道题已经答完了"。
    """
    with create_session_factory(create_db_engine(db))() as s:
        s.get(Session_, 100).status = "finished"
        s.commit()

    html = member.get("/").text
    assert 'href="/interview/100"' not in html
    assert "继续面试" not in html


def test_resume_entry_is_absent_for_anonymous(client: TestClient) -> None:
    assert "继续面试" not in client.get("/").text


# ---------------------------------------------------------------------------
# 状态栏的今日额度点
# ---------------------------------------------------------------------------
def test_statusbar_shows_today_quota(member: TestClient) -> None:
    from app.account import service as account

    html = member.get("/").text
    assert f"今日额度 <span class=\"ok\">{account.DAILY_UNITS}/{account.DAILY_UNITS}</span>" in html


def test_statusbar_says_library_only_when_the_quota_is_gone(member: TestClient, db: Path) -> None:
    """额度耗尽 = 降级成纯题库模式（决策 13）。

    状态栏必须**把降级说出来**，否则用户看到的是"还剩 0 点"却不知道发生了什么 ——
    而"降级可以，静默不行"是 §3.1。
    """
    from app.account import service as account

    with create_session_factory(create_db_engine(db))() as s:
        # 花光 32 点：5 场模拟面试（6 点）+ 2 次单题追问（1 点）
        for _ in range(5):
            account.spend_units(s, user_id=2, mode="interview")
        for _ in range(2):
            account.spend_units(s, user_id=2, mode="drill")
        s.commit()
        assert account.quota_state(s, 2).library_only

    html = member.get("/").text
    assert f'今日额度 <span class="ok">0/{account.DAILY_UNITS}</span>' in html
    assert "纯题库" in html


def test_anonymous_statusbar_has_no_quota(client: TestClient) -> None:
    assert "今日额度" not in client.get("/").text
