import base64
import math
import time
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import (
    AppConfig,
    DailyConfig,
    LLMConfig,
    NotificationConfig,
    NowcoderConfig,
    SourcesConfig,
)
from app.db import commit, get_session, init_db
from app.errors import LLMError
from app.models import (
    Judgment,
    Question,
    QuestionStatus,
    QuestionType,
    Session,
    SessionKind,
    SessionStatus,
    Source,
    SourceType,
    User,
)
from app.web.routes import _inflight, create_app
from tests.fakes import FakeEmbedder, FakeLLM

CONTINUE = {"action": "continue", "followup": "追问：谈谈扩容"}
FINISH = {"action": "finish", "followup": "好的，本轮结束"}
JUDGMENT = {
    "scores": {"accuracy": 85, "completeness": 70, "clarity": 90, "depth": 60},
    "review": "整体不错",
    "reference_answer": "参考答案",
    "weak_tags": ["RAG"],
}


def make_config():
    return AppConfig(
        llm=LLMConfig(
            base_url="http://x", api_key_env="LLM_API_KEY",
            generate_model="g", judge_model="j",
        ),
        nowcoder=NowcoderConfig(cookie_env="NOWCODER_COOKIE", request_interval=0, retries=1),
        daily=DailyConfig(
            max_new_questions=36, knowledge_limit=3, design_limit=2,
            project_limit=1, chain_max_rounds=20, schedule="08:00",
        ),
        notification=NotificationConfig(enabled=False),
        sources=SourcesConfig(github_repos=[]),
    )


def make_client(db, llm, runner=None, embedder=None, user_role="owner", username="testuser", **overrides):
    """测试客户端：自动创建测试用户（owner）+ token 带在请求头（多用户鉴权后适配）。"""
    from app.auth import create_token, hash_password
    from app.models import User

    embedder = embedder or FakeEmbedder()
    app = create_app(
        make_config(),
        llm_factory=lambda role: llm,
        daily_runner=runner,
        embedder_factory=lambda: embedder,
    )
    with get_session() as session:
        user = session.scalars(
            select(User).where(User.username == username)
        ).first()
        if user is None:
            user = User(
                username=username,
                password_hash=hash_password("testpass1"),
                role=user_role,
            )
            session.add(user)
            commit(session)
            session.refresh(user)
        token = create_token(user.id)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def vec(*xs):
    xs = list(xs)
    while len(xs) < 8:
        xs.append(0.0)
    norm = math.sqrt(sum(x * x for x in xs))
    return [x / norm for x in xs]


@pytest.fixture
def db(tmp_path):
    """路由测试用文件 SQLite：FastAPI 线程池中内存库每线程独立连接会丢表。"""
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    with get_session() as session:
        yield session


def ensure_test_user(db):
    """确保测试用户存在（与 make_client 同一用户），返回 User。"""
    from app.auth import create_token, hash_password
    from app.models import User

    user = db.scalars(select(User).where(User.username == "testuser")).first()
    if user is None:
        user = User(username="testuser", password_hash=hash_password("testpass1"), role="owner")
        db.add(user)
        commit(db)
        db.refresh(user)
    return user


def _backdate_picks(db, question_id: int, days: int = 1):
    """把某题的 UserPick 回拨到 N 天前（模拟昨天选的题）。"""
    from sqlalchemy import update as _update
    from app.models import UserPick

    db.execute(
        _update(UserPick)
        .where(UserPick.question_id == question_id)
        .values(picked_at=datetime.now() - timedelta(days=days))
    )
    commit(db)


def add_today_question(db, stem="讲一下 HashMap 底层原理", status_today=True, tags=None, qtype=QuestionType.knowledge):
    from app.models import UserPick

    source = Source(type=SourceType.manual, source_hash=f"h-{stem}")
    db.add(source)
    commit(db)
    db.refresh(source)
    q = Question(
        source_id=source.id,
        type=qtype,
        stem=stem,
        tags=tags or [],
        good_criteria=["完整、准确"],
        bad_criteria=["答非所问"],
        status=QuestionStatus.today if status_today else QuestionStatus.pending,
        selected_at=datetime.now() if status_today else None,
    )
    db.add(q)
    commit(db)
    db.refresh(q)
    if status_today:
        user = ensure_test_user(db)
        db.add(UserPick(user_id=user.id, question_id=q.id, picked_at=datetime.now()))
        commit(db)
    return q


@pytest.fixture(autouse=True)
def clean_inflight():
    yield
    _inflight.clear()


# --- happy ---


def test_full_answer_flow(db):
    q = add_today_question(db)
    llm = FakeLLM([FINISH, JUDGMENT])
    client = make_client(db, llm)

    resp = client.get("/api/today")
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == q.id
    assert resp.json()[0]["done"] is False

    resp = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"})
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    resp = client.post(f"/api/sessions/{session_id}/answer", json={"answer": "HashMap 底层是数组加链表"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "judging"

    resp = client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["judgment"]["total_score"] == 76
    assert body["judgment"]["weak_tags"] == ["RAG"]

    resp = client.get("/api/today")
    assert resp.json()[0]["done"] is True


def test_open_kind_single_round(db):
    q = add_today_question(db)
    llm = FakeLLM([JUDGMENT])
    client = make_client(db, llm)
    resp = client.post("/api/sessions", json={"question_id": q.id, "kind": "open"})
    session_id = resp.json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "我的回答"})
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["status"] == "done"
    assert body["judgment"]["total_score"] == 76


def test_chain_multi_rounds(db):
    q = add_today_question(db)
    llm = FakeLLM([CONTINUE, FINISH, JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]

    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "第一轮回答"})
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["status"] == "active"
    assert body["rounds_done"] == 1
    assert body["followup"] == "追问：谈谈扩容"  # 面试官追问透传给前端
    assert body["transcript"] == [
        {"role": "user", "content": "第一轮回答"},
        {"role": "interviewer", "content": "追问：谈谈扩容"},
    ]

    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "第二轮回答"})
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["status"] == "done"  # 判分完成；transcript 仅 active 状态透传（结果页展示判分）


def test_history_list(db):
    q = add_today_question(db, tags=["RAG"])
    llm = FakeLLM([JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "open"}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "回答"})

    groups = client.get("/api/history").json()
    assert len(groups) == 1
    items = groups[0]["items"]
    assert len(items) == 1
    assert items[0]["done"] is True
    assert items[0]["total_score"] == 76
    assert items[0]["type"] == "knowledge"
    assert items[0]["status"] == "ok"
    assert items[0]["tags"] == ["RAG"]  # 分类筛选依赖


def test_judgment_reference_used_flag(db):
    """先答出一道高分题（≥80）→ 再答同类题时注入参考，判分结果暴露 reference_used=True。"""
    high = {
        "scores": {"accuracy": 90, "completeness": 85, "clarity": 85, "depth": 85},
        "review": "优秀", "reference_answer": "参考答案", "weak_tags": ["RAG"],
    }
    seed = add_today_question(db, stem="讲一下 KV Cache 的原理", tags=["KV Cache"])
    other = add_today_question(db, stem="请讲讲 KV Cache 的实现", tags=["KV Cache"])
    embedder = FakeEmbedder(
        vectors={
            "讲一下 KV Cache 的原理": vec(1),
            "请讲讲 KV Cache 的实现": vec(1),
        }
    )
    client = make_client(db, FakeLLM([high, high]), embedder=embedder)

    sid1 = client.post("/api/sessions", json={"question_id": seed.id, "kind": "open"}).json()["session_id"]
    client.post(f"/api/sessions/{sid1}/answer", json={"answer": "完整的高分回答内容"})

    sid2 = client.post("/api/sessions", json={"question_id": other.id, "kind": "open"}).json()["session_id"]
    client.post(f"/api/sessions/{sid2}/answer", json={"answer": "同类题的回答"})
    body = client.get(f"/api/sessions/{sid2}").json()
    assert body["status"] == "done"
    assert body["judgment"]["reference_used"] is True


