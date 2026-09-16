"""首页 = 内容推荐中心（决策 3）的测试。

基线的原话：**开始模拟面试（主按钮）+ 今日推荐题 + 我的私有题集 + 继续未完成会话**，
而且 **「今日推荐题」是即时计算，不是每日锁定的题单**。

这两句话决定了测什么：

· 推荐**跟着掌握度走** —— 薄弱点换了，推荐的题要跟着换（库里不留下"今日题单"）
· 推荐**不摆答过的题** —— 刚答过的题再推一遍是浪费用户的注意力
· 推荐**不越可见性** —— 挂在同一个知识点下、但属于别人的私有题不能出现
· **没测出薄弱点时要自认兜底** —— 摆一个看起来很像推荐的列表出来更糟
· **"继续未完成"只列真的还没收尾的** —— `finished` / `abandoned` 都不算；
  连"面试还 active 但题都收尾了"也要跳过（放一个不知跳去哪的链接比不列更糟）
· 首页是**装配层**：规则都在各领域里，所以这里只测"拼出来的东西对不对"

数据直接造行（不调 LLM）：这一页的输入是"库里有什么"，不是"模型说了什么"。

夹具里的四道题是刻意摆的：
`volatile 的问题`(公共/知识点1) · `线程池的问题`(公共/知识点2) ·
`别人的私有题`(3 号私有/知识点1) · `volatile 进阶题`(公共/知识点1)
—— 于是"薄弱点 1 → 推进阶题"和"薄弱点 2 → 推线程池那道"都能被区分出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Attempt,
    Criterion,
    Domain,
    Interview,
    KnowledgePoint,
    Question,
    User,
)
from app.db.models import (
    Session_ as InterviewSession,
)
from app.interview import rules
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "home.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="h@local", username="h", password_hash=_HASH, role="user"))
        s.add(User(id=3, email="other@local", username="other", password_hash=_HASH, role="user"))
        s.flush()
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.add(KnowledgePoint(id=2, domain_id=1, name="线程池", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Criterion(id=2, point_id=2, seq=1, text="拒绝策略", shared=0))
        s.flush()
        for qid, stem, point_id, owner in (
            (1, "volatile 的问题", 1, None),
            (2, "线程池的问题", 2, None),
            (3, "别人的私有题", 1, 3),
            (4, "volatile 进阶题", 1, None),
        ):
            s.add(Question(id=qid, kind="knowledge", stem=stem, difficulty=3,
                           primary_point_id=point_id, origin="generated" if owner else "seed",
                           owner_user_id=owner,
                           visibility="private" if owner else "public"))
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
        c.post("/login", data={"email": "h@local", "password": PASSWORD})
        yield c


def _answer(
    db: Path,
    *,
    question_id: int,
    hits: dict[int, str],
    status: str = "active",
    interview_id: int = 1,
) -> int:
    """造一场"答过一轮"的面试（直接落行）。返回题会话 id。

    `hits` = `{criterion_id: 状态}` —— 掌握度矩阵就是从这里推出来的。
    """
    with create_session_factory(create_db_engine(db))() as s:
        s.add(Interview(id=interview_id, user_id=2, mode="drill", status=status))
        s.flush()
        ts = InterviewSession(id=interview_id, interview_id=interview_id,
                              question_id=question_id, seq=1, status="active", max_rounds=3)
        s.add(ts)
        s.flush()
        s.add(Attempt(session_id=ts.id, round_no=1, is_followup=0, input_mode="text",
                      answer_text="答了", feedback_text="继续",
                      hits={str(k): v for k, v in hits.items()}))
        s.commit()
        return ts.id


def test_home_refuses_to_offer_a_second_interview(client: TestClient, db: Path) -> None:
    """一场没结束就**不给"开始"按钮**，而是给"继续 / 放弃"两条路（决策 97）。

    首页是主要入口，所以这条规则在页面上必须是**看得见的**，而不是等到用户点了
    开始、撞上一堵错误页才知道。
    """
    ts_id = _answer(db, question_id=1, hits={1: "命中"})

    html = client.get("/").text
    assert "开始模拟面试" not in html, "还有没结束的面试时不该再出现开始按钮"
    assert f'href="/interview/{ts_id}"' in html, "没有指出未结束的那一场在哪"
    assert 'action="/interview/1/abandon"' in html
    assert "额度点不退还" in html, "放弃的代价要写在按钮上面"


def test_home_offers_start_again_after_abandoning(client: TestClient, db: Path) -> None:
    _answer(db, question_id=1, hits={1: "命中"})

    r = client.post("/interview/1/abandon", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert "开始模拟面试" in client.get("/").text


# ---------------------------------------------------------------------------
# 推荐跟着掌握度走
# ---------------------------------------------------------------------------
def test_missed_point_drives_the_recommendation(client: TestClient, db: Path) -> None:
    """在 volatile 上答错 → 推荐 volatile 那一道（答过的排除、进阶题补上）。"""
    _answer(db, question_id=1, hits={1: rules.MISS})

    body = client.get("/").text
    assert "今日推荐题" in body
    assert "volatile 进阶题" in body
    assert "线程池的问题" not in body, "推荐要跟着薄弱点走"


def test_answered_question_is_not_recommended_again(client: TestClient, db: Path) -> None:
    """**答过的那一道要排除** —— 刚答完再推一遍是浪费用户注意力。"""
    _answer(db, question_id=4, hits={1: rules.MISS})  # 答的是进阶题，薄弱点仍是 volatile

    body = client.get("/").text
    assert "volatile 的问题" in body, "同知识点的另一道该被推出来"
    assert "volatile 进阶题" not in body, "刚答过的那道不该再出现"


def test_hit_point_is_not_weak_so_the_list_falls_back(client: TestClient, db: Path) -> None:
    """全答对 → 没有薄弱点 → 走兜底，而且**页面上要说清楚这是兜底**。"""
    _answer(db, question_id=1, hits={1: rules.HIT})

    body = client.get("/").text
    assert "没测出薄弱点" in body
    assert "volatile 进阶题" in body, "兜底摆的是最新的题"


def test_recommendation_never_shows_someone_elses_private_question(
    client: TestClient, db: Path
) -> None:
    """推荐也要过可见性：练到某个知识点只剩"别人的私有题"时，宁可不推。

    做法是答掉公共的两道、把公共题从知识点上摘掉，于是挂在同一知识点下的只剩
    3 号那道私有题 —— 它绝不能出现在推荐里。
    """
    _answer(db, question_id=1, hits={1: rules.MISS})
    with create_session_factory(create_db_engine(db))() as s:
        for qid in (1, 4):
            s.get(Question, qid).primary_point_id = None
        s.commit()

    body = client.get("/").text
    assert "别人的私有题" not in body
    assert "题库里还没有可推荐的题" in body


def test_recommendation_is_recomputed_not_frozen(client: TestClient, db: Path) -> None:
    """**即时计算**：把薄弱点从 volatile 换到线程池，推荐立刻跟着换。

    这条钉的是"没有 `user_picks` / `selected_at` 那样的每日固化"（基线里点明了）：
    换成别的推荐算法时不该留下数据残骸，所以换完输入就换输出。
    """
    _answer(db, question_id=1, hits={1: rules.MISS})
    assert "volatile 进阶题" in client.get("/").text

    # 再答两轮：volatile 命中了，线程池没命中 —— 薄弱点换成了线程池
    with create_session_factory(create_db_engine(db))() as s:
        s.add(Attempt(session_id=1, round_no=2, is_followup=1, input_mode="text",
                      answer_text="答对了", feedback_text="好", hits={"1": rules.HIT}))
        s.add(Attempt(session_id=1, round_no=3, is_followup=0, input_mode="text",
                      answer_text="答错", feedback_text="继续", hits={"2": rules.MISS}))
        s.commit()

    body = client.get("/").text
    assert "线程池的问题" in body, "薄弱点换了，推荐要跟着换"
    assert "volatile 进阶题" not in body


# ---------------------------------------------------------------------------
# 继续未完成会话
# ---------------------------------------------------------------------------
def test_active_interview_is_listed(client: TestClient, db: Path) -> None:
    ts_id = _answer(db, question_id=1, hits={1: rules.MISS}, status="active")
    body = client.get("/").text
    assert "继续未完成" in body
    assert f'href="/interview/{ts_id}"' in body


def test_finished_and_abandoned_are_not_listed(client: TestClient, db: Path) -> None:
    """出了报告的、以及用户自己放弃的，都不该出现在"继续"里。"""
    for index, status in enumerate(("finished", "abandoned"), start=1):
        _answer(db, question_id=1, hits={1: rules.MISS}, status=status, interview_id=index)
        assert "没有答到一半的面试" in client.get("/").text, status


def test_active_interview_without_a_pending_session_is_skipped(
    client: TestClient, db: Path
) -> None:
    """面试还 active、但所有题会话都收尾了 —— 没有可跳转的目标，就不列。

    （列表里放一个点进去不知道去哪的链接，比不列出来更糟。）
    """
    _answer(db, question_id=1, hits={1: rules.MISS})
    with create_session_factory(create_db_engine(db))() as s:
        s.get(InterviewSession, 1).status = "finished"
        s.commit()
    assert "没有答到一半的面试" in client.get("/").text


# ---------------------------------------------------------------------------
# 我的私有题集与收藏
# ---------------------------------------------------------------------------
def test_private_set_entry_shows_only_my_questions(client: TestClient, db: Path) -> None:
    with create_session_factory(create_db_engine(db))() as s:
        # 注意 `kind` 只有 knowledge / design 两种（`0001` 的 CHECK + 决策 20）：
        # 私有题集里的项目深挖题走 design，不存在第三个 kind。
        s.add(Question(id=5, kind="design", stem="我的项目题", difficulty=3,
                       origin="generated", owner_user_id=2, visibility="private"))
        s.commit()

    body = client.get("/bank?mine=1").text
    assert "我的私有题集" in body
    assert "我的项目题" in body
    assert "volatile 的问题" not in body, "公共题不属于「我的私有题集」"
    assert "别人的私有题" not in body


def test_my_private_set_is_empty_for_a_new_user(client: TestClient, db: Path) -> None:
    assert "共 0 道" in client.get("/bank?mine=1").text


def test_home_shows_private_and_favorite_counts(client: TestClient, db: Path) -> None:
    client.post("/bank/1/favorite")
    body = client.get("/").text
    assert "我的私有题集与收藏" in body
    assert "收藏</dt><dd>1 道" in body
    assert "/me/favorites" in body


# ---------------------------------------------------------------------------
# 匿名与空库
# ---------------------------------------------------------------------------
def test_anonymous_home_has_no_member_sections(app) -> None:
    with TestClient(app) as c:
        body = c.get("/").text
        assert "今日推荐题" not in body
        assert "继续未完成" not in body
        assert "今日额度" not in body
        assert "从题库挑一道题开始" in body


def test_home_survives_a_database_without_migrations(tmp_dir: Path) -> None:
    """没跑迁移的库也要能打开首页（它是用户看到的第一个页面）—— 页面上给那条命令。"""
    app = create_app(Settings(database_path=tmp_dir / "empty.db"))
    with TestClient(app) as c:
        body = c.get("/").text
        assert "未初始化" in body
        assert "python -m migrations.run" in body
