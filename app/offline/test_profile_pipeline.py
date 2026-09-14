"""简历 → 档案 → 私有题集（决策 8）的测试。

三条最要紧的性质：

· **不保存原文**：`candidate_profiles` 里只有结构化档案，简历正文一个字都不落库
· **私有隔离**：生成的题只有本人可见 —— 直接调 `bank` 的可见性入口验证
· **可判分**：生成的题必须带考察点（否则判分会走"全部未涉及"那条降级路径，
  用户的面试会永远 0 分，而他不会知道为什么）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.db import create_db_engine, create_session_factory
from app.db.models import CandidateProfile, Criterion, KnowledgePoint, Question, User
from app.errors import InvalidInput
from app.llm import LLMCallError
from app.offline import profile_pipeline
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply

ME = 2
OTHER = 3

RESUME = """
张三，后端开发，3 年经验。
项目：优惠券系统（2023-2024）
  用 Redis 做分布式锁控制库存扣减，QPS 从 800 提升到 3000。
  负责订单状态机设计，用 RabbitMQ 做最终一致性。
技能：Java、SpringBoot、Redis、MySQL、RabbitMQ
"""

PROFILE = {
    "headline": "后端开发，3 年经验",
    "years": "3 年",
    "skills": ["Java", "SpringBoot", "Redis", "MySQL", "RabbitMQ"],
    "projects": [
        {
            "name": "优惠券系统",
            "role": "后端负责人",
            "tech": ["Redis", "RabbitMQ"],
            "highlights": ["用 Redis 分布式锁控制库存扣减，QPS 从 800 提升到 3000"],
            "probe_points": ["分布式锁的超时与业务耗时怎么协调", "QPS 提升 3 倍是怎么测的"],
        }
    ],
}

QUESTIONS = {
    "questions": [
        {
            "stem": "你提到用 Redis 分布式锁控制库存扣减 —— 锁超时和业务执行时间怎么协调？",
            "kind": "design",
            "difficulty": 4,
            "source": "优惠券系统",
            "criteria": ["说清锁超时与续期机制", "说清误删他人锁的风险", "给出降级方案"],
        },
        {
            "stem": "QPS 从 800 提升到 3000 是怎么测出来的？瓶颈原来在哪？",
            "kind": "design",
            "difficulty": 3,
            "source": "优惠券系统",
            "criteria": ["说清压测方法", "定位到具体瓶颈"],
        },
    ]
}


@pytest.fixture
def session(tmp_dir: Path) -> Session:
    db = tmp_dir / "pipeline.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        s.add_all(
            [
                User(id=ME, email="me@local", username="me", password_hash="h", role="user"),
                User(id=OTHER, email="o@local", username="o", password_hash="h", role="user"),
            ]
        )
        s.commit()
        yield s


def test_parses_resume_into_a_structured_profile(session: Session) -> None:
    llm = FakeLLM().queue(FakeReply(data=PROFILE))
    profile = profile_pipeline.parse_resume(RESUME, llm=llm)

    assert profile["headline"].startswith("后端开发")
    assert profile["skills"] == ["Java", "SpringBoot", "Redis", "MySQL", "RabbitMQ"]
    assert profile["projects"][0]["name"] == "优惠券系统"
    assert "QPS 从 800 提升到 3000" in profile["projects"][0]["highlights"][0]


def test_parse_tolerates_missing_fields_without_inventing(session: Session) -> None:
    """模型漏给的字段补空 —— **不编造**（"简历里没说"就该显示为没说）。"""
    llm = FakeLLM().queue(FakeReply(data={"headline": "某人"}))
    profile = profile_pipeline.parse_resume(RESUME, llm=llm)
    assert profile["skills"] == []
    assert profile["projects"] == []
    assert profile["years"] == "未提及"


def test_short_resume_is_refused_before_calling_the_model(session: Session) -> None:
    """太短的多半是误操作。**在调模型之前就拒绝** —— 省一次调用，也省一份垃圾档案。"""
    llm = FakeLLM()
    with pytest.raises(InvalidInput):
        profile_pipeline.parse_resume("太短了", llm=llm)
    assert llm.calls == [], "不该为了一个必然失败的输入去调模型"


def test_too_long_resume_is_refused(session: Session) -> None:
    llm = FakeLLM()
    with pytest.raises(InvalidInput):
        profile_pipeline.parse_resume("字" * (profile_pipeline.MAX_RESUME_CHARS + 1), llm=llm)
    assert llm.calls == []


def test_full_pipeline_creates_private_questions_with_criteria(session: Session) -> None:
    llm = FakeLLM().queue(FakeReply(data=PROFILE), FakeReply(data=QUESTIONS))
    result = profile_pipeline.create_private_question_set(
        session, user_id=ME, resume_text=RESUME, llm=llm
    )
    session.commit()

    assert result.questions_created == 2
    rows = session.execute(select(Question).where(Question.owner_user_id == ME)).scalars().all()
    assert len(rows) == 2
    for q in rows:
        assert q.visibility == "private"
        assert q.primary_point_id is not None, "必须挂到锚点上，否则无法判分"
        criteria = session.execute(
            select(Criterion).where(Criterion.point_id == q.primary_point_id)
        ).scalars().all()
        assert criteria, "没有考察点就无法判分（会永远 0 分且用户不知道原因）"
        assert criteria[0].text


def test_resume_text_is_never_persisted(session: Session) -> None:
    """**决策 8 的硬规定**：不保存简历原文。库里不该出现它的任何片段。"""
    llm = FakeLLM().queue(FakeReply(data=PROFILE), FakeReply(data=QUESTIONS))
    profile_pipeline.create_private_question_set(session, user_id=ME, resume_text=RESUME, llm=llm)
    session.commit()

    saved = session.execute(select(CandidateProfile)).scalars().all()
    assert len(saved) == 1
    blob = json.dumps(saved[0].structured, ensure_ascii=False) + (saved[0].source_note or "")
    assert "张三，后端开发" not in blob, "简历原文不许进库"
    assert "QPS 从 800 提升到 3000" in blob, "但结构化档案里的量化结果要留（它是深挖入口）"


def test_generated_questions_are_invisible_to_others(session: Session) -> None:
    """**私有隔离**：走 `bank` 的可见性入口，别人看不到我简历生成的题。"""
    llm = FakeLLM().queue(FakeReply(data=PROFILE), FakeReply(data=QUESTIONS))
    profile_pipeline.create_private_question_set(session, user_id=ME, resume_text=RESUME, llm=llm)
    session.commit()

    mine, _ = bank_repository.list_questions(session, ME)
    theirs, _ = bank_repository.list_questions(session, OTHER)
    anon, _ = bank_repository.list_questions(session, None)
    assert len(mine) == 2
    assert theirs == [] and anon == []


def test_pipeline_is_idempotent_on_the_same_profile(session: Session) -> None:
    """同一份档案重复生成不该出现两道一模一样的题。"""
    llm = FakeLLM().queue(FakeReply(data=PROFILE), FakeReply(data=QUESTIONS))
    profile_pipeline.create_private_question_set(session, user_id=ME, resume_text=RESUME, llm=llm)
    session.commit()

    llm2 = FakeLLM().queue(FakeReply(data=PROFILE), FakeReply(data=QUESTIONS))
    second = profile_pipeline.create_private_question_set(
        session, user_id=ME, resume_text=RESUME, llm=llm2
    )
    session.commit()
    assert second.questions_created == 0
    assert len(session.execute(select(Question).where(Question.owner_user_id == ME)).scalars().all()) == 2


def test_question_generation_failure_keeps_the_profile(session: Session) -> None:
    """出题失败时**档案要留着** —— 用户不必重贴一次简历（降级但不丢工作）。"""
    llm = FakeLLM().queue(FakeReply(data=PROFILE), FakeReply(error=LLMCallError("模型挂了")))
    result = profile_pipeline.create_private_question_set(
        session, user_id=ME, resume_text=RESUME, llm=llm
    )
    session.commit()

    assert result.llm_failed is True
    assert result.questions_created == 0
    assert "不必重贴简历" in result.note
    assert session.execute(select(CandidateProfile)).scalars().first() is not None


def test_private_anchor_is_draft_and_reused(session: Session) -> None:
    """锚点是 `draft`（没经过人审的容器），且**同一个人复用同一个**。"""
    llm = FakeLLM().queue(FakeReply(data=PROFILE), FakeReply(data=QUESTIONS))
    profile_pipeline.create_private_question_set(session, user_id=ME, resume_text=RESUME, llm=llm)
    session.commit()

    anchors = session.execute(
        select(KnowledgePoint).where(KnowledgePoint.name.like("私有题集（用户 %"))
    ).scalars().all()
    assert len(anchors) == 1
    assert anchors[0].status == "draft", "它不是人审过的骨架节点"


def test_bad_kind_falls_back_to_design(session: Session) -> None:
    """模型给了个不在枚举里的题型 → 归到 `design`，而不是把错值写进库。

    决策 20 定了公共题库只有 knowledge / design；私有题沿用同一套，
    所以不新增枚举值（那要动 schema）。
    """
    llm = FakeLLM().queue(
        FakeReply(data=PROFILE),
        FakeReply(data={"questions": [{"stem": "某题", "kind": "project", "difficulty": 9,
                                       "criteria": ["a"]}]}),
    )
    profile_pipeline.create_private_question_set(session, user_id=ME, resume_text=RESUME, llm=llm)
    session.commit()

    q = session.execute(select(Question).where(Question.owner_user_id == ME)).scalars().one()
    assert q.kind == "design"
    assert q.difficulty == 5, "难度要钳进 1-5"
