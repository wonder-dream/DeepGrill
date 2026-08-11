import pytest
from datetime import datetime, timedelta
from sqlalchemy import select

from app.config import (
    AppConfig,
    DailyConfig,
    LLMConfig,
    NotificationConfig,
    NowcoderConfig,
    SourcesConfig,
)
from app.crawler.nowcoder import NowcoderError
from app.db import commit, latest_task_log
from app.errors import LLMError
from app.models import (
    Question,
    QuestionType,
    Session,
    SessionKind,
    SessionStatus,
    Source,
    SourceType,
)
from app.pipeline.daily import default_sources, run_daily
from tests.fakes import FakeEmbedder, FakeLLM, FakeSource

EMBEDDER = FakeEmbedder()

Q1 = [
    {
        "type": "knowledge",
        "stem": "讲一下 HashMap 底层原理",
        "tags": [],
        "difficulty": 1,
        "good_criteria": [],
        "bad_criteria": [],
    }
]


Q2 = [
    {
        "type": "knowledge",
        "stem": "讲一下 Redis 持久化",
        "tags": [],
        "difficulty": 1,
        "good_criteria": [],
        "bad_criteria": [],
    }
]


def make_config(daily_overrides=None, github_repos=None):
    daily_defaults = dict(
        max_new_questions=36,
        knowledge_limit=3,
        design_limit=2,
        project_limit=1,
        chain_max_rounds=20,
        schedule="08:00",
    )
    daily_defaults.update(daily_overrides or {})
    return AppConfig(
        llm=LLMConfig(
            base_url="http://x", api_key_env="LLM_API_KEY",
            generate_model="g", judge_model="j",
        ),
        nowcoder=NowcoderConfig(cookie_env="NOWCODER_COOKIE", request_interval=0, retries=1),
        daily=DailyConfig(**daily_defaults),
        notification=NotificationConfig(enabled=False),
        sources=SourcesConfig(github_repos=github_repos or []),
    )


_source_seq = 0


def add_source(db, cleaned="一面：\n问了 HashMap。", type=SourceType.manual):
    global _source_seq
    _source_seq += 1
    source = Source(
        type=type,
        source_hash=f"src-{_source_seq}",
        cleaned_text=cleaned,
        raw_text=cleaned,
    )
    db.add(source)
    commit(db)
    db.refresh(source)
    return source


# --- happy ---


def test_happy_full_flow(db):
    s1 = add_source(db, cleaned="一面：\n问了 HashMap。")
    s2 = add_source(db, cleaned="二面：\n问了 Redis。")
    llm = FakeLLM([Q1, Q2])
    report = run_daily(make_config(), [FakeSource("a", [s1, s2])], llm, EMBEDDER)

    assert report["sources"]["a"] == {"fetched": 2, "failed": 0}
    assert report["new_questions"] == 2
    assert report["errors"] == []
    assert report["today_questions"] == []  # 选题由用户懒加载，流水线不再选题

    log = latest_task_log(db, "daily")
    assert log.status == "success"
    assert log.fetched_count == 2
    assert log.generated_count == 2


def test_default_sources_registry():
    names = [p.name for p in default_sources(make_config())]
    assert "importer" in names
    assert "nowcoder" in names
    assert "github" not in names  # 未配置仓库不启用

    names = [p.name for p in default_sources(make_config(github_repos=["a/b"]))]
    assert "github" in names


# --- edge ---


def test_no_new_sources_picks_old_pending(db):
    s = add_source(db)
    for i in range(2):
        q = Question(source_id=s.id, type=QuestionType.knowledge, stem=f"旧题{i}")
        db.add(q)
    commit(db)

    llm = FakeLLM([])
    report = run_daily(make_config(), [FakeSource("a", [])], llm, EMBEDDER)

    assert report["new_questions"] == 0
    assert len(llm.calls) == 0
    assert report["today_questions"] == []  # 无新题不选题（懒加载机制接管）
    assert report["sources"]["a"] == {"fetched": 0, "failed": 0}


