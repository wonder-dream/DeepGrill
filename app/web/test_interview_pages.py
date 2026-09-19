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
from app.db.models import (
    Attempt,
    Criterion,
    Domain,
    Interview,
    KnowledgePoint,
    Question,
    User,
)
from app.db.models import Session_ as InterviewSession
from app.deps import get_llm
from app.main import create_app
from app.security import hash_password
from app.web.interview_page import _questions_for_mock
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

    # 答题页：题干、知识点在；考察点不再展示给考生（避免照着点答）
    body = client.get(location).text
    assert "说说 volatile 的作用" in body
    assert "volatile" in body
    assert "底层内存屏障" not in body

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
    # 判定依据印的是**考察点正文**，不是内部主键（第一版印的是"考察点 1 = 命中"）
    assert "底层内存屏障 = " in report
    assert "可见性与有序性 = " in report
    assert "考察点 1" not in report, "criterion_id 是内部主键，不该上页面"

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


# ---------------------------------------------------------------------------
# 一场没结束，不许开第二场（决策 97）
# ---------------------------------------------------------------------------
def test_a_second_start_is_refused_with_a_way_out(client: TestClient) -> None:
    """被挡下时页面必须指出**是哪一场**，并给出"继续 / 放弃"两条路。

    只摆一句"你还有一场面试没结束"等于把用户晾在墙前面 —— 而"放弃"正是这次
    一起补上的那条路（决策 97）。放弃的代价（额度点不退）也必须写在按钮上面。
    """
    _login(client)
    _start_drill(client)

    r = client.post("/interview/start", data={"mode": "drill", "question_id": "1"})
    assert r.status_code == 400
    assert "还有一场面试没结束" in r.text
    assert 'action="/interview/1/abandon"' in r.text
    assert "已答 0 轮" in r.text
    assert "退还 <strong>1</strong> 点" in r.text, "单题追问 1 点，一轮没答应当全额退"


