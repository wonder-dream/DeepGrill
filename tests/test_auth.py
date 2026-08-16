"""多用户认证与数据隔离测试（阶段 A）。"""
import hashlib
import time
import pytest
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import MAX_USERS, hash_password
from app.db import commit, get_session, init_db
from app.models import Question, QuestionType, Session, SessionKind, Source, SourceType, User, UserFavorite, UserPick
from app.web.routes import create_app
from tests.fakes import FakeEmbedder, FakeLLM

from tests.test_routes import make_config


@pytest.fixture
def db(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    with get_session() as session:
        yield session


def bare_client(llm=None):
    """无 token 的裸客户端（测 401/注册）。"""
    app = create_app(
        make_config(),
        llm_factory=lambda role: llm or FakeLLM([]),
        daily_runner=None,
        embedder_factory=lambda: FakeEmbedder(),
    )
    return TestClient(app)


def authed_client(account="testuser", role="owner"):
    """按显示名构造用户（email 自动 account@test.com），返回带 token 的客户端。"""
    from app.auth import create_token

    app = create_app(
        make_config(),
        llm_factory=lambda role: FakeLLM([]),
        daily_runner=None,
        embedder_factory=lambda: FakeEmbedder(),
    )
    email = f"{account}@test.com"
    with get_session() as session:
        user = session.scalars(select(User).where(User.email == email)).first()
        if user is None:
            user = User(email=email, username=account, password_hash=hash_password("pass1234"), role=role)
            session.add(user)
            commit(session)
            session.refresh(user)
        token = create_token(user.id)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def register(client, email="alice@163.com", password="pass1234", code="123456"):
    """邮箱注册：直插固定验证码 123456 到内存（跳过真实 SMTP），请求用传入的 code。"""
    from app import email_code

    email = email.lower()
    with email_code._codes_lock:
        email_code._codes[email] = {
            "code": "123456", "ts": time.time(), "last_sent": 0, "attempts": 0,
        }
    return client.post(
        "/api/auth/register",
        json={"email": email, "code": code, "password": password},
    )


# --- 验证码 / 注册 / 登录 ---


def test_send_code_sends_and_stores(monkeypatch, db):
    """send-code：SMTP 发送被替换后正常落码（真实发送函数被 mock）。"""
    from app import email_code

    sent = []

    def fake_send(email, code):
        sent.append((email, code))

    monkeypatch.setattr(email_code, "send_smtp_code", fake_send)
    resp = bare_client().post("/api/auth/send-code", json={"email": "new@163.com"})
    assert resp.status_code == 200
    assert len(sent) == 1 and sent[0][0] == "new@163.com"
    with email_code._codes_lock:
        entry = email_code._codes["new@163.com"]
    assert entry["code"] == sent[0][1] and len(entry["code"]) == 6


def test_send_code_invalid_email_400(db):
    c = bare_client()
    assert c.post("/api/auth/send-code", json={"email": "not-an-email"}).status_code == 400
    assert c.post("/api/auth/send-code", json={"email": ""}).status_code == 400


def test_send_code_rate_limit_429(monkeypatch, db):
    from app import email_code

    monkeypatch.setattr(email_code, "send_smtp_code", lambda email, code: None)
    c = bare_client()
    assert c.post("/api/auth/send-code", json={"email": "a@163.com"}).status_code == 200
    assert c.post("/api/auth/send-code", json={"email": "a@163.com"}).status_code == 429  # 60s 内限频


def test_send_code_smtp_unconfigured_503(monkeypatch, db):
    """SMTP 未配置（发送抛 RuntimeError）→ 503 且不留码。"""
    from app import email_code

    def boom(email, code):
        raise RuntimeError("SMTP 未配置")

    monkeypatch.setattr(email_code, "send_smtp_code", boom)
    c = bare_client()
    assert c.post("/api/auth/send-code", json={"email": "b@163.com"}).status_code == 503
    with email_code._codes_lock:
        assert "b@163.com" not in email_code._codes


def test_register_first_user_is_owner(db):
    resp = register(bare_client())
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["role"] == "owner"
    assert body["user"]["username"] == "alice"  # 显示名 = 邮箱前缀
    assert body["token"]


def test_register_second_user_is_user(db):
    c = bare_client()
    register(c, "alice@163.com")
    body = register(c, "bob@163.com").json()
    assert body["user"]["role"] == "user"


def test_register_duplicate_email_409(db):
    c = bare_client()
    register(c, "alice@163.com")
    assert register(c, "alice@163.com").status_code == 409


def test_register_wrong_code_400(db):
    c = bare_client()
    assert register(c, "alice@163.com", code="000000").status_code == 400  # 码错误
    assert c.get("/api/auth/me").status_code == 401


def test_register_code_consumed_after_use(db):
    """验证码用后即删：注册成功后同码不可复用。"""
    from app import email_code

    c = bare_client()
    assert register(c, "alice@163.com").status_code == 200
    with email_code._codes_lock:
        assert "alice@163.com" not in email_code._codes


def test_register_weak_password_400(db):
    c = bare_client()
    assert register(c, "alice@163.com", "onlyletters").status_code == 400  # 无数字
    assert register(c, "alice@163.com", "12345678").status_code == 400  # 无字母
    assert register(c, "alice@163.com", "short1").status_code == 400  # 过短
    assert register(c, "alice@163.com", "goodpass1").status_code == 200


def test_register_quota_full_409(db):
    c = bare_client()
    register(c, "alice@163.com")
    for i in range(MAX_USERS):
        register(c, f"user{i}@163.com")
    # MAX_USERS 个 user 已满（alice 是 owner 不计入）
    assert register(c, "overflow@163.com").status_code == 409


def test_concurrent_register_single_owner(db):
    """并发注册（双线程）→ 仅 1 个 owner，无双 owner 竞态（B4）。"""
    import threading

    results = []
    barrier = threading.Barrier(2)

    def reg(name):
        barrier.wait()
        c = bare_client()
        resp = register(c, f"{name}@163.com")
        results.append((name, resp.status_code, resp.json().get("user", {}).get("role")))

    threads = [threading.Thread(target=reg, args=(n,)) for n in ("carol", "dave")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    roles = [r[2] for r in results]
    assert roles.count("owner") == 1  # 只有一个 owner（另一个是 user）
    with get_session() as s:
        owners = len(s.scalars(select(User).where(User.role == "owner")).all())
    assert owners == 1


def test_login_success_and_wrong_password(db):
    c = bare_client()
    register(c, "alice@163.com", "pass1234")
    ok = c.post("/api/auth/login", json={"email": "alice@163.com", "password": "pass1234"})
    assert ok.status_code == 200
    assert ok.json()["token"]
    bad = c.post("/api/auth/login", json={"email": "alice@163.com", "password": "wrong123"})
    assert bad.status_code == 401


def test_login_rate_limit_429(db):
    """同 IP 连续 5 次登录失败后，第 6 次请求 429。"""
    c = bare_client()
    register(c, "alice@163.com", "pass1234")
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "alice@163.com", "password": "wrong123"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "alice@163.com", "password": "wrong123"}).status_code == 429