def test_reference_retrieval_user_isolation(db):
    """判分参考检索跨用户隔离（A3）：B 作答不得注入 A 的高分回答。"""
    high = {
        "scores": {"accuracy": 90, "completeness": 85, "clarity": 85, "depth": 85},
        "review": "优秀", "reference_answer": "参考答案", "weak_tags": ["RAG"],
    }
    seed = add_today_question(db, stem="讲一下 KV Cache 的原理", tags=["KV Cache"])
    other = add_today_question(db, stem="请讲讲 KV Cache 的实现", tags=["KV Cache"])
    embedder = FakeEmbedder(
        vectors={
            "讲一下 KV Cache 的原理": vec(1),
            "请讲讲 KV Cache 的实现": vec(1),
        }
    )
    llm = FakeLLM([high, high])
    alice = make_client(db, llm, embedder=embedder, user_role="user", username="alice")
    bob = make_client(db, llm, embedder=embedder, user_role="user", username="bob")

    sid1 = alice.post("/api/sessions", json={"question_id": seed.id, "kind": "open"}).json()["session_id"]
    alice.post(f"/api/sessions/{sid1}/answer", json={"answer": "A 独有的高分回答机密内容"})

    sid2 = bob.post("/api/sessions", json={"question_id": other.id, "kind": "open"}).json()["session_id"]
    bob.post(f"/api/sessions/{sid2}/answer", json={"answer": "B 自己的回答"})
    body = bob.get(f"/api/sessions/{sid2}").json()
    assert body["status"] == "done"
    assert body["judgment"]["reference_used"] is False  # B 的判分未注入 A 的高分参考
    bob_prompt = " ".join(
        m.get("content", "") for m in llm.calls[1] if m["role"] == "user"
    )
    assert "A 独有的高分回答机密内容" not in bob_prompt


def test_history_contains_unanswered_today_question(db):
    add_today_question(db, stem="没做过的题")
    client = make_client(db, FakeLLM([]))
    groups = client.get("/api/history").json()
    assert len(groups) == 1
    item = groups[0]["items"][0]
    assert item["done"] is False
    assert item["status"] == "not_answered"
    assert item["total_score"] is None


def test_history_excludes_never_selected_questions(db):
    add_today_question(db, stem="选过的题")
    add_today_question(db, stem="没选过的题", status_today=False)
    client = make_client(db, FakeLLM([]))
    groups = client.get("/api/history").json()
    stems = [i["stem"] for g in groups for i in g["items"]]
    assert stems == ["选过的题"]


def _finish_session(db, question_id, user_id, score, days_ago=0):
    """造一条 finished + 判分 ok 的会话（ended_at = 今天 - days_ago 天）。"""
    from app.models import SessionStatus

    s = Session(
        question_id=question_id,
        user_id=user_id,
        kind=SessionKind.open,
        status=SessionStatus.finished,
        ended_at=datetime.now() - timedelta(days=days_ago),
    )
    db.add(s)
    commit(db)
    db.refresh(s)
    db.add(Judgment(
        session_id=s.id,
        scores={"status": "ok"},
        total_score=score,
        weak_tags=[],
    ))
    commit(db)
    return s


def test_stats_aggregates_trend(db):
    """stats：答题数按 ended_at 天聚合、平均分、汇总数字正确；failed 判分（total_score=NULL）不计入。"""
    add_today_question(db, stem="统计题 A", status_today=False)
    add_today_question(db, stem="统计题 B", status_today=False)
    add_today_question(db, stem="统计题 C", status_today=False)
    client = make_client(db, FakeLLM([]))
    uid = db.scalars(select(User).where(User.username == "testuser")).one().id
    qs = db.scalars(select(Question)).all()
    _finish_session(db, qs[0].id, uid, 80, days_ago=0)
    _finish_session(db, qs[1].id, uid, 60, days_ago=0)
    _finish_session(db, qs[2].id, uid, None, days_ago=0)  # failed 判分（无总分）

    data = client.get("/api/stats").json()
    assert data["bank_total"] == 3
    assert data["answered_total"] == 2  # failed 不计入
    assert data["avg_score"] == 70
    assert len(data["trend"]) == 30
    today_row = data["trend"][-1]
    assert today_row["answered"] == 2
    assert today_row["avg_score"] == 70


def test_stats_empty_db(db):
    """stats：无任何答题 → trend 30 天空行，汇总为 0/None。"""
    client = make_client(db, FakeLLM([]))
    data = client.get("/api/stats").json()
    assert len(data["trend"]) == 30
    assert all(r["answered"] == 0 for r in data["trend"])
    assert data["answered_total"] == 0
    assert data["avg_score"] is None
    assert data["done_questions"] == 0


def test_stats_user_isolation(db):
    """stats per-user：A 的答题不出现在 B 的统计里。"""
    add_today_question(db, stem="隔离统计题", status_today=False)
    alice = make_client(db, FakeLLM([]), user_role="user", username="alice")
    bob = make_client(db, FakeLLM([]), user_role="user", username="bob")
    uid = db.scalars(select(User).where(User.username == "alice")).one().id
    q = db.scalars(select(Question)).one()
    _finish_session(db, q.id, uid, 90, days_ago=0)

    a = alice.get("/api/stats").json()
    b = bob.get("/api/stats").json()
    assert a["answered_total"] == 1
    assert b["answered_total"] == 0


def test_today_empty_list(db):
    client = make_client(db, FakeLLM([]))
    assert client.get("/api/today").json() == []


def test_today_excludes_yesterday(db):
    """回归：昨日被选为今日的题不再出现在今日列表（今日 = 今天选中的题）。"""
    add_today_question(db, stem="今天的题")
    yesterday = add_today_question(db, stem="昨天的题")
    yesterday.selected_at = datetime.now() - timedelta(days=1)
    _backdate_picks(db, yesterday.id)
    commit(db)

    client = make_client(db, FakeLLM([]))
    items = client.get("/api/today").json()
    assert [i["stem"] for i in items] == ["今天的题"]


def test_today_date_param_selects_day(db):
    """日历单选：date=YYYY-MM-DD 返回被选为当日题目的题（不限 status）。"""
    add_today_question(db, stem="今天的题")
    old = add_today_question(db, stem="昨天的题")
    old.selected_at = datetime.now() - timedelta(days=1)
    _backdate_picks(db, old.id)
    commit(db)
    client = make_client(db, FakeLLM([]))

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    by_day = client.get("/api/today", params={"date": yesterday}).json()
    assert [i["stem"] for i in by_day] == ["昨天的题"]
    today_items = client.get("/api/today", params={"date": today}).json()
    assert [i["stem"] for i in today_items] == ["今天的题"]
    assert client.get("/api/today", params={"date": "2026-13-99"}).status_code == 400
    assert client.get("/api/today", params={"date": "not-a-date"}).status_code == 400


def test_create_session_resumes_existing_active(db):
    """D18：已有 active 会话时重复创建 → 返回同一会话，不产生新记录。"""
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    first = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()
    second = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()
    assert second["session_id"] == first["session_id"]
    assert second["resumed"] is True
    with db:
        from sqlalchemy import func as f
        assert db.scalars(f.count(Session.id)).one() == 1


def test_create_session_new_when_none_active(db):
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    resp = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()
    assert resp["resumed"] is False
    assert resp["status"] == "active"


def test_today_list_marks_in_progress(db):
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"})
    item = client.get("/api/today").json()[0]
    assert item["active_session_id"] is not None
    assert item["done"] is False


