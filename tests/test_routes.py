import math
import pytest
from datetime import datetime
from fastapi.testclient import TestClient

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


def make_client(db, llm, runner=None, embedder=None, **overrides):
    embedder = embedder or FakeEmbedder()
    app = create_app(
        make_config(),
        llm_factory=lambda role: llm,
        daily_runner=runner,
        embedder_factory=lambda: embedder,
    )
    return TestClient(app)


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


def add_today_question(db, stem="讲一下 HashMap 底层原理", status_today=True, tags=None, qtype=QuestionType.knowledge):
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


def test_today_empty_list(db):
    client = make_client(db, FakeLLM([]))
    assert client.get("/api/today").json() == []


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

    items = client.get("/api/review", params={"tag": "RAG"}).json()
    assert [i["stem"] for i in items] == ["未做的 RAG 题", "已做的 RAG 题"]  # 未做优先
    assert items[0]["done"] is False
    assert items[1]["done"] is True
    assert items[1]["total_score"] == 76


def test_review_excludes_other_tags(db):
    add_today_question(db, stem="RAG 题", tags=["RAG"])
    add_today_question(db, stem="Java 题", tags=["Java"])
    add_today_question(db, stem="RAG 检索题", tags=["RAG", "检索"])
    client = make_client(db, FakeLLM([]))
    items = client.get("/api/review", params={"tag": "RAG"}).json()
    assert len(items) == 2  # tags 数组含 "RAG" 元素的才命中，"Java 题"排除
    assert all("RAG" in i["tags"] for i in items)


def test_review_invalid_tag_400(db):
    client = make_client(db, FakeLLM([]))
    resp = client.get("/api/review", params={"tag": "深度不足"})
    assert resp.status_code == 400


def test_review_no_questions_empty(db):
    client = make_client(db, FakeLLM([]))
    assert client.get("/api/review", params={"tag": "Agent"}).json() == []


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
    assert len(tags) == 58  # 全部词表标签
    # 有计数的排最前
    counts = [t["count"] for t in tags]
    assert counts[0] == 2 and counts[1] == 1
    assert all(c == 0 for c in counts[2:])


def test_review_tags_empty_db(db):
    client = make_client(db, FakeLLM([]))
    tags = client.get("/api/review/tags").json()
    assert len(tags) == 58
    assert all(t["count"] == 0 for t in tags)


def test_tag_categories_structure(db):
    """GET /api/tags：分类结构 + 标签总数与词表一致。"""
    from app.tags import TAG_CATEGORIES, TAG_VOCABULARY

    client = make_client(db, FakeLLM([]))
    cats = client.get("/api/tags").json()
    assert [c["name"] for c in cats] == [name for name, _ in TAG_CATEGORIES]
    flat = [t for c in cats for t in c["tags"]]
    assert flat == list(TAG_VOCABULARY)
    assert len(flat) == 58


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


# --- edge ---


def test_resume_unfinished_session(db):
    q = add_today_question(db)
    llm = FakeLLM([CONTINUE, JUDGMENT])
    client = make_client(db, llm)
    session_id = client.post("/api/sessions", json={"question_id": q.id, "kind": "chain"}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/answer", json={"answer": "第一轮"})

    body = client.get(f"/api/sessions/{session_id}/resume").json()
    assert body["rounds_done"] == 1
    assert body["max_rounds"] == 20


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