def test_abandoning_lets_a_new_one_start(client: TestClient) -> None:
    _login(client)
    _start_drill(client)

    r = client.post("/interview/1/abandon", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert client.post(
        "/interview/start", data={"mode": "drill", "question_id": "1"}, follow_redirects=False
    ).status_code == 302, "放弃之后应该能开新的一场"


def test_abandoned_interview_page_says_so_instead_of_showing_the_form(client: TestClient) -> None:
    """放弃之后旧标签页/历史记录里的链接还会被点开 —— 不能再把答题界面摆出来。

    否则表单照样能提交，用户以为自己还能答，实际只会撞一句"已经放弃了"。
    """
    _login(client)
    location = _start_drill(client)
    client.post("/interview/1/abandon")

    r = client.get(location)
    assert r.status_code == 409
    assert "这场面试已经放弃" in r.text
    assert 'name="answer_text"' not in r.text, "放弃之后的页面上不该还有答题框"


def test_abandoning_is_idempotent_from_the_page(client: TestClient) -> None:
    """双击"放弃"、后退重放，都该回到首页，而不是一堵错误页。"""
    _login(client)
    _start_drill(client)
    assert client.post("/interview/1/abandon", follow_redirects=False).status_code == 302
    assert client.post("/interview/1/abandon", follow_redirects=False).status_code == 302


def test_exit_link_only_leaves_without_touching_this_interview(app, client: TestClient) -> None:
    """「退出」只是离开这一页（决策 99）—— 它不写库、不改状态。

    它与「放弃本场」是**两个不同的动作**：退出之后这一场还活着（回首页点「继续未完成」
    接着答），放弃才是结束这一场。用 `unfollowed`（不开 follow_redirects）走这条链接，
    断言那之后**还能继续作答** —— 这正是"没被标成 abandoned"的行为证据。
    """
    with create_session_factory(create_db_engine(app.state.test_db))() as s:
        s.add(Interview(id=10, user_id=2, mode="drill", status="active"))
        s.add(InterviewSession(id=1, interview_id=10, question_id=1, seq=1,
                               status="active", max_rounds=3))
        s.commit()

    _login(client)
    html = client.get("/interview/1").text
    assert '<a class="pill" href="/"' in html, "退出必须是纯链接（POST /abandon 是另一个动作）"
    assert 'action="/interview/10/abandon"' in html, "放弃本场那个表单要还在（它才是结束这一场）"

    exited = client.get("/", follow_redirects=False)
    assert exited.status_code == 200, "退出 = 打开首页，不是一个跳转"

    with create_session_factory(create_db_engine(app.state.test_db))() as s:
        assert s.get(Interview, 10).status == "active", "退出不许把这一场标成放弃"
        assert s.get(Interview, 10).ended_at is None

    # 这一场还能接着答：退出之后回到同一个地址，表格与作答框照旧
    again = client.get("/interview/1")
    assert again.status_code == 200
    assert 'name="answer_text"' in again.text


def test_exam_pages_are_not_cacheable(app, client: TestClient) -> None:
    """答题页与"这场已经放弃"那一页都必须是 `no-store`。

    否则浏览器把整页放进 back/forward 缓存：**放弃之后按返回键，会从缓存里复原
    那个看起来还能答的界面**（实测就是这个现象）。标上 `no-store` 之后返回键变成
    一次真请求 —— 那时服务端渲染的是真实状态（已放弃 ⇒ 409 那一页）。
    """
    _login(client)
    location = _start_drill(client)

    live = client.get(location)
    assert live.status_code == 200
    assert live.headers["cache-control"] == "no-store"

    client.post("/interview/1/abandon")
    abandoned = client.get(location)
    assert abandoned.status_code == 409
    assert abandoned.headers["cache-control"] == "no-store", "409 那一页也不能被缓存"


def test_exam_bar_actions_are_plain_labelled_pills(app, client: TestClient) -> None:
    """考场条那两个出口的结构要对（它们不是导航栏那种 pill）。

    导航栏的 pill 靠一个**收起态才显形的单字图标**（`.g`，平时 `visibility: hidden`
    但仍占位）撑着"展开/收起"两种形态；考场条是常显文字，带着那个占位符就会把可见
    文字往右推（用户报的"没居中"）。所以这里钉的是：**两个出口各自只有一个 `<span
    class="lbl">` 文字**，不再有 `.g`。CSS 那侧（`.exambar-actions .pill` 的
    `justify-content: center` + `gap: 0`）只负责短粗胶囊的样子。
    """
    _login(client)
    _start_drill(client)
    html = client.get("/interview/1").text

    bar = html.split('class="exambar-actions"', 1)[1].split("</header>", 1)[0]
    assert 'class="lbl"' in bar
    assert 'class="g"' not in bar, "考场条的按钮不该再带那个占位图标"
    assert "退出" in bar and "放弃本场" in bar
    assert 'href="/"' in bar and "/abandon" in bar


def test_abandon_button_targets_the_interview_not_the_session(app, client: TestClient) -> None:
    """⚠️ 面试 id 与题会话 id 是两张表的 id，**不保证相等**。

    隔离库（第一个用户的第一场面试）里两者都是 1，所以"表单指向了题会话 id"
    这个错误在那个夹具下看不出来 —— 一旦用了一阵子，点「放弃本场」就会 404
    （实测：`/interview/10/abandon` 而真正的面试 id 是 3）。
    """
    with create_session_factory(create_db_engine(app.state.test_db))() as s:
        s.add(Interview(id=10, user_id=2, mode="drill", status="active"))
        s.add(InterviewSession(id=1, interview_id=10, question_id=1, seq=1,
                               status="active", max_rounds=3))
        s.commit()

    _login(client)
    html = client.get("/interview/1").text
    assert 'action="/interview/10/abandon"' in html, "放弃表单要提交**面试** id（路由收的是它）"
    assert 'action="/interview/1/abandon"' not in html, "题会话 id 提交给放弃路由 ⇒ 404"

    r = client.post("/interview/10/abandon", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"


# ---------------------------------------------------------------------------
# 模拟面试抽哪几道题（决策 100：薄弱点优先 + 排除答过的）
# ---------------------------------------------------------------------------
@pytest.fixture
def pick_db(tmp_dir: Path) -> Path:
    """题库：公共题 q1-q8（q4 缺号留白），再加一道**别人的**私有题 q9。

    挂载刻意错开，好让两种情形分得开：
    · **volatile（知识点 1）**：q1、q3、q4、q7、q8
    · **线程池（知识点 2）**：q2、q5、q6

    于是"哪个题更新"与"哪道题在薄弱点下"不重合 —— 断言顺序时才分得清它是按
    薄弱点排的，还是又退回去按 id 倒序了（用户报的就是后者）。
    """
    path = tmp_dir / "pick.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add(User(id=2, email="pick@local", username="pick", password_hash=_PASSWORD_HASH,
                   role="user"))
        s.add(User(id=3, email="other@local", username="other", password_hash=_PASSWORD_HASH,
                   role="user"))
        s.flush()
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.add(KnowledgePoint(id=2, domain_id=1, name="线程池", status="confirmed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="可见性", shared=0))
        s.add(Criterion(id=2, point_id=2, seq=1, text="拒绝策略", shared=0))
        s.flush()
        layout = {1: 1, 2: 2, 3: 1, 4: 1, 5: 2, 6: 2, 7: 1, 8: 1, 9: 1}
        for qid, point_id in layout.items():
            s.add(Question(id=qid, kind="knowledge", stem=f"第 {qid} 题", difficulty=3,
                           primary_point_id=point_id, origin="seed",
                           owner_user_id=3 if qid == 9 else None,
                           visibility="private" if qid == 9 else "public"))
        s.commit()
    return path


def _play_once(db: Path, *, question_id: int, hits: dict[int, str]) -> None:
    """造"这个人答过一轮"的记录（直接落行）—— 掌握度就是从这里推出来的。

    注意**只加 attempt、不算"答过的题"之外的东西**：`answered_question_ids()` 数的是
    "回答过的题"，而决策 100 要排除的正是这种题（只被排进过计划、一轮没答的不算）。
    id 由题目 id 推出来（同一场测试里一道题只会造一次），免得一行行去数。
    """
    ref = 900 + question_id
    with create_session_factory(create_db_engine(db))() as s:
        s.add(Interview(id=ref, user_id=2, mode="drill", status="finished"))
        s.flush()
        s.add(InterviewSession(id=ref, interview_id=ref, question_id=question_id, seq=1,
                               status="active", max_rounds=3))
        s.flush()
        s.add(Attempt(session_id=ref, round_no=1, is_followup=0, input_mode="text",
                      answer_text="答了", feedback_text="继续",
                      hits={str(k): v for k, v in hits.items()}))
        s.commit()


def _pick(db: Path) -> list[int]:
    with create_session_factory(create_db_engine(db))() as s:
        return _questions_for_mock(s, user_id=2)


def test_a_mock_prefers_the_weak_point_over_the_newest(pick_db: Path) -> None:
    """薄弱点下还有没答过的题时，**它排在前面**（决策 100 的第一段）。

    线程池是薄弱点（q2 答错），同一知识点下还有没答过的 q6 / q5 ⇒ 它们必须排在
    最前面；其余从"全库没答过"里按 id 倒序补齐。若顺序变成纯 id 倒序（8,7,6,5,…），
    说明它根本没看掌握度 —— 那正是用户报的坏形态。
    """
    _play_once(pick_db, question_id=2, hits={2: "未命中"})
    picked = _pick(pick_db)
    assert picked[:2] == [6, 5], "薄弱点下没答过的题应当排在最前面"
    assert 2 not in picked, "答过的那道不再出现"
    assert picked == [6, 5, 8, 7, 4, 3, 1], "剩下的按 id 倒序补齐"


def test_a_mock_skips_questions_the_user_already_answered(pick_db: Path) -> None:
    """答过的题不再出现在新一场里（决策 100 的 A）。

    这也解释了轮换：**答过之后，下一场才会换题**。只被排进计划、一轮都没答的
    （放弃的那几场）不算"答过" —— 这也是用户那四场为什么没让它换题。
    """
    _play_once(pick_db, question_id=3, hits={2: "命中"})
    assert 3 not in _pick(pick_db)


def test_a_mock_still_starts_when_everything_was_answered(pick_db: Path) -> None:
    """全都答过时**照样要能开场**（决策 100 的第三段，每场 8 道）。

    抽不出来就报错等于把"练得多"变成了惩罚。注意别人的私有题（q9）任何一段都不许出现。
    """
    for qid in (1, 2, 3, 4, 5, 6, 7, 8):
        _play_once(pick_db, question_id=qid, hits={})
    picked = _pick(pick_db)
    assert picked == [8, 7, 6, 5, 4, 3, 2, 1], "放开'没答过'这条之后退回最新八道"
    assert 9 not in picked, "别人的私有题不许被抽到"


def test_the_same_state_gives_the_same_eight(pick_db: Path) -> None:
    """**不引入随机**（决策 100）：同一份库状态永远同一套、同一顺序。

    报告要可复现、测试要能钉住 —— 随机抽题会把这两件都毁掉。
    """
    _play_once(pick_db, question_id=2, hits={2: "未命中"})
    assert _pick(pick_db) == _pick(pick_db) == [6, 5, 8, 7, 4, 3, 1]


def test_a_new_user_gets_the_newest_questions(pick_db: Path) -> None:
    """还没答过任何题的人没有薄弱点（"没考过"不等于"薄弱"）⇒ 走兜底：最新八道。"""
    assert _pick(pick_db) == [8, 7, 6, 5, 4, 3, 2, 1]
