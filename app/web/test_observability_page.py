"""观测页的测试（决策 23）。

观测页最容易退化成一个"看起来有内容、实际全是 0"的页面 —— 那种页面比没有更糟，
因为它让人以为看过了。所以这里的断言都钉在**具体数字**上：

· 待定池只数**公共**题（私有题没挂载是正常的，混进去会让这个数字失去意义）
· `mounted_ratio` 在一道公共题都没有时是 `None`，不是 0（0% 和"没有"是两件事）
· 失败任务的原因必须**显示出来**（§3.1：降级不静默 —— 记了但没人看得到等于没记）
· 只有 owner 能进
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Domain, Job, KnowledgePoint, Question, QuestionFlag, TaskLog, User
from app.main import create_app
from app.security import hash_password
from app.web import observability
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "obs.db"
    migrate(path)
    engine = create_db_engine(path)
    with create_session_factory(engine)() as s:
        owner = s.execute(select(User).where(User.email == "owner@local")).scalars().one()
        owner.password_hash = _HASH
        s.add(User(id=3, email="user@local", username="u", password_hash=_HASH, role="user"))
        s.flush()

        s.add(Domain(id=1, name="后端"))
        s.flush()
        # 已挂载的公共题 / 两道没挂载的公共题 / 一道没挂载的私有题
        s.add(KnowledgePoint(id=1, domain_id=1, name="索引", status="confirmed", origin="manual"))
        s.add(KnowledgePoint(id=2, domain_id=1, name="待审草稿", status="draft", origin="proposed"))
        s.flush()
        s.add(Question(kind="knowledge", stem="已挂载", difficulty=3, primary_point_id=1))
        s.add(Question(kind="knowledge", stem="待挂 A", difficulty=3))
        s.add(Question(kind="knowledge", stem="待挂 B", difficulty=3))
        s.add(Question(kind="design", stem="私有待挂", difficulty=3, owner_user_id=3,
                       visibility="private", origin="generated"))
        s.flush()

        s.add(Job(kind="self_repair_stats", status="pending"))
        s.add(Job(kind="self_repair_stats", status="done", finished_at="2026-01-01 00:00:00"))
        s.add(Job(kind="self_repair_stats", status="failed", error="外部服务挂了"))
        s.add(QuestionFlag(question_id=2, kind="suspect_mount", detail="累计考过 6 次，未命中 5 次"))
        s.commit()
    return path


@pytest.fixture
def app(db: Path):
    return create_app(Settings(database_path=db))


@pytest.fixture
def owner_client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "owner@local", "password": PASSWORD})
        yield c


@pytest.fixture
def user_client(app):
    with TestClient(app) as c:
        c.post("/login", data={"email": "user@local", "password": PASSWORD})
        yield c


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------
def test_only_owner_can_see_observability(user_client: TestClient) -> None:
    """它是本地管理面 —— 普通用户看到题库规模、失败原因、库体积没有道理。"""
    assert user_client.get("/admin/observability").status_code == 403


def test_anonymous_cannot_see_observability(app) -> None:
    with TestClient(app) as c:
        assert c.get("/admin/observability", follow_redirects=False).status_code == 403


# ---------------------------------------------------------------------------
# 数字
# ---------------------------------------------------------------------------
def test_pending_pool_counts_public_questions_only(owner_client: TestClient) -> None:
    """待定池 = **公共**题里没挂载的。私有题没挂载是正常的（它等着私有挂载流程），
    混进来会让"还有多少题没法用"这个数字失去意义。"""
    body = owner_client.get("/admin/observability").text
    assert "<strong>2</strong> 道公共题还没挂到知识点上" in body


def test_scale_section_counts_every_thing(owner_client: TestClient) -> None:
    body = owner_client.get("/admin/observability").text
    assert "2（其中 draft 1）" in body, "知识点总数 + 没经人审的容器节点"
    assert "考察点" in body and "用户" in body


def test_mounted_ratio_is_visible(owner_client: TestClient) -> None:
    """3 道公共题里 1 道挂上了 → 33.3%。这个数字是"知识层建到哪了"的唯一直接读数。"""
    body = owner_client.get("/admin/observability").text
    assert "33.3%" in body


def test_no_public_questions_is_not_zero_percent(owner_client: TestClient, db: Path) -> None:
    """一道公共题都没有时显示「—」而不是 0% —— **0% 和"没有"是两件事**。

    （第一版把 `mounted_ratio` 直接算成 `1 - unmounted/questions`，在空库上会
    `ZeroDivisionError`；改成先返回 None 之后，模板必须能接住它。所以这条测试既
    钉"除零"，也钉"模板真的处理了 None"。）
    """
    with create_session_factory(create_db_engine(db))() as s:
        for q in s.execute(select(Question)).scalars():
            q.owner_user_id = 3          # 全变成私有题 → 公共题一道不剩
            q.visibility = "private"
        s.commit()

    body = owner_client.get("/admin/observability").text
    assert "还没有公共题" in body
    assert "0.0%" not in body


# ---------------------------------------------------------------------------
# 失败必须看得见
# ---------------------------------------------------------------------------
def test_failed_job_reason_is_visible(owner_client: TestClient) -> None:
    """决策 23：没有了观测，前面所有"降级不静默"只是"记下来了"。

    所以"记了但没人看得到"是这条测试要拦的东西。
    """
    body = owner_client.get("/admin/observability").text
    assert "外部服务挂了" in body
    assert "self_repair_stats" in body


def test_job_counts_are_shown_per_status(owner_client: TestClient) -> None:
    body = owner_client.get("/admin/observability").text
    assert "最老的一条待执行" in body
    assert "总计" in body


def test_oldest_pending_is_flagged_as_worker_missing(owner_client: TestClient) -> None:
    """最老的那条 pending 很旧 = 没有 worker 在跑。这句话必须印在页面上 ——
    否则"队列堵了"要靠人自己看出来。"""
    body = owner_client.get("/admin/observability").text
    assert "没有 worker 在跑" in body


# ---------------------------------------------------------------------------
# 质量仪表板
# ---------------------------------------------------------------------------
def test_quality_flags_are_listed(owner_client: TestClient) -> None:
    """ADR-0002："矛盾记录是这套结构唯一白送的能力" —— 那它必须落在页面上。"""
    body = owner_client.get("/admin/observability").text
    assert "suspect_mount" in body
    assert "累计考过 6 次" in body


# ---------------------------------------------------------------------------
# 库体积
# ---------------------------------------------------------------------------
def test_db_size_is_shown_in_human_units(owner_client: TestClient, db: Path) -> None:
    """2C2G 的机器上"库多大 / WAL 多大"是要盯的数字。

    断言与页面用同一个函数算出的值是**有意的**：这条测的是"接线有没有接上"，
    单位换算本身由下面的 `test_human_bytes_*` 钉死。
    """
    body = owner_client.get("/admin/observability").text
    assert observability.human_bytes(db.stat().st_size) in body
    assert "WAL" in body


def test_human_bytes_units() -> None:
    assert observability.human_bytes(0) == "0 B"
    assert observability.human_bytes(1023) == "1023 B"
    assert observability.human_bytes(1024) == "1.0 KB"
    assert observability.human_bytes(1536) == "1.5 KB"
    assert observability.human_bytes(1024 * 1024) == "1.0 MB"
    assert observability.human_bytes(3 * 1024**3) == "3.0 GB"


# ---------------------------------------------------------------------------
# 离线任务报告（task_logs）
# ---------------------------------------------------------------------------
def test_task_report_section_is_empty_before_any_job(owner_client: TestClient, db: Path) -> None:
    body = owner_client.get("/admin/observability").text
    assert "还没有离线任务报告" in body


def test_task_report_shows_a_human_sentence(owner_client: TestClient, db: Path) -> None:
    """报告要能读懂 —— 页面上出现 `{'job_id': 1, ...}` 这种 dict 字面量等于没写。"""
    with create_session_factory(create_db_engine(db))() as s:
        s.add(TaskLog(kind="generate_questions",
                      payload={"count": 6},
                      result={"job_id": 1, "status": "done", "message": "生成 6 道题"}))
        s.commit()

    body = owner_client.get("/admin/observability").text
    assert "生成 6 道题" in body
    assert "job_id" not in body, "报告必须是给人看的话，不是 JSON"


def test_task_report_marks_failures(owner_client: TestClient, db: Path) -> None:
    """失败的**报告行**要标红。

    ⚠️ 这条测试第一版是漏的：它只断言页面上有 `class="error"`，而队列那一节
    本来就有一个失败任务会渲染出同样的类名 —— 于是"报告不标失败"这个 bug
    照样全绿（实测被变异测试抓出来的）。所以这里先把 jobs 清空，让那个类名
    **只可能来自报告行**。
    """
    with create_session_factory(create_db_engine(db))() as s:
        for job in s.execute(select(Job)).scalars():
            s.delete(job)
        s.add(TaskLog(kind="self_repair_stats",
                      result={"job_id": 2, "status": "failed", "error": "模型超时"}))
        s.commit()

    body = owner_client.get("/admin/observability").text
    assert "没有失败的任务" in body, "队列里已经没有任务了，类名只可能来自报告"
    assert "模型超时" in body
    assert 'class="error"' in body
