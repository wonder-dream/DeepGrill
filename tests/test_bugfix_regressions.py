"""接口缺陷修复的回归测试（对应 docs/接口缺陷清单-20260913.md）。

每条用例先复现原缺陷（注释给出修复前行为），再断言修复后的行为。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import commit, get_session, init_db
from app.models import Question, QuestionType, Source, SourceType, User
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