def test_login_rate_limit_reset_on_success(db):
    """成功登录清零失败计数：4 次失败 + 1 次成功后可再失败 5 次才触发 429。"""
    c = bare_client()
    register(c, "alice@163.com", "pass1234")
    for _ in range(4):
        c.post("/api/auth/login", json={"email": "alice@163.com", "password": "wrong123"})
    assert c.post("/api/auth/login", json={"email": "alice@163.com", "password": "pass1234"}).status_code == 200
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "alice@163.com", "password": "wrong123"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "alice@163.com", "password": "wrong123"}).status_code == 429


def test_register_rate_limit_429(db):
    """注册接口同样限速：连续 5 次注册失败（重名 409）后 429。"""
    c = bare_client()
    register(c, "alice@163.com", "pass1234")
    for _ in range(5):
        assert register(c, "alice@163.com").status_code == 409
    assert register(c, "alice@163.com").status_code == 429


def test_me_and_logout(db):
    c = bare_client()
    token = register(c, "alice@163.com").json()["token"]
    me = c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["username"] == "alice"
    c.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_expiry_401_and_lazy_cleanup(db):
    """过期 token：401 且过期行被惰性删除（user_tokens 不无限增长）。"""
    from datetime import timedelta

    from app.models import UserToken

    c = bare_client()
    token = register(c, "alice@163.com").json()["token"]
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with get_session() as s:
        row = s.scalars(select(UserToken).where(UserToken.token_hash == token_hash)).one()
        row.expires_at = datetime.now() - timedelta(seconds=1)  # 强制过期
        commit(s)

    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    with get_session() as s:
        assert s.scalars(
            select(UserToken).where(UserToken.token_hash == token_hash)
        ).first() is None  # 过期行已删除


