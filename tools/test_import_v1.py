"""v1 导入的测试（决策 49）。

这一组**不碰真的 v1 库**（它可能不在别的机器上、也不该被测试依赖），而是造一个
**形状相同**的小 v1 库，然后验证映射规则。真库只在 `--dry-run` 与实跑时读一次。

最值钱的两条：

· **源库只读**：导入过程绝不能写 v1（AGENTS.md §五），有护栏与测试
· **幂等**：重复跑导入不会翻倍 —— 靠题干（v1 实测零重复题干）
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import Question
from migrations._runner import migrate
from tools import import_v1

#: 一个与 v1 形状相同的最小库（列名与类型都对照真实库）。
V1_DDL = """
CREATE TABLE questions (
    id INTEGER PRIMARY KEY,
    source_id INTEGER,
    type TEXT,
    stem TEXT,
    difficulty INTEGER,
    good_criteria TEXT,
    bad_criteria TEXT,
    suggested_category TEXT,
    suggested_tags TEXT,
    suggested_difficulty INTEGER,
    suggested_at TEXT,
    reviewed_at TEXT,
    selected_at TEXT,
    embedding BLOB,
    created_at TEXT
);
CREATE TABLE tag_categories (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, category_id INTEGER, name TEXT);
CREATE TABLE question_tags (question_id INTEGER, tag_id INTEGER);
"""


def _make_v1(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(V1_DDL)
        rows = [
            (1, 1, "knowledge", "说说 volatile 的作用", 3,
             json.dumps(["可见性", "内存屏障"], ensure_ascii=False), json.dumps(["不保证原子性"], ensure_ascii=False)),
            (2, 1, "design", "设计一个限流器", 4,
             json.dumps(["令牌桶", "漏桶"], ensure_ascii=False), json.dumps(["忽略分布式"], ensure_ascii=False)),
            # 要剔除的两道简历噪声
            (62, 1, "knowledge", "请介绍您参与的RAG项目背景及主要功能。", 3, '["a","b"]', '["c"]'),
            (69, 1, "knowledge", "请介绍您参与的优惠券项目背景及主要功能。", 3, '["a","b"]', '["c"]'),
            # 要改写的那道
            (2136, 1, "design", "请描述你负责的Agent系统架构，并解释为什么选择这种架构而不是其他方案。",
             4, '["取舍","模块划分"]', '["只说结论"]'),
            # 坏数据：criteria 不是 JSON 数组
            (7, 1, "knowledge", "criteria 坏掉的题", 3, "not-json", '["c"]'),
            # 难度越界（v1 实测没有，但**不信输入**）
            (8, 1, "knowledge", "难度越界的题", 9, '["a","b"]', '["c"]'),
        ]
        conn.executemany(
            "INSERT INTO questions (id, source_id, type, stem, difficulty, good_criteria, bad_criteria) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute("INSERT INTO tags (id, category_id, name) VALUES (1, 1, 'Agent')")
        conn.execute("INSERT INTO tags (id, category_id, name) VALUES (2, 1, 'RAG')")
        conn.execute("INSERT INTO question_tags (question_id, tag_id) VALUES (1, 1)")
        conn.execute("INSERT INTO question_tags (question_id, tag_id) VALUES (1, 2)")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def v1_path(tmp_dir: Path) -> Path:
    return _make_v1(tmp_dir / "v1.db")


@pytest.fixture
def session(tmp_dir: Path) -> Session:
    db = tmp_dir / "v2.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        yield s


# ---------------------------------------------------------------------------
# 只读约束
# ---------------------------------------------------------------------------
def test_open_v1_is_readonly(v1_path: Path) -> None:
    """**源库必须只读** —— AGENTS.md §五："不要修改它"。"""
    conn = import_v1.open_v1_readonly(v1_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM questions")
    finally:
        conn.close()


def test_open_v1_reports_a_missing_file(tmp_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        import_v1.open_v1_readonly(tmp_dir / "nope.db")


def test_import_never_touches_the_source(v1_path: Path, session: Session) -> None:
    """导入前后源库的写计数不变 —— 护栏在 `run()` 里，这条测试守着它。"""
    before = v1_path.stat().st_mtime_ns
    result = import_v1.run(session=session, v1_path=v1_path)
    session.commit()
    assert result.inserted > 0
    assert v1_path.stat().st_mtime_ns == before, "源库被改了"


# ---------------------------------------------------------------------------
# 映射规则
# ---------------------------------------------------------------------------
def test_maps_the_basic_fields(v1_path: Path, session: Session) -> None:
    import_v1.run(session=session, v1_path=v1_path)
    session.commit()

    q = session.execute(select(Question).where(Question.stem == "说说 volatile 的作用")).scalars().one()
    assert q.kind == "knowledge"
    assert q.difficulty == 3
    assert q.origin == "seed"
    assert q.visibility == "public"
    assert q.owner_user_id is None, "导入的是公共题"
    assert q.primary_point_id is None, "挂载由知识层管道做，导入不猜挂载点"
    assert q.answer_tier == "long_tail", "冷门题只给评分标准（决策 12）"
    assert json.loads(q.good_criteria) == ["可见性", "内存屏障"]


def test_design_type_maps_to_design_kind(v1_path: Path, session: Session) -> None:
    import_v1.run(session=session, v1_path=v1_path)
    session.commit()
    q = session.execute(select(Question).where(Question.stem == "设计一个限流器")).scalars().one()
    assert q.kind == "design"


def test_drops_the_two_resume_noise_questions(v1_path: Path, session: Session) -> None:
    """§12.3 点名的两道：它们是 v1 把简历题目误存进公共题库的产物，对任何人都不可答。"""
    result = import_v1.run(session=session, v1_path=v1_path)
    session.commit()
    assert sorted(result.dropped) == [62, 69]
    stems = {q.stem for q in session.execute(select(Question)).scalars()}
    assert not any("请介绍您参与的" in s for s in stems)


def test_rewrites_the_first_person_question(v1_path: Path, session: Session) -> None:
    """§12.3 的第三道是"边缘"：问的是设计能力，只是用了第一人称 —— 改写而不是剔除。"""
    result = import_v1.run(session=session, v1_path=v1_path)
    session.commit()
    assert result.rewritten == [2136]
    stems = {q.stem for q in session.execute(select(Question)).scalars()}
    assert import_v1.REWRITE[2136] in stems
    assert not any(s.startswith("请描述你负责的") for s in stems)


def test_bad_criteria_is_reported_not_silently_dropped(v1_path: Path, session: Session) -> None:
    """坏数据要进 `bad_rows`（**不静默丢弃**，AGENTS.md §3.1）。"""
    result = import_v1.run(session=session, v1_path=v1_path)
    session.commit()
    assert (7, "good_criteria 不是 JSON 数组") in result.bad_rows
    assert all(q.stem != "criteria 坏掉的题" for q in session.execute(select(Question)).scalars())


def test_out_of_range_difficulty_is_clamped(v1_path: Path, session: Session) -> None:
    """**不信输入**：难度越界钳到 1-5（v1 实测没有越界，但导入脚本不该依赖那一点）。"""
    result = import_v1.run(session=session, v1_path=v1_path)
    session.commit()
    q = session.execute(select(Question).where(Question.stem == "难度越界的题")).scalars().one()
    assert q.difficulty == 5
    assert result.bad_rows == [] or all("难度" not in why for _id, why in result.bad_rows)


def test_import_is_idempotent(v1_path: Path, session: Session) -> None:
    """重复跑不会翻倍 —— 靠题干（v1 实测零重复题干）。"""
    first = import_v1.run(session=session, v1_path=v1_path)
    session.commit()
    before = len(session.execute(select(Question)).scalars().all())

    second = import_v1.run(session=session, v1_path=v1_path)
    session.commit()
    after = len(session.execute(select(Question)).scalars().all())

    assert first.inserted == before
    assert second.inserted == 0
    assert second.skipped_existing == before
    assert before == after


def test_tag_mapping_is_exported(v1_path: Path, session: Session, tmp_dir: Path) -> None:
    """标签不进 v2 的产品结构，但导出成映射留作聚类辅助信号与回查依据。"""
    out = tmp_dir / "tags.json"
    import_v1.run(session=session, v1_path=v1_path, tag_mapping_path=out)
    session.commit()
    mapping = json.loads(out.read_text(encoding="utf-8"))
    assert mapping["1"] == ["Agent", "RAG"]


def test_result_summary_is_readable(v1_path: Path, session: Session) -> None:
    result = import_v1.run(session=session, v1_path=v1_path)
    assert "扫过" in result.summary()
    assert "剔除 2" in result.summary()
