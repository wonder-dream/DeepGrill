"""接口缺陷修复的回归测试（对应 docs/接口缺陷清单-20260913.md）。

每条用例先复现原缺陷（注释给出修复前行为），再断言修复后的行为。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import commit, get_session, init_db
from app.models import Question, QuestionType, Source, SourceType, User
from app.web.routes import create_app
from tests.fakes import FakeEmbedder, FakeLLM
from tests.test_routes import add_today_question, make_client, make_config


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    with get_session() as session:
        yield session


def _fresh_get(model, pk):
    """用新会话查一行（避免测试会话 identity map 里被别处删掉的旧实例干扰）。"""
    with get_session() as s:
        return s.get(model, pk)


def _client(db, role="owner", username="testuser"):
    return make_client(db, FakeLLM([]), user_role=role, username=username)


# --- P0-1/2：整数参数超 SQLite int64 上限 → 原本 OverflowError 500 ---


def test_bank_page_upper_bound_400(db):
    """GET /api/bank?page=INT64_MAX 原本 500（offset 溢出）→ 现在 400。"""
    c = _client(db)
    for page in ("9223372036854775807", "99999999999999999999", "100000001"):
        resp = c.get("/api/bank", params={"page": page})
        assert resp.status_code == 400, page
        assert resp.json()["detail"] == "invalid page or page_size"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/questions/{big}"),
    ("GET", "/api/questions/{big}/history"),
    ("GET", "/api/questions/{big}/similar"),
    ("GET", "/api/sessions/{big}"),
    ("GET", "/api/sessions/{big}/resume"),
    ("GET", "/api/ugc/submissions/{big}"),
    ("PUT", "/api/admin/questions/{big}"),
    ("DELETE", "/api/admin/questions/{big}"),
    ("DELETE", "/api/admin/tags/{big}"),
    ("DELETE", "/api/admin/categories/{big}"),
    ("POST", "/api/admin/feedback/{big}/resolve"),
    ("POST", "/api/favorites/{big}"),
])
def test_oversized_id_never_500(db, method, path):
    """超 int64 的资源 id 原本在驱动层 OverflowError → 500；现在一律 400/404。"""
    c = _client(db)
    resp = c.request(method, path.format(big="99999999999999999999"))
    assert resp.status_code in (400, 404), (method, path, resp.status_code, resp.text[:120])


def test_favorites_oversized_id_400(db):
    c = _client(db)
    assert c.post("/api/favorites/99999999999999999999").status_code == 400


def test_review_suggestions_oversized_id_400(db):
    """?ids=<超大整数> 原本 500（绑定溢出）→ 现在忽略非法项返回空建议。"""
    c = _client(db)
    resp = c.get("/api/review/suggestions", params={"ids": "99999999999999999999"})
    assert resp.status_code == 200
    assert resp.json() == {"suggestions": {}}


def test_repeated_query_param_oversized_400(db):
    """?page=1&page=<超大> 重复参数原本 500 → 现在 400。"""
    c = _client(db)
    resp = c.get("/api/bank?page=1&page=99999999999999999999")
    assert resp.status_code == 400


# --- P0-3：date 溢出 + 解析口径 ---


def test_today_date_overflow_400(db):
    """date=9999-12-31 原本 +1 天 OverflowError → 500；现在 400。"""
    c = _client(db)
    for bad in ("9999-12-31", "9999-12-30", "2099-01-01"):
        assert c.get("/api/today", params={"date": bad}).status_code == 400, bad


def test_today_date_strict_iso(db, tmp_path):
    """非补零 `2026-9-3` 与带时间的 `2026-09-13T00:00:00` 都被拒（只收 YYYY-MM-DD）。"""
    c = _client(db)
    for bad in ("2026-9-3", "2026-09-13T00:00:00", "2026-09-13 ", "20260913"):
        assert c.get("/api/today", params={"date": bad}).status_code == 400, bad
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    assert c.get("/api/today", params={"date": today}).status_code == 200


# --- P0-4/5、P1-6：admin 改题的 null 语义 ---


def test_admin_update_null_rejected_400(db):
    """stem/difficulty/criteria 传 null 原本 500（且 stem 那次绕开异常处理器）；现在 400。"""
    q = add_today_question(db, stem="空值语义测试题")
    c = _client(db)
    for body in ({"stem": None}, {"difficulty": None}, {"good_criteria": None},
                 {"bad_criteria": None}, {"tags": None}, {"reviewed": None}):
        resp = c.put(f"/api/admin/questions/{q.id}", json=body)
        assert resp.status_code == 400, (body, resp.status_code, resp.text[:120])
        assert "detail" in resp.json()


def test_attribute_error_becomes_400_json(db):
    """请求体类型畸形时不再返回纯文本 500：统一 400 + JSON detail。"""
    c = _client(db)
    resp = c.put("/api/admin/questions/1", json={"stem": 123})
    assert resp.status_code == 400, resp.text[:120]
    assert isinstance(resp.json().get("detail"), str)


def test_admin_update_reviewed_null_does_not_unpublish(db):
    """reviewed=null 原本 200 且把题目静默下架；现在 400 且 reviewed_at 不变。"""
    q = add_today_question(db, stem="下架保护测试题")
    assert q.reviewed_at is not None
    c = _client(db)
    assert c.put(f"/api/admin/questions/{q.id}", json={"reviewed": None}).status_code == 400
    db.expire_all()
    assert db.get(Question, q.id).reviewed_at is not None


def test_admin_update_difficulty_null_keeps_row_intact(db):
    """difficulty=null 原本 500（NOT NULL 违规）；现在 400 且原值保留。"""
    q = add_today_question(db, stem="难度空值测试题")
    before = q.difficulty
    c = _client(db)
    assert c.put(f"/api/admin/questions/{q.id}", json={"difficulty": None}).status_code == 400
    db.expire_all()
    assert db.get(Question, q.id).difficulty == before


def test_admin_update_ok_paths_still_work(db):
    """省略字段=不改；正常值仍可写入。"""
    q = add_today_question(db, stem="正常改题测试题")
    c = _client(db)
    resp = c.put(f"/api/admin/questions/{q.id}", json={"difficulty": 4, "stem": "改写后的题干内容"})
    assert resp.status_code == 200
    assert resp.json()["difficulty"] == 4 and resp.json()["stem"] == "改写后的题干内容"
    # reviewed=True 仍可正常下架/上架
    assert c.put(f"/api/admin/questions/{q.id}", json={"reviewed": False}).status_code == 200
    db.expire_all()
    assert db.get(Question, q.id).reviewed_at is None
    assert c.put(f"/api/admin/questions/{q.id}", json={"reviewed": True}).status_code == 200


# --- P1-7：限流额度只计真正执行的请求 ---


def test_rejected_requests_do_not_consume_quota(db):
    """被业务层拒绝（4xx）的写请求不占额度：否则 10 次非法上传就能锁死合法上传。

    走完整登录链路（Cookie 认证）拿到 owner，再用恒 400 的非法后缀请求验证额度未被吃光。
    """
    from app import ratelimit
    from app.auth import hash_password
    from app.models import User as _U

    with get_session() as s:
        s.add(_U(email="quota@test.com", username="quota",
                 password_hash=hash_password("quotapass1"), role="owner"))
        commit(s)

    app = create_app(make_config(), llm_factory=lambda role: FakeLLM([]),
                     daily_runner=None, embedder_factory=lambda: FakeEmbedder())
    client = TestClient(app)
    assert client.post("/api/auth/login", json={
        "email": "quota@test.com", "password": "quotapass1"}).status_code == 200
    bad = {"filename": "a.exe", "content": "x"}
    ratelimit.reset()
    for _ in range(40):  # 远超 LLM_COST_LIMIT(10)
        resp = client.post("/api/upload", json=bad, headers={"X-Requested-With": "fetch"})
        assert resp.status_code == 400, resp.text[:120]
    assert client.post("/api/upload", json=bad,
                       headers={"X-Requested-With": "fetch"}).status_code == 400


def test_rate_limit_still_blocks_after_real_requests(db):
    """真正执行的写请求照常计数（成功响应累计到上限后 429）。"""
    from app import ratelimit

    c = _client(db)
    q = add_today_question(db, stem="限流计数测试题")
    ratelimit.reset()
    codes = [c.post(f"/api/favorites/{q.id}").status_code for _ in range(62)]
    assert codes.count(200) == 60
    assert codes[-1] == 429


# --- P1-8：验证码冷却 ---


def test_send_code_failure_still_cools_down(monkeypatch, db):
    """SMTP 失败也占 60s 冷却（修复前失败不留 last_sent → 可无限重发）。"""
    from app import email_code

    def boom(email, code):
        raise RuntimeError("SMTP 未配置")

    monkeypatch.setattr(email_code, "send_smtp_code", boom)
    c = _client(db)
    assert c.post("/api/auth/send-code", json={"email": "x@163.com"}).status_code == 503
    assert c.post("/api/auth/send-code", json={"email": "x@163.com"}).status_code == 429


# --- P1-9：并发建同名标签/分类 → 409 而非 500 ---


def test_duplicate_category_conflict_409(db):
    c = _client(db)
    assert c.post("/api/admin/categories", json={"name": "并发分类"}).status_code == 200
    resp = c.post("/api/admin/categories", json={"name": "并发分类"})
    assert resp.status_code == 409


def test_duplicate_tag_conflict_409(db):
    c = _client(db)
    assert c.post("/api/admin/tags", json={"name": "并发标签", "category_id": 1}).status_code == 200
    assert c.post("/api/admin/tags", json={"name": "并发标签", "category_id": 1}).status_code == 409


# --- P1-10：上传文件名归一化 ---


def test_upload_filename_traversal_normalized(db):
    """`../../evil.md` 原本按后缀放行且原样入库；现在只取 basename。"""
    c = _client(db)
    resp = c.post("/api/upload", json={"filename": "../../evil.md",
                                       "content": "Q: 路径穿越归一化测试题"})
    assert resp.status_code == 200
    from app.models import Source as _S

    source = db.scalars(select(_S).order_by(_S.id.desc())).first()
    assert source.title == "evil.md"
    assert "/" not in source.title and "\\" not in source.title


def test_upload_filename_overlong_400(db):
    c = _client(db)
    resp = c.post("/api/upload", json={"filename": "a" * 500 + ".md", "content": "Q: 超长文件名测试"})
    assert resp.status_code == 400


# --- P2-12：LIKE 元字符按字面量 ---


def test_bank_search_wildcard_is_literal(db):
    """q=% / q=_ 原本是 LIKE 通配（整个题库命中）→ 现在只做字面量匹配。"""
    add_today_question(db, stem="Buffer 与缓冲区设计题")
    add_today_question(db, stem="含百分号%的题目")
    add_today_question(db, stem="含下划线_的题目")
    c = _client(db)
    assert c.get("/api/bank").json()["total"] == 3
    assert c.get("/api/bank", params={"q": "%"}).json()["total"] == 1  # 修复前 = 3
    assert c.get("/api/bank", params={"q": "_"}).json()["total"] == 1  # 修复前 = 3
    assert c.get("/api/bank", params={"q": "Buffer"}).json()["total"] == 1
    assert c.get("/api/bank", params={"q": "不含的关键词"}).json()["total"] == 0


# --- P2-13：suggestions 条数上限 ---


def test_review_suggestions_limited(db):
    from app.web.routes import SUGGESTIONS_MAX_IDS

    for i in range(5):
        add_today_question(db, stem=f"审核建议上限题{i}")
    c = _client(db)
    ids = ",".join(str(i) for i in range(1, SUGGESTIONS_MAX_IDS + 50))
    resp = c.get("/api/review/suggestions", params={"ids": ids})
    assert resp.status_code == 200
    assert len(resp.json()["suggestions"]) <= SUGGESTIONS_MAX_IDS


# --- P2-14：stats 口径一致 ---


def test_stats_done_questions_excludes_failed_judgment(db):
    """判分失败的会话原本计入 done_questions 但不计入 answered_total → 口径不一致。"""
    from datetime import datetime

    from app.models import Judgment, Session, SessionKind, SessionStatus

    q = add_today_question(db, stem="判分失败口径测试题")
    user = db.scalars(select(User).where(User.email == "testuser@test.com")).first()
    s = Session(question_id=q.id, user_id=user.id, kind=SessionKind.open,
                status=SessionStatus.finished, ended_at=datetime.now())
    db.add(s)
    commit(db)
    db.refresh(s)
    db.add(Judgment(session_id=s.id, scores={"status": "failed"}, total_score=None,
                    review="判分失败", reference_answer="", weak_tags=[]))
    commit(db)

    c = _client(db)
    body = c.get("/api/stats").json()
    assert body["done_questions"] == body["answered_total"] == 0


# --- P3-18：质检不自动删已审核题 ---


def test_quality_check_skips_reviewed_question(db):
    """verdict=delete 原本连已审核题（可能已被作答）一起删；现在只删未审核题。"""
    from datetime import datetime

    from app.web.routes import _tag_direct_questions

    source = Source(type=SourceType.manual, source_hash="qc-1", cleaned_text="x")
    db.add(source)
    commit(db)
    db.refresh(source)
    reviewed = Question(source_id=source.id, type=QuestionType.knowledge, stem="已审核的题",
                        good_criteria=[], bad_criteria=[], reviewed_at=datetime.now())
    pending = Question(source_id=source.id, type=QuestionType.knowledge, stem="待审核的题",
                       good_criteria=[], bad_criteria=[])
    db.add_all([reviewed, pending])
    commit(db)
    db.refresh(reviewed)
    db.refresh(pending)

    llm = FakeLLM([{"items": [
        {"stem": "已审核的题", "tags": [], "difficulty": 1, "verdict": "delete", "new_stem": ""},
        {"stem": "待审核的题", "tags": [], "difficulty": 1, "verdict": "delete", "new_stem": ""},
    ]}])
    _tag_direct_questions(source.id, lambda role: llm)

    assert _fresh_get(Question, reviewed.id) is not None  # 已审核题保留
    assert _fresh_get(Question, pending.id) is None  # 待审核题按质检结果删除


# --- 回归：正常链路未被上述改动影响 ---


def test_today_and_bank_normal_path(db):
    q = add_today_question(db, stem="链路回归测试题", tags=["Java"])
    c = _client(db)
    assert c.get("/api/today").status_code == 200
    assert c.get(f"/api/questions/{q.id}").status_code == 200
    assert c.get("/api/bank", params={"q": "链路回归"}).json()["total"] == 1