def test_token_not_expired_still_valid(db):
    """未过期 token 正常放行（expires_at 在将来）。"""
    c = bare_client()
    token = register(c, "alice@163.com").json()["token"]
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_unauthorized_401(db):
    c = bare_client()
    assert c.get("/api/today").status_code == 401
    assert c.get("/api/bank").status_code == 401
    assert c.get("/api/questions/1").status_code == 401  # 题目详情匿名不可读（D2）
    assert c.post("/api/sessions", json={"question_id": 1, "kind": "open"}).status_code == 401


# --- 数据隔离 ---


def _add_question(db, stem):
    src = Source(type=SourceType.manual, source_hash=f"h-{stem}", cleaned_text="x")
    db.add(src)
    commit(db)
    db.refresh(src)
    q = Question(source_id=src.id, type=QuestionType.knowledge, stem=stem,
                 tags=["Java"], good_criteria=["完整"], bad_criteria=["答非所问"],
                 reviewed_at=datetime.now())  # 测试默认已审核（可见）
    db.add(q)
    commit(db)
    db.refresh(q)
    return q


def test_session_isolation(db):
    """A 用户的会话 B 不可见（404 越权拦截）。"""
    q = _add_question(db, "隔离题")
    user_a = User(email="alice@test.com", username="alice", password_hash=hash_password("pass1234"), role="user")
    db.add(user_a)
    commit(db)
    db.refresh(user_a)
    s = Session(question_id=q.id, user_id=user_a.id, kind=SessionKind.open)
    db.add(s)
    commit(db)
    db.refresh(s)

    bob = authed_client("bob", role="user")
    assert bob.get(f"/api/sessions/{s.id}").status_code == 404
    assert bob.post(f"/api/sessions/{s.id}/answer", json={"answer": "回答内容"}).status_code == 404
    assert bob.get(f"/api/sessions/{s.id}/resume").status_code == 404
    assert bob.delete(f"/api/sessions/{s.id}").status_code == 404


def test_history_and_bank_done_isolation(db):
    """done 标志按用户隔离：A 答过（finished+judgment），B 仍显示未做。"""
    from app.models import Judgment, SessionStatus

    q = _add_question(db, "隔离题")
    with get_session() as s:
        user_a = User(email="alice@test.com", username="alice", password_hash=hash_password("pass1234"), role="user")
        s.add(user_a)
        commit(s)
        s.refresh(user_a)
        ss = Session(question_id=q.id, user_id=user_a.id, kind=SessionKind.open,
                     status=SessionStatus.finished, ended_at=datetime.now())
        s.add(ss)
        commit(s)
        s.refresh(ss)
        s.add(Judgment(session_id=ss.id, scores={"status": "ok"}, total_score=90,
                       review="好", reference_answer="答", weak_tags=["Java"]))
        commit(s)

    alice = authed_client("alice", role="user")
    bob = authed_client("bob", role="user")
    alice_items = alice.get("/api/bank", params={"page_size": 50}).json()["items"]
    bob_items = bob.get("/api/bank", params={"page_size": 50}).json()["items"]
    assert alice_items[0]["done"] is True
    assert bob_items[0]["done"] is False


def test_today_picks_per_user(db):
    """今日选题懒加载：两个用户各自独立选题（不共享 picks）。"""
    for i in range(6):
        _add_question(db, f"题{i}")
    alice = authed_client("alice", role="user")
    bob = authed_client("bob", role="user")
    a_items = alice.get("/api/today").json()
    b_items = bob.get("/api/today").json()
    assert len(a_items) == 3  # knowledge 配额 3（6 题 pending 取 3）
    assert len(b_items) == 3
    # 各自独立：互不影响
    with get_session() as s:
        a_picks = len(s.scalars(select(UserPick).where(UserPick.user_id == 1)).all())
        b_picks = len(s.scalars(select(UserPick).where(UserPick.user_id == 2)).all())
    assert a_picks == 3 and b_picks == 3


# --- 管理员题库管理 ---