def test_d16_cap_stops_generation(db):
    sources = [add_source(db) for _ in range(12)]
    gen_batches = [
        [
            {"type": "knowledge", "stem": f"s{i}q{j}", "tags": [], "difficulty": 1,
             "good_criteria": [], "bad_criteria": []}
            for j in range(4)
        ]
        for i in range(12)
    ]
    llm = FakeLLM(gen_batches)
    report = run_daily(
        make_config({"max_new_questions": 36}), [FakeSource("a", sources)], llm, EMBEDDER
    )

    assert report["new_questions"] == 36
    assert len(llm.calls) == 9  # 第 10 个源起停止生成
    count = db.scalars(select(Question)).all()
    assert len(count) == 36


def test_d16_cap_truncates_overshoot(db):
    """D16 严格截断：源产出超过剩余额度时只入库额度内的题。"""
    s1 = add_source(db, cleaned="一面：\n问了 HashMap。")
    s2 = add_source(db, cleaned="一面：\n问了 Redis。")
    big = [
        {"type": "knowledge", "stem": f"题{j}", "tags": [], "difficulty": 1,
         "good_criteria": [], "bad_criteria": []}
        for j in range(4)
    ]
    llm = FakeLLM([big])
    report = run_daily(make_config({"max_new_questions": 3}), [FakeSource("a", [s1, s2])], llm, EMBEDDER)

    assert report["new_questions"] == 3  # 4 题只入库 3 题
    count = db.scalars(select(Question)).all()
    assert len(count) == 3


def test_d16_cap_never_exceeds_limit(db):
    """多源累计入库始终 ≤ 上限（严格截断后）。"""
    sources = [add_source(db, cleaned=f"一面：\n源{i} 问题。") for i in range(5)]
    batches = [
        [
            {"type": "knowledge", "stem": f"src{i}q{j}", "tags": [], "difficulty": 1,
             "good_criteria": [], "bad_criteria": []}
            for j in range(10)
        ]
        for i in range(5)
    ]
    llm = FakeLLM(batches)
    report = run_daily(make_config({"max_new_questions": 7}), [FakeSource("a", sources)], llm, EMBEDDER)

    assert report["new_questions"] <= 7
    count = db.scalars(select(Question)).all()
    assert len(count) <= 7


def test_today_questions_respected_quota_types(db):
    s = add_source(db)
    payload = [
        {"type": "knowledge", "stem": "知识题1", "tags": [], "difficulty": 1,
         "good_criteria": [], "bad_criteria": []},
        {"type": "knowledge", "stem": "知识题2", "tags": [], "difficulty": 1,
         "good_criteria": [], "bad_criteria": []},
        {"type": "design", "stem": "设计题1", "tags": [], "difficulty": 1,
         "good_criteria": [], "bad_criteria": []},
        {"type": "design", "stem": "设计题2", "tags": [], "difficulty": 1,
         "good_criteria": [], "bad_criteria": []},
    ]
    llm = FakeLLM([payload])
    report = run_daily(make_config(), [FakeSource("a", [s])], llm, EMBEDDER)
    assert report["new_questions"] == 4
    assert report["today_questions"] == []  # 选题由用户懒加载

    from app.db import pick_questions
    from app.models import User

    user = User(username="quota_user", password_hash="x")
    db.add(user)
    commit(db)
    db.refresh(user)
    # 每用户池按 D8 配额（knowledge 3/design 2/project 1）选取
    picked = []
    for qtype, limit in (
        (QuestionType.knowledge, 3),
        (QuestionType.design, 2),
        (QuestionType.project, 1),
    ):
        picked.extend(pick_questions(db, user.id, limit, qtype))
    types = [q.type for q in picked]
    assert types.count(QuestionType.knowledge) == 2
    assert types.count(QuestionType.design) == 2


def test_rerun_idempotent(db):
    s = add_source(db)
    llm = FakeLLM([Q1, Q1])
    providers = [FakeSource("a", [s])]
    first = run_daily(make_config(), providers, llm, EMBEDDER)
    assert first["new_questions"] == 1

    second = run_daily(make_config(), providers, llm, EMBEDDER)
    assert second["new_questions"] == 0  # 与库内重复被拒收
    count = db.scalars(select(Question)).all()
    assert len(count) == 1


