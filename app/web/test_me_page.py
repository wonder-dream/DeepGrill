"""注销与导出入口的测试（走真 HTTP，含三重防误触）。

这三条防误触每一条都对应一种真实的坏结果：
· 不要求确认短语 → 手滑点一下就没了
· 不要求重输口令 → 借别人没锁屏的电脑就能删掉他的数据
· 允许 owner 自删 → 删完没人能进后台（且那台机器上没有第二个 owner）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Interview, User
from app.main import create_app
from app.security import hash_password
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "me.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        s.add_all(
            [
                User(id=2, email="me@local", username="me", password_hash=_HASH, role="user"),
                User(id=3, email="owner@x.local", username="owner", password_hash=_HASH, role="owner"),
            ]
        )
        # 先落 users 再插面试：`interviews.user_id` 的外键**真的开着**，
        # 而 SQLAlchemy 的 unit of work 不保证"被引用者先插"。
        s.flush()
        s.add(Interview(id=1, user_id=2, mode="drill", status="finished", quota_charged=1))
        s.commit()
    return path


@pytest.fixture
def client(db: Path):
    with TestClient(create_app(Settings(database_path=db))) as c:
        c.post("/login", data={"email": "me@local", "password": PASSWORD})
        yield c


def test_export_downloads_a_json_file(client: TestClient) -> None:
    r = client.get("/me/export")
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    payload = r.json()
    assert payload["account"]["email"] == "me@local"
    assert len(payload["interviews"]) == 1


def test_me_page_shows_what_will_be_deleted(client: TestClient) -> None:
    body = client.get("/me").text
    assert "我的数据" in body
    assert "注销账号与数据" in body
    assert "1 场" in body or "1 道" in body


def test_delete_requires_the_confirmation_phrase(client: TestClient, db: Path) -> None:
    r = client.post("/me/delete", data={"confirm": "delete", "password": PASSWORD})
    assert r.status_code == 400
    assert "确认短语" in r.text
    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(User, 2) is not None, "没通过校验就绝不许删"


def test_delete_requires_the_password(client: TestClient, db: Path) -> None:
    r = client.post("/me/delete", data={"confirm": "DELETE", "password": "wrong-xxxx"})
    assert r.status_code == 403
    assert "口令不正确" in r.text
    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(User, 2) is not None


def test_delete_removes_the_account_and_clears_the_cookie(client: TestClient, db: Path) -> None:
    r = client.post(
        "/me/delete", data={"confirm": "DELETE", "password": PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"

    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(User, 2) is None, "身份要真的删掉"
        assert s.execute(select(Interview).where(Interview.user_id == 2)).first() is None
    # 之后的请求应当回到未登录状态
    assert client.get("/me", follow_redirects=False).status_code == 302


def test_owner_cannot_delete_itself(client: TestClient, db: Path) -> None:
    """owner 自删会让后台再也进不去 —— 那台机器上没有第二个 owner。"""
    client.post("/logout")
    client.post("/login", data={"email": "owner@x.local", "password": PASSWORD})

    r = client.post("/me/delete", data={"confirm": "DELETE", "password": PASSWORD})
    assert r.status_code == 403
    assert "owner" in r.text
    with create_session_factory(create_db_engine(db))() as s:
        assert s.get(User, 3) is not None


def test_export_requires_login(client: TestClient) -> None:
    client.post("/logout")
    assert client.get("/me/export", follow_redirects=False).status_code == 302


# ---------------------------------------------------------------------------
# 简历页（决策 8）
# ---------------------------------------------------------------------------
PROFILE = {
    "headline": "后端，3 年",
    "years": "3 年",
    "skills": ["Redis"],
    "projects": [
        {"name": "优惠券系统", "role": "后端", "tech": ["Redis"],
         "highlights": ["QPS 从 800 提升到 3000"], "probe_points": ["锁超时怎么协调"]}
    ],
}
QUESTIONS = {
    "questions": [
        {"stem": "你用 Redis 分布式锁扣库存 —— 锁超时和业务耗时怎么协调？",
         "kind": "design", "difficulty": 4, "source": "优惠券系统",
         "criteria": ["说清续期", "说清误删风险"]}
    ]
}


def test_resume_page_shows_the_privacy_statement(client: TestClient) -> None:
    body = client.get("/me/resume").text
    assert "不保存简历原文" in body
    # 「只支持粘贴」这件事要**如实说明**，不能让人以为能传文件；但用哪句话说由页面定 ——
    # 文案清理那一轮把「当前只支持粘贴文本（不做 PDF/DOCX 上传）」这句删掉了（用户不需要
    # 知道我们内部为什么不做），所以这里钉的是**意图**而不是那一句原文：
    # 页面上仍是粘贴流程（标题写着「粘贴简历」），而且没有任何"上传文件"的入口。
    assert "粘贴" in body, "要如实说明这是粘贴流程，不能让人以为能传文件"
    assert 'type="file"' not in body, "没有上传入口，就别在文案上留下会被误解的余地"


def test_resume_submit_creates_private_questions(client: TestClient, db: Path) -> None:
    from app.deps import get_llm
    from tests.fakes import FakeLLM, FakeReply

    fake = FakeLLM().queue(FakeReply(data=PROFILE), FakeReply(data=QUESTIONS))
    client.app.dependency_overrides[get_llm] = lambda: fake

    r = client.post("/me/resume", data={"resume_text": "张三，后端 3 年。" * 10, "note": "我的"})
    assert r.status_code == 200
    assert "新建私有题" in r.text
    assert "1 道" in r.text
    assert "优惠券系统" in r.text, "档案要显示出来"

    with create_session_factory(create_db_engine(db))() as s:
        from app.db.models import CandidateProfile, Question

        assert s.execute(select(Question).where(Question.owner_user_id == 2)).scalars().all()
        prof = s.execute(select(CandidateProfile)).scalars().one()
        assert prof.structured["headline"] == "后端，3 年"


def test_resume_submit_reports_model_failure(client: TestClient) -> None:
    from app.deps import get_llm
    from app.llm import LLMCallError
    from tests.fakes import FakeLLM, FakeReply

    client.app.dependency_overrides[get_llm] = lambda: FakeLLM().queue(
        FakeReply(error=LLMCallError("模型挂了"))
    )
    r = client.post("/me/resume", data={"resume_text": "张三，后端 3 年。" * 10})
    assert r.status_code == 502
    assert "解析没成功" in r.text


def test_resume_submit_refuses_too_short_input(client: TestClient) -> None:
    """太短的多半是误操作 —— 明确拒绝，且**不调模型**。"""
    from app.deps import get_llm
    from tests.fakes import FakeLLM

    fake = FakeLLM()
    client.app.dependency_overrides[get_llm] = lambda: fake
    r = client.post("/me/resume", data={"resume_text": "太短"})
    assert r.status_code == 400
    assert "太短" in r.text
    assert fake.calls == []


# ---------------------------------------------------------------------------
# 面试记录页（决策 96）
# ---------------------------------------------------------------------------
def _add_interviews(db: Path, *, user_id: int, ids: list[int], **fields: object) -> None:
    with create_session_factory(create_db_engine(db))() as s:
        for i in ids:
            row: dict[str, object] = {"mode": "drill", "status": "finished", "quota_charged": 1}
            row.update(fields)
            s.add(Interview(id=i, user_id=user_id, **row))
        s.commit()


def test_interviews_page_shows_every_record_not_just_the_recent_ten(
    client: TestClient, db: Path
) -> None:
    """`/me` 的摘要只摆最近 10 条，记录页要**全部** —— 这正是它存在的理由。

    夹具里已经有一条 `id=1`；再补 12 条，于是最新的 10 条（31…22）在 `/me` 上，
    而 `id=1` 只在记录页上。**别人的面试一条都不能出现**（同一个页面最容易漏的
    就是 owner_user_id 过滤那一类事）。
    """
    _add_interviews(db, user_id=2, ids=list(range(20, 32)), report_body="一份报告")
    _add_interviews(db, user_id=3, ids=[99], report_body="别人的报告")
    with create_session_factory(create_db_engine(db))() as s:
        # 夹具里那条最老的面试原本没有报告正文 —— 给它一份，好断言"它也在记录页上"
        s.get(Interview, 1).report_body = "一份报告"
        s.commit()

    log = client.get("/me/interviews").text
    assert "共 13 场" in log
    assert 'href="/report/1"' in log, "最老的那条也必须在（/me 的摘要里它已经看不到了）"
    assert 'href="/report/31"' in log
    assert 'href="/report/99"' not in log, "看到了别人的面试记录"

    assert 'href="/report/1"' not in client.get("/me").text, "摘要本来就只给最近 10 条"


def test_interviews_page_counts_abandoned_too(client: TestClient, db: Path) -> None:
    """放弃掉的（`abandoned`）也要看得见 —— 否则用户会以为那场面试凭空消失了。

    判据是"我面过哪些"，不是"我面完哪些"：要"继续哪一场"是另一件事，走 `resume_target`。
    """
    _add_interviews(db, user_id=2, ids=[50], status="abandoned", quota_charged=6)
    assert "abandoned" in client.get("/me/interviews").text
    assert "共 2 场" in client.get("/me/interviews").text


def test_interviews_page_requires_login(db: Path) -> None:
    with TestClient(create_app(Settings(database_path=db))) as c:
        r = c.get("/me/interviews", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_me_page_links_to_the_full_interview_log(client: TestClient) -> None:
    assert 'href="/me/interviews"' in client.get("/me").text