def test_admin_edit_question(db):
    q = _add_question(db, "管理题")
    owner = authed_client("owner", role="owner")
    resp = owner.put(f"/api/admin/questions/{q.id}", json={
        "stem": "修改后的题干", "difficulty": 4, "tags": ["Java", "分布式"],
    })
    assert resp.status_code == 200
    assert resp.json()["stem"] == "修改后的题干"
    assert resp.json()["difficulty"] == 4
    assert resp.json()["tags"] == ["Java", "分布式"]
    assert owner.put(f"/api/admin/questions/{q.id}", json={"difficulty": 9}).status_code == 400


def test_admin_delete_question(db):
    q = _add_question(db, "待删题")
    with get_session() as s:
        user_a = User(email="alice@test.com", username="alice", password_hash=hash_password("pass1234"), role="user")
        s.add(user_a)
        commit(s)
        s.refresh(user_a)
        s.add(Session(question_id=q.id, user_id=user_a.id, kind=SessionKind.open))
        s.add(UserPick(user_id=user_a.id, question_id=q.id))
        s.add(UserFavorite(user_id=user_a.id, question_id=q.id))
        commit(s)
        # 给题挂标签关联（级联验证）
        from app.models import QuestionTag, Tag
        from app.tags import TAG_CATEGORIES
        tag = s.scalars(select(Tag).where(Tag.name == "Java")).first()
        if tag is None:
            tag = Tag(category_id=1, name="Java")
            s.add(tag)
            commit(s)
        s.add(QuestionTag(question_id=q.id, tag_id=tag.id))
        commit(s)
    owner = authed_client("owner", role="owner")
    assert owner.delete(f"/api/admin/questions/{q.id}").json()["deleted"] is True
    assert owner.delete(f"/api/admin/questions/{q.id}").status_code == 404
    with get_session() as s:
        assert s.scalars(select(Session).where(Session.question_id == q.id)).first() is None
        assert s.scalars(select(UserPick).where(UserPick.question_id == q.id)).first() is None
        assert s.scalars(select(UserFavorite).where(UserFavorite.question_id == q.id)).first() is None
        assert s.scalars(select(QuestionTag).where(QuestionTag.question_id == q.id)).first() is None


def test_admin_requires_owner(db):
    q = _add_question(db, "管理题")
    normal = authed_client("alice", role="user")
    assert normal.put(f"/api/admin/questions/{q.id}", json={"stem": "x" * 10}).status_code == 403
    assert normal.delete(f"/api/admin/questions/{q.id}").status_code == 403
    assert normal.post("/api/upload", json={"filename": "a.md", "content": "Q: x"}).status_code == 403


def test_seed_owner_from_env(monkeypatch, db):
    """OWNER_EMAIL/OWNER_PASSWORD 预置 owner：无 owner 时创建，幂等不重复。"""
    from app.web.routes import _seed_owner_if_configured

    monkeypatch.setenv("OWNER_EMAIL", "boss@163.com")
    monkeypatch.setenv("OWNER_PASSWORD", "pass12345")
    _seed_owner_if_configured()
    with get_session() as s:
        u = s.scalars(select(User).where(User.email == "boss@163.com")).first()
        assert u is not None and u.role == "owner"
        assert u.username == "boss"  # 显示名 = 邮箱前缀
    monkeypatch.setenv("OWNER_EMAIL", "boss2@163.com")
    _seed_owner_if_configured()
    with get_session() as s:
        assert s.scalars(select(User).where(User.email == "boss2@163.com")).first() is None  # 已有 owner 不重复建


def test_admin_batch_delete_questions(db):
    q1 = _add_question(db, "批量删 1")
    q2 = _add_question(db, "批量删 2")
    q3 = _add_question(db, "保留题")
    owner = authed_client("owner", role="owner")
    resp = owner.post("/api/admin/questions/batch-delete", json={"ids": [q1.id, q2.id]})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    with get_session() as s:
        assert s.get(Question, q1.id) is None
        assert s.get(Question, q2.id) is None
        assert s.get(Question, q3.id) is not None
    # 参数校验
    assert owner.post("/api/admin/questions/batch-delete", json={"ids": []}).status_code == 400
    assert owner.post("/api/admin/questions/batch-delete", json={"ids": list(range(501))}).status_code == 400
    assert owner.post("/api/admin/questions/batch-delete", json={"ids": ["x"]}).status_code == 400
    # 非 owner 拒绝
    normal = authed_client("alice", role="user")
    assert normal.post("/api/admin/questions/batch-delete", json={"ids": [q3.id]}).status_code == 403