def test_picked_questions_flow_through_generation(db):
    """生成入库后题可被用户按需选中（每用户池）；无 finished 会话的题可被再次选。"""
    s = add_source(db)
    llm = FakeLLM([Q1])
    report = run_daily(make_config(), [FakeSource("a", [s])], llm, EMBEDDER)
    assert report["new_questions"] == 1

    from app.models import User

    user = User(username="dailyuser", password_hash="x")
    db.add(user)
    commit(db)
    db.refresh(user)

    from app.db import list_today_questions, pick_questions

    picked = pick_questions(db, user.id, 5)
    assert [q.stem for q in picked] == ["讲一下 HashMap 底层原理"]
    assert [q.stem for q in list_today_questions(db, user.id)] == ["讲一下 HashMap 底层原理"]


# --- fail ---


def test_generates_from_sources_without_questions_on_rerun(db):
    """回归：源已入库但上次生成中断（首次同步被杀场景）→ 本轮零新采集也补生成。"""
    s1 = add_source(db, cleaned="一面：\n问了 HashMap。")
    s2 = add_source(db, cleaned="一面：\n问了 Redis。")
    # 模拟首次同步已导入源但生成中断：库里有源、无题
    llm = FakeLLM([Q1, Q2])
    report = run_daily(make_config(), [FakeSource("a", [])], llm, EMBEDDER)

    assert report["new_questions"] == 2
    assert report["errors"] == []
    assert report["today_questions"] == []  # 懒加载语义


def test_source_failure_isolated(db):
    s = add_source(db)
    llm = FakeLLM([Q1])
    report = run_daily(
        make_config(),
        [
            FakeSource("bad", error=NowcoderError("boom")),
            FakeSource("ok", [s]),
        ],
        llm,
        EMBEDDER,
    )
    assert report["sources"]["bad"] == {"fetched": 0, "failed": 1}
    assert report["sources"]["ok"] == {"fetched": 1, "failed": 0}
    assert report["new_questions"] == 1
    assert any("bad" in e for e in report["errors"])


def test_generation_failure_skips_source_keeps_others(db):
    s1 = add_source(db)
    s2 = add_source(db)
    llm = FakeLLM([Q1, LLMError("down")])
    report = run_daily(make_config(), [FakeSource("a", [s1, s2])], llm, EMBEDDER)

    assert report["new_questions"] == 1  # s1 成功，s2 失败跳过
    assert any("generate" in e for e in report["errors"])
    log = latest_task_log(db, "daily")
    assert log.status == "partial"


def test_manual_sources_generate_first(db):
    """手动导入的源（manual/resume）优先于采集源生成，避免被历史积压挤到后面。"""
    nowcoder_src = add_source(db, type=SourceType.nowcoder, cleaned="一面：\n问了 HashMap。")
    manual_src = add_source(db, type=SourceType.manual, cleaned="一面：\n问了 Redis。")
    llm = FakeLLM([Q1, Q2])
    report = run_daily(make_config({"max_new_questions": 1}), [FakeSource("a", [])], llm, EMBEDDER)

    assert report["new_questions"] == 1
    q = db.scalars(select(Question)).all()
    assert len(q) == 1
    assert q[0].source_id == manual_src.id  # manual 源先被处理
    assert len(llm.calls) == 1


def test_run_daily_never_raises(db):
    llm = FakeLLM([])
    report = run_daily(
        make_config(),
        [FakeSource("bad", error=RuntimeError("unexpected"))],
        llm,
        EMBEDDER,
    )
    assert report["sources"]["bad"] == {"fetched": 0, "failed": 1}
    assert len(report["errors"]) == 1


def test_run_daily_rejects_overlap(db):
    """并发/重复触发被互斥锁拒绝：返回 SKIPPED_REPORT 且不写 task_log。"""
    from app.pipeline.daily import _run_lock

    assert _run_lock.acquire(blocking=False)
    try:
        report = run_daily(make_config(), [FakeSource("a", [])], FakeLLM([]), EMBEDDER)
    finally:
        _run_lock.release()
    assert report["skipped"] is True
    assert latest_task_log(db, "daily") is None