# --- 历史详情 ---


def test_question_history_multiple_attempts(db):
    q = add_today_question(db)
    llm = FakeLLM([FINISH, JUDGMENT, FINISH, JUDGMENT])
    client = make_client(db, llm)
    sid1 = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{sid1}/answer", json={"answer": "第一次回答"})
    # 新开会话（上次已 finished，create 不再复用 active）
    sid2 = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{sid2}/answer", json={"answer": "第二次回答"})

    data = client.get(f"/api/questions/{q.id}/history").json()
    assert [a["session_id"] for a in data["attempts"]] == [sid2, sid1]  # 倒序
    assert data["attempts"][0]["judgment_status"] == "ok"
    assert data["attempts"][0]["total_score"] == 76
    assert data["attempts"][0]["transcript"] == [
        {"role": "user", "content": "第二次回答"},
        {"role": "interviewer", "content": "好的，本轮结束"},
    ]
    assert data["attempts"][0]["judgment"]["weak_tags"] == ["RAG"]
    assert data["attempts"][1]["judgment"]["total_score"] == 76


def test_question_history_failed_judgment(db):
    q = add_today_question(db)
    llm = FakeLLM([LLMError("down"), LLMError("down")])
    client = make_client(db, llm)
    sid = client.post("/api/sessions", json={"question_id": q.id, "kind": "open"}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/answer", json={"answer": "回答"})

    data = client.get(f"/api/questions/{q.id}/history").json()
    a = data["attempts"][0]
    assert a["judgment_status"] == "failed"
    assert a["judgment"] is None
    assert a["total_score"] is None


def test_question_history_unfinished_session(db):
    """答过 ≥1 轮的未完成会话照常显示（未完成徽标），未答过的空壳被跳过。"""
    q = add_today_question(db)
    llm = FakeLLM([CONTINUE])  # 第一轮 continue，会话保持 active
    client = make_client(db, llm)
    sid = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/answer", json={"answer": "答了一轮"})

    data = client.get(f"/api/questions/{q.id}/history").json()
    a = data["attempts"][0]
    assert a["status"] == "active"
    assert a["judgment_status"] is None
    assert len(a["transcript"]) == 2  # user + interviewer 追问


def test_question_history_empty_and_404(db):
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    data = client.get(f"/api/questions/{q.id}/history").json()
    assert data["attempts"] == []
    assert client.get("/api/questions/99999/history").status_code == 404


def test_question_history_skips_empty_shell_sessions(db):
    """空壳会话（无回答无判分）不出现在尝试列表。"""
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"})  # 空壳
    data = client.get(f"/api/questions/{q.id}/history").json()
    assert data["attempts"] == []  # 空壳被跳过，不显示"第 N 次"


# --- 历史删除 ---


def test_delete_session_cascades(db):
    q = add_today_question(db)
    llm = FakeLLM([FINISH, JUDGMENT])
    client = make_client(db, llm)
    sid = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/answer", json={"answer": "回答"})

    resp = client.delete(f"/api/sessions/{sid}")
    assert resp.status_code == 200
    with db:
        from sqlalchemy import text as _t
        assert db.exec(_t("SELECT COUNT(*) FROM sessions WHERE id = :i"), params={"i": sid}).one()[0] == 0
        assert db.exec(_t("SELECT COUNT(*) FROM attempts WHERE session_id = :i"), params={"i": sid}).one()[0] == 0
        assert db.exec(_t("SELECT COUNT(*) FROM judgments WHERE session_id = :i"), params={"i": sid}).one()[0] == 0
        assert db.get(Question, q.id) is not None  # 题目保留


def test_delete_session_404_and_inflight_409(db):
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    assert client.delete("/api/sessions/99999").status_code == 404
    sid = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    _inflight.add(sid)
    try:
        assert client.delete(f"/api/sessions/{sid}").status_code == 409
    finally:
        _inflight.discard(sid)


def test_delete_question_history_keeps_question(db):
    q = add_today_question(db)
    llm = FakeLLM([FINISH, JUDGMENT, FINISH, JUDGMENT])
    client = make_client(db, llm)
    for _ in range(2):
        sid = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
        client.post(f"/api/sessions/{sid}/answer", json={"answer": "回答"})

    resp = client.delete(f"/api/questions/{q.id}/history")
    assert resp.status_code == 200
    data = client.get(f"/api/questions/{q.id}/history").json()
    assert data["attempts"] == []
    with db:
        from sqlalchemy import text as _t
        assert db.exec(_t("SELECT COUNT(*) FROM sessions WHERE question_id = :i"), params={"i": q.id}).one()[0] == 0
        assert db.get(Question, q.id) is not None  # 题目保留


def test_delete_question_history_404(db):
    client = make_client(db, FakeLLM([]))
    assert client.delete("/api/questions/99999/history").status_code == 404


# --- 薄弱点复习 ---


def test_review_returns_matching_questions(db):
    done_q = add_today_question(db, stem="已做的 RAG 题", tags=["RAG"])
    undone_q = add_today_question(db, stem="未做的 RAG 题", tags=["RAG"])
    add_today_question(db, stem="Java 题", tags=["Java"])
    llm = FakeLLM([JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": done_q.id, "kind": "open"}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "回答"})

    items = client.get("/api/review", params={"tag": "RAG"}).json()["items"]
    assert [i["stem"] for i in items] == ["未做的 RAG 题", "已做的 RAG 题"]  # 未做优先
    assert items[0]["done"] is False
    assert items[1]["done"] is True
    assert items[1]["total_score"] == 76


def test_review_paper_generates(db):
    """复习卷 v2：LLM 生成讲义（markdown→HTML）+ 推荐题（须在素材内）。"""
    from app.web.routes import _review_paper_cache

    q1 = add_today_question(db, stem="讲一下 RAG 检索流程", tags=["RAG"])
    q2 = add_today_question(db, stem="RAG 与向量数据库", tags=["RAG"])
    llm = FakeLLM([{
        "paper": "## 核心知识点\n\n- RAG 全流程\n\n## 答题框架\n\n**分层回答**",
        "recommended_ids": [q1.id, q2.id],
    }])
    client = make_client(db, llm)
    data = client.get("/api/review/paper", params={"tag": "RAG"}).json()

    assert "<h2>核心知识点</h2>" in data["paper_html"]  # markdown 已转 HTML
    assert "<strong>分层回答</strong>" in data["paper_html"]
    assert sorted(data["recommended_ids"]) == sorted([q1.id, q2.id])
    assert data["paper_html"]  # 非空
    with db:
        _review_paper_cache.clear()


def test_review_paper_cached(db):
    """标签级缓存：二次请求不重复调 LLM。"""
    from app.web.routes import _review_paper_cache

    add_today_question(db, stem="RAG 题", tags=["RAG"])
    llm = FakeLLM([{
        "paper": "## 讲义", "recommended_ids": [],
    }])
    client = make_client(db, llm)
    client.get("/api/review/paper", params={"tag": "RAG"})
    client.get("/api/review/paper", params={"tag": "RAG"})
    assert len(llm.calls) == 1  # 第二次命中缓存
    with db:
        _review_paper_cache.clear()


def test_review_paper_cache_expires(db):
    """缓存 TTL 过期后重新生成（旧缓存不永久生效）。"""
    from app.web.routes import _review_paper_cache
    from app.web.routes import _review_paper_ttl
    import time as _time

    add_today_question(db, stem="RAG 题", tags=["RAG"])
    llm = FakeLLM([
        {"paper": "## 讲义 v1", "recommended_ids": []},
        {"paper": "## 讲义 v2", "recommended_ids": []},
    ])
    client = make_client(db, llm)
    client.get("/api/review/paper", params={"tag": "RAG"})
    with db:
        key = next(iter(_review_paper_cache))
        _review_paper_cache[key]["ts"] = _time.time() - _review_paper_ttl - 1  # 强制过期
    data = client.get("/api/review/paper", params={"tag": "RAG"}).json()
    assert len(llm.calls) == 2  # 过期后重新生成
    assert "讲义 v2" in data["paper_html"]
    with db:
        _review_paper_cache.clear()


def test_review_paper_llm_failure_degraded(db):
    """LLM 生成失败降级：返回空讲义 + error 提示（不 500）。"""
    from app.errors import LLMError

    add_today_question(db, stem="RAG 题", tags=["RAG"])
    llm = FakeLLM([LLMError("llm down")])
    client = make_client(db, llm)
    resp = client.get("/api/review/paper", params={"tag": "RAG"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["paper_html"] == ""
    assert data["recommended_ids"] == []
    assert "失败" in data["error"]


def test_review_paper_invalid_tag_400(db):
    client = make_client(db, FakeLLM([]))
    assert client.get("/api/review/paper", params={"tag": "不存在的标签"}).status_code == 400


def test_review_excludes_other_tags(db):
    add_today_question(db, stem="RAG 题", tags=["RAG"])
    add_today_question(db, stem="Java 题", tags=["Java"])
    add_today_question(db, stem="RAG 检索题", tags=["RAG", "检索"])
    client = make_client(db, FakeLLM([]))
    data = client.get("/api/review", params={"tag": "RAG"}).json()
    assert data["fallback"] is False
    items = data["items"]
    assert len(items) == 2  # tags 数组含 "RAG" 元素的才命中，"Java 题"排除
    assert all("RAG" in i["tags"] for i in items)


def test_review_chinese_tag_matches(db):
    """中文标签精确匹配（json_each 修复）：LIKE cast 对 \\uXXXX 转义存储匹配不上。"""
    q1 = add_today_question(db, stem="缓存穿透题", tags=["缓存"])
    add_today_question(db, stem="Java 题", tags=["Java"])
    client = make_client(db, FakeLLM([]))
    data = client.get("/api/review", params={"tag": "缓存"}).json()
    assert data["fallback"] is False  # 不是回退：确实命中中文标签题
    assert [i["id"] for i in data["items"]] == [q1.id]
    assert data["items"][0]["tags"] == ["缓存"]


def test_bank_search_chinese_tag(db):
    """题库中文标签搜索（json_each 修复）：关键词搜中文标签能命中。"""
    add_today_question(db, stem="缓存一致性设计", tags=["缓存"], status_today=False)
    add_today_question(db, stem="纯题干题", tags=["Java"], status_today=False)
    client = make_client(db, FakeLLM([]))
    hit = client.get("/api/bank", params={"q": "缓存"}).json()
    assert hit["total"] == 1  # 题干命中（纯题干题无缓存标签）
    cat = client.get("/api/bank", params={"category": "后端基础", "page_size": 50}).json()
    assert cat["total"] >= 2  # 分类过滤（中文标签 Java/缓存）都命中


def test_review_invalid_tag_400(db):
    client = make_client(db, FakeLLM([]))
    resp = client.get("/api/review", params={"tag": "深度不足"})
    assert resp.status_code == 400


def test_review_no_questions_empty(db):
    client = make_client(db, FakeLLM([]))
    data = client.get("/api/review", params={"tag": "Agent"}).json()
    assert data["items"] == []
    assert data["fallback"] is True  # 空库时分类回退也为空，但标记已尝试回退
    assert data["fallback_category"] == "Agent 生态"


def test_review_fallback_same_category(db):
    """判分 weak_tags 指向题库无该标签的题 → 回退同分类题目（修复复习死胡同）。"""
    add_today_question(db, stem="Java 并发题", tags=["Java"])
    add_today_question(db, stem="Redis 题", tags=["Redis"])
    add_today_question(db, stem="RAG 题", tags=["RAG"])
    client = make_client(db, FakeLLM([]))

    data = client.get("/api/review", params={"tag": "MySQL"}).json()  # 无 MySQL 题
    assert data["fallback"] is True
    assert data["fallback_category"] == "后端基础"
    stems = [i["stem"] for i in data["items"]]
    assert "Java 并发题" in stems and "Redis 题" in stems
    assert "RAG 题" not in stems  # 其他分类不混入


def test_review_paper_fallback_uses_category_questions(db):
    """复习卷在该标签无题时用分类回退题作素材生成（LLM 失败降级不 500）。"""
    from app.web.routes import _review_paper_cache

    add_today_question(db, stem="Java 并发题", tags=["Java"])
    llm = FakeLLM([{"paper": "## 讲义", "recommended_ids": []}])
    client = make_client(db, llm)

    data = client.get("/api/review/paper", params={"tag": "MySQL"}).json()
    assert "讲义" in data["paper_html"]
    assert llm.calls  # 素材来自分类回退题，仍会生成
    with db:
        _review_paper_cache.clear()


def test_review_tags_aggregates_weak_tags(db):
    q1 = add_today_question(db, stem="RAG 题", tags=["RAG"])
    q2 = add_today_question(db, stem="缓存题", tags=["缓存"])
    client = make_client(db, FakeLLM([]))
    s1 = client.post("/api/sessions", json={"question_id": q1.id, "kind": "open"}).json()["session_id"]
    s2 = client.post("/api/sessions", json={"question_id": q2.id, "kind": "open"}).json()["session_id"]
    with db:
        db.add(Judgment(session_id=s1, scores={"status": "ok"}, total_score=80,
                        weak_tags=["RAG", "缓存"]))
        db.add(Judgment(session_id=s2, scores={"status": "ok"}, total_score=80,
                        weak_tags=["RAG"]))
        commit(db)

    tags = client.get("/api/review/tags").json()
    by_tag = {t["tag"]: t["count"] for t in tags}
    assert by_tag["RAG"] == 2
    assert by_tag["缓存"] == 1
    assert by_tag["Agent"] == 0
    assert len(tags) == 68  # 全部词表标签
    # 有计数的排最前
    counts = [t["count"] for t in tags]
    assert counts[0] == 2 and counts[1] == 1
    assert all(c == 0 for c in counts[2:])


def test_review_tags_empty_db(db):
    client = make_client(db, FakeLLM([]))
    tags = client.get("/api/review/tags").json()
    assert len(tags) == 68
    assert all(t["count"] == 0 for t in tags)


def test_tag_categories_structure(db):
    """GET /api/tags：分类结构 + 标签总数与词表一致。"""
    from app.tags import TAG_CATEGORIES, TAG_VOCABULARY

    client = make_client(db, FakeLLM([]))
    cats = client.get("/api/tags").json()
    assert [c["name"] for c in cats] == [name for name, _ in TAG_CATEGORIES]
    flat = [t for c in cats for t in c["tags"]]
    assert flat == list(TAG_VOCABULARY)
    assert len(flat) == 68


# --- 题库浏览 ---


def test_bank_pagination(db):
    for i in range(25):
        add_today_question(db, stem=f"题库题{i}", status_today=False, tags=["Java"])
    client = make_client(db, FakeLLM([]))

    p1 = client.get("/api/bank", params={"page": 1, "page_size": 20}).json()
    p2 = client.get("/api/bank", params={"page": 2, "page_size": 20}).json()
    assert p1["total"] == 25
    assert p1["total_pages"] == 2
    assert len(p1["items"]) == 20
    assert len(p2["items"]) == 5
    ids1 = [i["id"] for i in p1["items"]]
    ids2 = [i["id"] for i in p2["items"]]
    assert not set(ids1) & set(ids2)  # 两页不重叠
    assert ids1 == sorted(ids1, reverse=True)  # id 倒序（最新在前）


def test_bank_filter_type_and_category(db):
    add_today_question(db, stem="知识题", tags=["Java"], status_today=False)
    add_today_question(db, stem="设计题", tags=["缓存"], status_today=False, qtype=QuestionType.design)
    client = make_client(db, FakeLLM([]))

    by_type = client.get("/api/bank", params={"type": "design"}).json()
    assert by_type["total"] == 1
    assert by_type["items"][0]["stem"] == "设计题"

    by_cat = client.get("/api/bank", params={"category": "后端基础"}).json()
    assert by_cat["total"] == 2  # Java/缓存 均在"后端基础"分类


def test_bank_invalid_params(db):
    client = make_client(db, FakeLLM([]))
    assert client.get("/api/bank", params={"page": 0}).status_code == 400
    assert client.get("/api/bank", params={"page_size": 999}).status_code == 400
    assert client.get("/api/bank", params={"type": "essay"}).status_code == 400
    assert client.get("/api/bank", params={"category": "不存在"}).status_code == 400
    over = client.get("/api/bank", params={"page": 99}).json()
    assert over["items"] == []  # 超页返回空


def test_bank_search_stem_and_type(db):
    add_today_question(db, stem="Redis 缓存一致性", tags=["Java"], status_today=False)
    add_today_question(db, stem="MySQL 索引优化", tags=["Java"], status_today=False)
    add_today_question(
        db, stem="缓存设计", tags=["Redis"], status_today=False,
        qtype=QuestionType.design,
    )
    client = make_client(db, FakeLLM([]))

    hit = client.get("/api/bank", params={"q": "redis"}).json()
    assert hit["total"] == 2  # 题干命中（大小写不敏感）+ 标签命中（题干不含关键词）
    assert {i["stem"] for i in hit["items"]} == {"Redis 缓存一致性", "缓存设计"}

    combo = client.get("/api/bank", params={"q": "redis", "type": "design"}).json()
    assert combo["total"] == 1
    assert combo["items"][0]["stem"] == "缓存设计"

    missed = client.get("/api/bank", params={"q": "不存在的词"}).json()
    assert missed["total"] == 0
    assert missed["items"] == []

    blank = client.get("/api/bank", params={"q": "   "}).json()
    assert blank["total"] == 3  # 空白 q 视为无过滤


def test_bank_filter_difficulty(db):
    add_today_question(db, stem="入门题", tags=["Java"], status_today=False, qtype=QuestionType.knowledge)
    add_today_question(db, stem="深度题", tags=["Java"], status_today=False, qtype=QuestionType.knowledge)
    with db:
        from sqlalchemy import text as _t
        db.exec(_t("UPDATE questions SET difficulty = 5 WHERE stem = :s"),
                params={"s": "深度题"})
        commit(db)
    client = make_client(db, FakeLLM([]))

    by_diff = client.get("/api/bank", params={"difficulty": 5}).json()
    assert by_diff["total"] == 1
    assert by_diff["items"][0]["stem"] == "深度题"

    combo = client.get("/api/bank", params={"difficulty": 1, "type": "knowledge"}).json()
    assert combo["total"] == 1
    assert combo["items"][0]["stem"] == "入门题"

    assert client.get("/api/bank", params={"difficulty": 9}).status_code == 400
    assert client.get("/api/bank", params={"difficulty": 0}).status_code == 400


def test_bank_done_flag(db):
    q = add_today_question(db, stem="做过题", tags=["Java"], status_today=False)
    add_today_question(db, stem="未做题", tags=["Java"], status_today=False)
    llm = FakeLLM([JUDGMENT])
    client = make_client(db, llm)
    sid = client.post("/api/sessions", json={"question_id": q.id, "kind": "open"}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/answer", json={"answer": "回答"})

    data = client.get("/api/bank", params={"page_size": 50}).json()
    item = next(i for i in data["items"] if i["id"] == q.id)
    assert item["done"] is True
    undone = next(i for i in data["items"] if i["id"] != q.id)
    assert undone["done"] is False


# --- 用户上传题目 ---


def test_upload_direct_mode_inserts_questions(db):
    content = "Q: 讲一下 HashMap 底层原理\n- 缓存穿透怎么解决？\n\nQ: Redis 分布式锁"
    client = make_client(db, FakeLLM([]))
    resp = client.post("/api/upload", json={"filename": "题.md", "content": content})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert body["count"] == 3
    with db:
        from sqlalchemy import text as _t
        n = db.exec(_t("SELECT COUNT(*) FROM questions WHERE source_id = :i"),
                    params={"i": body["source_id"]}).one()[0]
        assert n == 3
        stems = [r[0] for r in db.exec(_t(
            "SELECT stem FROM questions WHERE source_id = :i ORDER BY id"),
            params={"i": body["source_id"]}).all()]
        assert stems == ["讲一下 HashMap 底层原理", "缓存穿透怎么解决？", "Redis 分布式锁"]
        assert db.exec(_t(
            "SELECT difficulty FROM questions WHERE source_id = :i ORDER BY id LIMIT 1"),
            params={"i": body["source_id"]}).one()[0] == 1


def test_upload_direct_mode_tags_llm(db):
    import json as _json

    content = "Q: 讲一下 HashMap 底层原理\nQ: Redis 分布式锁"
    llm = FakeLLM([
        {"items": [
            {"stem": "讲一下 HashMap 底层原理", "tags": ["Java", "词表外", "数据结构"], "difficulty": 4},
            {"stem": "Redis 分布式锁", "tags": ["缓存"], "difficulty": 9},
        ]}
    ])
    client = make_client(db, llm)
    body = client.post("/api/upload", json={"filename": "题.md", "content": content}).json()
    with db:
        from sqlalchemy import text as _t
        tags1 = _json.loads(db.exec(_t("SELECT tags FROM questions WHERE stem = :s"),
                                    params={"s": "讲一下 HashMap 底层原理"}).one()[0])
        tags2 = _json.loads(db.exec(_t("SELECT tags FROM questions WHERE stem = :s"),
                                    params={"s": "Redis 分布式锁"}).one()[0])
        assert "词表外" not in tags1 and "Java" in tags1 and "数据结构" in tags1
        assert "缓存" in tags2
        diff1 = db.exec(_t("SELECT difficulty FROM questions WHERE stem = :s"),
                        params={"s": "讲一下 HashMap 底层原理"}).one()[0]
        diff2 = db.exec(_t("SELECT difficulty FROM questions WHERE stem = :s"),
                        params={"s": "Redis 分布式锁"}).one()[0]
        assert diff1 == 4  # LLM 标注难度回写
        assert diff2 == 5  # 9 钳制到 5


def test_upload_direct_mode_verdict_delete(db):
    """后台校验：verdict=delete 的题（反问/闲聊）被级联删除。"""
    content = "Q: 讲一下 HashMap 底层原理\nQ: 反问：贵公司主要做什么业务"
    llm = FakeLLM([
        {"items": [
            {"stem": "讲一下 HashMap 底层原理", "tags": ["Java"], "difficulty": 3,
             "verdict": "keep", "new_stem": ""},
            {"stem": "反问：贵公司主要做什么业务", "tags": [], "difficulty": 1,
             "verdict": "delete", "new_stem": ""},
        ]}
    ])
    client = make_client(db, llm)
    body = client.post("/api/upload", json={"filename": "题.md", "content": content}).json()
    assert body["count"] == 2  # 上传时先入库 2 题

    with db:
        from sqlalchemy import text as _t
        remaining = [r[0] for r in db.exec(_t("SELECT stem FROM questions WHERE source_id = :i"),
                                           params={"i": body["source_id"]}).all()]
        assert remaining == ["讲一下 HashMap 底层原理"]  # delete 题已移除


def test_upload_direct_mode_verdict_rewrite(db):
    """后台校验：verdict=rewrite 的题用 new_stem 更新题干（自然化改写）。"""
    content = "Q: 看你项目里用过压测，怎么做的？"
    llm = FakeLLM([
        {"items": [
            {"stem": "看你项目里用过压测，怎么做的？", "tags": ["测试"], "difficulty": 2,
             "verdict": "rewrite", "new_stem": "请描述你使用压测工具的项目实践与结果。"},
        ]}
    ])
    client = make_client(db, llm)
    body = client.post("/api/upload", json={"filename": "题.md", "content": content}).json()

    with db:
        from sqlalchemy import text as _t
        stem = db.exec(_t("SELECT stem FROM questions WHERE source_id = :i"),
                       params={"i": body["source_id"]}).one()[0]
        assert stem == "请描述你使用压测工具的项目实践与结果。"


def test_upload_resume_generates_candidates(db):
    """简历上传：后台解析出 project 候选题，轮询接口返回候选列表。"""
    from app.web.routes import _resume_candidates

    content = "# 简历\n\n项目一：Agent 系统\n项目二：RAG 平台"
    llm = FakeLLM([[
        {"type": "project", "stem": "讲一下 Agent 系统的技术难点和取舍", "tags": ["Agent"],
         "difficulty": 3, "good_criteria": ["真实"], "bad_criteria": ["编造"]},
        {"type": "project", "stem": "讲一下 RAG 平台的检索优化", "tags": ["RAG"],
         "difficulty": 4, "good_criteria": ["真实"], "bad_criteria": ["编造"]},
    ]])
    client = make_client(db, llm)
    body = client.post("/api/upload", json={
        "filename": "简历.md", "content": content, "type": "resume", "count": 3,
    }).json()
    assert body["mode"] == "resume"
    token = body["token"]
    try:
        data = None
        for _ in range(100):
            data = client.get(f"/api/upload/candidates/{token}").json()
            if data["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert data["status"] == "done"
        assert len(data["items"]) == 2
        assert data["items"][0]["stem"] == "讲一下 Agent 系统的技术难点和取舍"
        assert data["items"][0]["difficulty"] == 3
    finally:
        with db:
            _resume_candidates.pop(token, None)


def test_upload_resume_confirm_cross_user_404(db):
    """resume 候选 token 绑定创建者：他人 confirm 一律 404（B7 越权拦截）。"""
    from app.web.routes import _resume_candidates

    content = "# 简历\n\n项目一：Agent 系统"
    llm = FakeLLM([[
        {"type": "project", "stem": "讲一下 Agent 系统的技术难点和取舍", "tags": ["Agent"],
         "difficulty": 3, "good_criteria": ["真实"], "bad_criteria": ["编造"]},
    ]])
    alice = make_client(db, llm, user_role="owner", username="alice")
    bob = make_client(db, FakeLLM([]), user_role="owner", username="bob")
    body = alice.post("/api/upload", json={
        "filename": "简历.md", "content": content, "type": "resume",
    }).json()
    token = body["token"]
    try:
        for _ in range(100):
            data = alice.get(f"/api/upload/candidates/{token}").json()
            if data["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert data["status"] == "done"
        resp = bob.post("/api/upload/confirm", json={
            "token": token,
            "items": [{"stem": data["items"][0]["stem"]}],
        })
        assert resp.status_code == 404
        with db:
            assert token in _resume_candidates  # 未消费：alice 自己仍可确认
        ok = alice.post("/api/upload/confirm", json={
            "token": token,
            "items": [{"stem": data["items"][0]["stem"]}],
        })
        assert ok.status_code == 200
    finally:
        with db:
            _resume_candidates.pop(token, None)


def test_upload_resume_confirm_imports(db):
    """简历候选确认：编辑后的题干入库（project 题，保留候选 tags/difficulty），重复题被去重拒收。"""
    from app.web.routes import _resume_candidates

    content = "# 简历\n\n项目一：Agent 系统"
    llm = FakeLLM([[
        {"type": "project", "stem": "讲一下 Agent 系统的技术难点和取舍", "tags": ["Agent"],
         "difficulty": 3, "good_criteria": ["真实"], "bad_criteria": ["编造"]},
    ]])
    client = make_client(db, llm)
    body = client.post("/api/upload", json={
        "filename": "简历.md", "content": content, "type": "resume",
    }).json()
    token = body["token"]
    try:
        for _ in range(100):
            data = client.get(f"/api/upload/candidates/{token}").json()
            if data["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        cand = data["items"][0]
        edited = cand["stem"] + "？请说明设计取舍。"
        resp = client.post("/api/upload/confirm", json={
            "token": token,
            "items": [
                {"stem": edited, "tags": cand["tags"], "difficulty": cand["difficulty"]},
                {"stem": cand["stem"]},  # 与已入库题重复 → dedup 拒收
            ],
        })
        assert resp.status_code == 200
        with db:
            from sqlalchemy import text as _t
            rows = []
            for n in range(100):  # confirm 为后台任务，轮询等待入库完成
                rows = db.exec(_t("SELECT stem, tags, difficulty FROM questions WHERE source_id = :i"),
                               params={"i": body["source_id"]}).all()
                if rows:
                    break
                time.sleep(0.05)
            assert sorted(r[0] for r in rows) == sorted([edited, cand["stem"]])
            by_stem = {r[0]: r for r in rows}
            assert by_stem[edited][1] == '["Agent"]'  # 候选标签保留（JSON 文本）
            assert by_stem[edited][2] == 3            # 候选难度保留
    finally:
        with db:
            _resume_candidates.pop(token, None)


def test_upload_facejing_mode_generates(db):
    content = "一面：\n面试官：缓存穿透怎么解决？"
    llm = FakeLLM([Q1_FACEJING])
    client = make_client(db, llm)
    resp = client.post("/api/upload", json={"filename": "面经.md", "content": content})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "facejing"
    with db:
        from sqlalchemy import text as _t
        n = db.exec(_t("SELECT COUNT(*) FROM questions WHERE source_id = :i"),
                    params={"i": body["source_id"]}).one()[0]
        assert n == 1


Q1_FACEJING = [
    {
        "type": "knowledge",
        "stem": "缓存穿透怎么解决？",
        "tags": ["缓存"],
        "difficulty": 2,
        "good_criteria": ["说清布隆过滤器"],
        "bad_criteria": ["说不清"],
    }
]


def test_upload_validation(db):
    client = make_client(db, FakeLLM([]))
    assert client.post("/api/upload", json={"filename": "题.json", "content": "x"}).status_code == 400
    assert client.post("/api/upload", json={"filename": "题.md", "content": ""}).status_code == 400
    assert client.post("/api/upload", json={"filename": "题.md", "content": "x" * (20 * 1024 * 1024 + 1)}).status_code == 400
    assert client.post("/api/upload", json={"filename": "题.pdf", "content_base64": "###"}).status_code == 400


# --- edge ---


def test_resume_unfinished_session(db):
    q = add_today_question(db)
    llm = FakeLLM([CONTINUE, JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "第一轮"})

    body = client.get(f"/api/sessions/{session_id}/resume").json()
    assert body["rounds_done"] == 1
    assert body["max_rounds"] == 6  # 难度 1 → 3+1*3=6


def test_resume_max_rounds_scales_with_difficulty(db):
    q = add_today_question(db)
    q.difficulty = 5
    commit(db)
    llm = FakeLLM([CONTINUE, JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "第一轮"})

    body = client.get(f"/api/sessions/{session_id}/resume").json()
    assert body["max_rounds"] == 18  # 难度 5 → 3+5*3=18


def test_resume_finished_session_409(db):
    q = add_today_question(db)
    llm = FakeLLM([FINISH, JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "回答"})
    assert client.get(f"/api/sessions/{session_id}/resume").status_code == 409


def test_daily_run_endpoint(db):
    runs = []
    llm = FakeLLM([])
    client = make_client(db, llm, runner=lambda: runs.append(1))
    resp = client.post("/api/daily/run")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert runs == [1]


# --- fail ---


def test_unknown_question_404(db):
    client = make_client(db, FakeLLM([]))
    assert client.post("/api/sessions", json={"question_id": 999, "kind": "chain"}).status_code == 404
    assert client.get("/api/questions/999").status_code == 404


def test_unknown_session_404(db):
    client = make_client(db, FakeLLM([]))
    assert client.get("/api/sessions/999").status_code == 404
    assert client.post("/api/sessions/999/answer", json={"answer": "回答内容"}).status_code == 404


def test_empty_answer_400(db):
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    for bad in ("", "   ", "a"):
        assert client.post(f"/api/sessions/{session_id}/answer", json={"answer": bad}).status_code == 400


def test_judge_failure_returns_failed_and_retryable(db):
    q = add_today_question(db)
    llm = FakeLLM([FINISH, LLMError("down"), LLMError("down"), JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "回答"})

    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["status"] == "failed"
    assert "重试" in body["message"]

    # 重试：再次作答触发重新判分
    resp = client.post(f"/api/sessions/{session_id}/answer", json={"answer": "重试判分"})
    assert resp.status_code == 200
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["status"] == "done"


def test_concurrent_answer_409(db):
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    _inflight.add(session_id)
    assert client.post(f"/api/sessions/{session_id}/answer", json={"answer": "回答内容"}).status_code == 409
    _inflight.discard(session_id)


def test_answer_after_finished_409(db):
    q = add_today_question(db)
    llm = FakeLLM([FINISH, JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "回答"})
    assert client.post(f"/api/sessions/{session_id}/answer", json={"answer": "再来一轮"}).status_code == 409


def test_invalid_kind_400(db):
    q = add_today_question(db)
    client = make_client(db, FakeLLM([]))
    assert client.post("/api/sessions", json={"question_id": q.id, "kind": "esoteric"}).status_code == 400


# --- 二进制上传（pdf/docx/doc，monkeypatch extract_text 避免真调 MinerU） ---


def _poll_upload_status(client, token, tries=200):
    data = None
    for _ in range(tries):
        data = client.get(f"/api/upload/status/{token}").json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"upload status timeout: {data}")


def test_upload_direct_dedup_skips_existing(db):
    """direct 模式重复上传：与库内归一化哈希相同的题跳过（不重复入库）。"""
    content = "Q: 讲一下 HashMap 底层原理\nQ: Redis 分布式锁"
    client = make_client(db, FakeLLM([]))
    first = client.post("/api/upload", json={"filename": "题.md", "content": content}).json()
    assert first["count"] == 2
    second = client.post("/api/upload", json={"filename": "题2.md", "content": content}).json()
    assert second["count"] == 0  # 全部重复
    third = client.post("/api/upload", json={
        "filename": "题3.md",
        "content": "Q: 讲一下 HashMap 底层原理\nQ: 缓存穿透怎么解决？",
    }).json()
    assert third["count"] == 1  # 只新增不重复的
    with db:
        from sqlalchemy import text as _t
        n = db.exec(_t("SELECT COUNT(*) FROM questions WHERE stem IN ('讲一下 HashMap 底层原理', 'Redis 分布式锁')")).one()[0]
        assert n == 2  # 库内仍只有首次导入的 2 题


def test_upload_binary_pdf_failed_friendly_error(monkeypatch, db):
    """二进制解析失败：前端拿到中文包装错误（不透传原始 stderr）。"""
    from app.web.routes import _upload_tasks

    def fake_extract(filename, data):
        raise RuntimeError("MinerU 解析失败：Traceback ... stdout stderr")

    monkeypatch.setattr("app.web.routes.extract_text", fake_extract)
    client = make_client(db, FakeLLM([]))
    body = client.post("/api/upload", json={
        "filename": "坏文件.pdf",
        "content_base64": base64.b64encode(b"x").decode(),
        "type": "auto",
    }).json()
    token = body["token"]
    try:
        data = _poll_upload_status(client, token)
        assert data["status"] == "failed"
        assert "MinerU" not in data["error"] or "解析失败（解析器异常）" in data["error"]
        assert "Traceback" not in data["error"]
    finally:
        with db:
            _upload_tasks.pop(token, None)


def test_friendly_upload_error_kinds():
    """中文错误包装：LibreOffice/MinerU/超时分类提示，未知异常保留原文。"""
    from app.web.routes import _friendly_upload_error

    assert "LibreOffice" in _friendly_upload_error(RuntimeError("未检测到 LibreOffice"))
    assert "解析器异常" in _friendly_upload_error(RuntimeError("MinerU 解析失败：xx"))
    assert "超时" in _friendly_upload_error(RuntimeError("command timed out"))
    assert "其他奇怪错误" in _friendly_upload_error(RuntimeError("其他奇怪错误"))


def test_upload_parse_serialized(monkeypatch, db):
    """并发二进制上传：MinerU 解析串行（Semaphore），解析最大并发恒为 1（防 OOM）。"""
    from app.web.routes import _upload_tasks
    import threading

    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def fake_extract(filename, data):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.15)
        with lock:
            state["active"] -= 1
        return "Q: 讲一下 HashMap 底层原理"

    monkeypatch.setattr("app.web.routes.extract_text", fake_extract)
    client = make_client(db, FakeLLM([]))
    t1 = client.post("/api/upload", json={
        "filename": "a.pdf", "content_base64": base64.b64encode(b"a").decode(), "type": "direct",
    }).json()["token"]
    t2 = client.post("/api/upload", json={
        "filename": "b.pdf", "content_base64": base64.b64encode(b"b").decode(), "type": "direct",
    }).json()["token"]
    try:
        assert _poll_upload_status(client, t1)["status"] == "done"
        assert _poll_upload_status(client, t2)["status"] == "done"
        assert state["max"] == 1  # 两个解析请求被信号量串行化
    finally:
        with db:
            _upload_tasks.pop(t1, None)
            _upload_tasks.pop(t2, None)


def test_upload_binary_pdf_direct(monkeypatch, db):
    """PDF 二进制上传：解析完成后按内容识别为 direct 模式入库。"""
    from app.web.routes import _upload_tasks

    def fake_extract(filename, data):
        return "Q: 讲一下 HashMap 底层原理\nQ: Redis 分布式锁"

    monkeypatch.setattr("app.web.routes.extract_text", fake_extract)
    client = make_client(db, FakeLLM([]))
    body = client.post("/api/upload", json={
        "filename": "题目.pdf",
        "content_base64": base64.b64encode(b"fake pdf bytes").decode(),
        "type": "auto",
    }).json()
    assert body["mode"] == "parsing"
    token = body["token"]
    try:
        data = _poll_upload_status(client, token)
        assert data["status"] == "done"
        assert data["mode"] == "direct"
        assert data["count"] == 2
    finally:
        with db:
            _upload_tasks.pop(token, None)


def test_upload_binary_pdf_facejing(monkeypatch, db):
    from app.web.routes import _upload_tasks

    def fake_extract(filename, data):
        return "一面：\n面试官：缓存穿透怎么解决？"

    monkeypatch.setattr("app.web.routes.extract_text", fake_extract)
    client = make_client(db, FakeLLM([Q1_FACEJING]))
    body = client.post("/api/upload", json={
        "filename": "面经.pdf",
        "content_base64": base64.b64encode(b"x").decode(),
        "type": "facejing",
    }).json()
    token = body["token"]
    try:
        data = _poll_upload_status(client, token)
        assert data["status"] == "done"
        assert data["mode"] == "facejing"
    finally:
        with db:
            _upload_tasks.pop(token, None)


def test_upload_binary_resume(monkeypatch, db):
    """PDF 简历上传：解析后进入候选确认流程。"""
    from app.web.routes import _resume_candidates, _upload_tasks

    def fake_extract(filename, data):
        return "# 简历\n\n项目一：Agent 系统"

    monkeypatch.setattr("app.web.routes.extract_text", fake_extract)
    llm = FakeLLM([[
        {"type": "project", "stem": "讲一下 Agent 系统的技术难点和取舍", "tags": ["Agent"],
         "difficulty": 3, "good_criteria": ["真实"], "bad_criteria": ["编造"]},
    ]])
    client = make_client(db, llm)
    body = client.post("/api/upload", json={
        "filename": "简历.pdf",
        "content_base64": base64.b64encode(b"x").decode(),
        "type": "resume",
        "count": 3,
    }).json()
    token = body["token"]
    cand_token = None
    try:
        data = _poll_upload_status(client, token)
        assert data["status"] == "done"
        assert data["mode"] == "resume"
        cand_token = data["candidates_token"]
        for _ in range(200):
            cand = client.get(f"/api/upload/candidates/{cand_token}").json()
            if cand["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert cand["status"] == "done"
        assert cand["items"][0]["stem"] == "讲一下 Agent 系统的技术难点和取舍"
    finally:
        with db:
            _upload_tasks.pop(token, None)
            if cand_token:
                _resume_candidates.pop(cand_token, None)


# --- 数据备份：导出/导入 ---


def test_export_backup_zip(db):
    """导出：zip 含 interview.db + meta.json，可解压且库可读。"""
    import io
    import sqlite3
    import zipfile

    add_today_question(db, stem="备份测试题", tags=["Java"])
    client = make_client(db, FakeLLM([]))
    resp = client.get("/api/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert "interview.db" in zf.namelist()
    assert "meta.json" in zf.namelist()
    db_bytes = zf.read("interview.db")
    with sqlite3.connect(":memory:") as conn:
        conn.execute("ATTACH DATABASE ':memory:' AS x")
        tmp = conn.execute
        # 直接用内存库读字节：写临时文件验证
        import tempfile
        from pathlib import Path

        p = Path(tempfile.mkdtemp()) / "check.db"
        p.write_bytes(db_bytes)
        with sqlite3.connect(str(p)) as c:
            n = c.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        assert n == 1


def test_import_backup_restores(tmp_path):
    """导入：备份 zip 恢复后题数变化；当前库先备份到 data/backup/。

    用独立引擎生命周期（不共享 db fixture 的 session 连接，避免 Windows 文件锁）。
    """
    import base64
    import io
    import sqlite3
    import zipfile

    from app.db import close as _close
    from app.db import get_session as _gs
    from app.db import init_db as _init
    from app.models import Source, SourceType

    real_db = tmp_path / "real.db"
    other_db = tmp_path / "other.db"
    _init(f"sqlite:///{real_db}")
    try:
        with _gs() as s:
            src = Source(type=SourceType.manual, source_hash="pre", cleaned_text="x")
            s.add(src)
            commit(s)
            s.refresh(src)
            s.add(Question(source_id=src.id, type=QuestionType.knowledge,
                           stem="导入前题", tags=[], good_criteria=[], bad_criteria=[]))
            commit(s)
        _close()
        _init(f"sqlite:///{other_db}")
        with _gs() as s:
            src = Source(type=SourceType.manual, source_hash="bh", cleaned_text="x")
            s.add(src)
            commit(s)
            s.refresh(src)
            s.add(Question(source_id=src.id, type=QuestionType.knowledge,
                           stem="恢复出来的题", tags=[], good_criteria=[], bad_criteria=[]))
            commit(s)
        _close()
        _init(f"sqlite:///{real_db}")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(other_db, "interview.db")
        payload = base64.b64encode(buf.getvalue()).decode()

        client = make_client(None, FakeLLM([]))
        resp = client.post("/api/import", json={"content_base64": payload})
        assert resp.status_code == 200
        body = resp.json()
        assert body["restored"] is True
        assert body["questions"] == 1
        with _gs() as s:
            from sqlalchemy import select as _sel

            stems = [q.stem for q in s.scalars(_sel(Question))]
            assert stems == ["恢复出来的题"]
    finally:
        _close()


def test_import_backup_invalid(db):
    client = make_client(db, FakeLLM([]))
    assert client.post("/api/import", json={"content_base64": "###"}).status_code == 400
    import base64
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("其他文件.txt", "x")
    assert client.post("/api/import", json={
        "content_base64": base64.b64encode(buf.getvalue()).decode(),
    }).status_code == 400


def test_import_backup_rejects_old_single_user_format(db):
    """旧单用户备份（无 users/user_tokens/user_picks）→ 400 拒绝（B8）。"""
    import base64
    import io
    import sqlite3
    import zipfile

    tmp = sqlite3.connect(":memory:")
    for t in ("questions", "sources", "sessions", "attempts", "judgments", "task_logs"):
        tmp.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
    tmp.commit()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("interview.db", tmp.serialize())

    client = make_client(db, FakeLLM([]))
    resp = client.post("/api/import", json={
        "content_base64": base64.b64encode(buf.getvalue()).decode(),
    })
    assert resp.status_code == 400
    assert "用户数据" in resp.json()["detail"]


def test_import_backup_keeps_login_for_same_user(tmp_path):
    """导入备份含当前用户 → 返回新 token，刷新后登录态保持（体验修复）。"""
    import base64
    import io
    import sqlite3
    import zipfile

    from app.auth import hash_password
    from app.db import close as _close
    from app.db import get_session as _gs
    from app.db import init_db as _init
    from app.models import Source, SourceType

    real_db = tmp_path / "real.db"
    other_db = tmp_path / "other.db"
    _init(f"sqlite:///{real_db}")
    try:
        client = make_client(None, FakeLLM([]))  # 创建 testuser（owner）
        _close()
        _init(f"sqlite:///{other_db}")
        with _gs() as s:
            s.add(User(username="testuser", password_hash=hash_password("testpass1"), role="owner"))
            commit(s)
            src = Source(type=SourceType.manual, source_hash="bh", cleaned_text="x")
            s.add(src)
            commit(s)
            s.refresh(src)
            s.add(Question(source_id=src.id, type=QuestionType.knowledge,
                           stem="恢复题", tags=[], good_criteria=[], bad_criteria=[]))
            commit(s)
        _close()
        _init(f"sqlite:///{real_db}")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(other_db, "interview.db")
        resp = client.post("/api/import", json={
            "content_base64": base64.b64encode(buf.getvalue()).decode(),
        })
        assert resp.status_code == 200
        new_token = resp.json().get("new_token")
        assert new_token
        assert client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {new_token}",
        }).status_code == 200
    finally:
        _close()


def test_default_embedder_factory_singleton():
    """默认 embedder 工厂返回同一实例（防并发答题/上传重复加载 bge-m3 打满 4C8G）。"""
    from app.web.routes import _default_embedder_factory

    factory = _default_embedder_factory()
    assert factory() is factory()


def test_sqlite_busy_timeout_set(db):
    """连接级 PRAGMA busy_timeout=5000 生效（WAL 下并发写等待而非立即报 locked）。"""
    from sqlalchemy import text as _t

    from app.db import engine

    with engine().connect() as conn:
        assert conn.execute(_t("PRAGMA busy_timeout")).scalar() == 5000
