"""事后治理的测试（决策 5 + ADR-0002）：检测 + 处置。

检测的三条性质，每条都对应一种"看起来在工作"的假象：

· **幂等** —— 它是离线任务，会被重跑（心跳回退、手动重跑）。跑两遍不该出两类标记
· **只标记** —— 自动改题库会让"这道题为什么不见了"没人能解释
· **只在同一个知识点下比** —— 两个知识点下有相似表述的题是**正常的**（那正是
  "同一概念被不同角度问"），拿它们报警会让仪表板变成噪音

处置的三条性质：

· 结掉 / 忽略只动标记；**藏起来**才改题库（而且是"不删数据"的那种改）
· 藏起来会把这道题上**所有**未处理的标记一起结掉（否则第二天它又冒出来）
· 越界与不存在的 id 有明确回应（404 / 400）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.bank import quality
from app.db import create_db_engine, create_session_factory
from app.db.models import Domain, KnowledgePoint, Question, QuestionFlag
from app.errors import InvalidInput, NotFound
from migrations._runner import migrate


@pytest.fixture
def session(tmp_dir: Path) -> Session:
    db = tmp_dir / "quality.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.add(KnowledgePoint(id=2, domain_id=1, name="线程池", status="confirmed"))
        s.flush()
        # 同一个知识点下的两道**几乎一样**的题（只差一个句号）
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile 的作用与边界",
                       difficulty=3, primary_point_id=1, origin="seed", visibility="public"))
        s.add(Question(id=2, kind="knowledge", stem="说说 volatile 的作用与边界。",
                       difficulty=3, primary_point_id=1, origin="generated", visibility="public"))
        # 另一个知识点下的一道题（不该被算成重复）
        s.add(Question(id=3, kind="knowledge", stem="说说 volatile 的作用与边界",
                       difficulty=3, primary_point_id=2, origin="seed", visibility="public"))
        s.commit()
        yield s


def _flags(session: Session) -> list[QuestionFlag]:
    return list(session.query(QuestionFlag).order_by(QuestionFlag.id).all())


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------
def test_duplicate_within_the_same_point_is_flagged(session: Session) -> None:
    result = quality.flag_duplicate_questions(session)
    assert result["flagged"] == 1
    assert result["pairs"] == [(1, 2)], "留先入库的那道，标后入库的"
    flag = _flags(session)[0]
    assert flag.question_id == 2
    assert flag.kind == quality.FLAG_KIND
    assert "#1" in flag.detail, "细节要说清和哪一道重复"


def test_similar_stems_in_different_points_are_not_flagged(session: Session) -> None:
    """不同知识点下的相似表述是**正常的** —— 拿它们报警会把仪表板变成噪音。"""
    session.get(Question, 2).primary_point_id = 2  # 只剩 #1 与 #3 同一个点
    session.get(Question, 3).primary_point_id = 1
    session.get(Question, 3).stem = "线程池的拒绝策略怎么选"
    session.flush()

    assert quality.flag_duplicate_questions(session)["flagged"] == 0


def test_detection_is_idempotent(session: Session) -> None:
    """离线任务会被重跑 —— 跑两遍不该出两批标记。"""
    first = quality.flag_duplicate_questions(session)
    second = quality.flag_duplicate_questions(session)
    assert first["flagged"] == 1
    assert second["flagged"] == 1
    assert second["deleted_previous"] == 1, "先清掉自己上一次的未处理记录"
    assert len(_flags(session)) == 1, "库里仍然只有一条"


def test_detection_does_not_touch_the_question_bank(session: Session) -> None:
    """**只标记，不改题库** —— 自动合并会让"题为什么不见了"没人能解释。"""
    before = session.get(Question, 2).to_dict()
    quality.flag_duplicate_questions(session)
    session.flush()
    assert session.get(Question, 2).to_dict() == before


def test_detection_ignores_private_and_hidden_questions(session: Session) -> None:
    """私有题与已下架的题不参与检测：前者不属于公共质量，后者已经处理过了。"""
    session.get(Question, 2).visibility = "hidden"
    session.flush()
    assert quality.flag_duplicate_questions(session)["flagged"] == 0


def test_detection_is_registered_as_an_offline_task(session: Session) -> None:
    from app.offline import jobs, tasks  # noqa: F401  （tasks 在 import 时注册）

    assert "flag_duplicate_questions" in jobs.TASKS
    assert jobs.TASKS["flag_duplicate_questions"].idempotent is True
    outcome = jobs.TASKS["flag_duplicate_questions"].run(session, {})
    assert "重复题" in outcome["message"]


# ---------------------------------------------------------------------------
# 处置
# ---------------------------------------------------------------------------
def test_resolve_and_dismiss_only_touch_the_flag(session: Session) -> None:
    quality.flag_duplicate_questions(session)
    flag_id = _flags(session)[0].id
    before = session.get(Question, 2).to_dict()

    quality.close(session, flag_id=flag_id)
    assert session.get(QuestionFlag, flag_id).status == quality.RESOLVED
    assert session.get(Question, 2).to_dict() == before, "结掉不改题目"

    quality.flag_duplicate_questions(session)  # 重新产出（因为上次那条已经结掉）
    second = _flags(session)[-1]
    quality.close(session, flag_id=second.id, status=quality.DISMISSED)
    assert session.get(QuestionFlag, second.id).status == quality.DISMISSED
    assert session.get(Question, 2).to_dict() == before


def test_close_rejects_an_unknown_status(session: Session) -> None:
    """`open` 不是"手工打开"的入口 —— 那该由重跑检测来做（否则脏点无从追溯）。"""
    quality.flag_duplicate_questions(session)
    with pytest.raises(InvalidInput):
        quality.close(session, flag_id=_flags(session)[0].id, status="open")


def test_close_unknown_flag_is_404(session: Session) -> None:
    with pytest.raises(NotFound):
        quality.close(session, flag_id=999)


def test_hide_takes_the_question_out_of_the_pool_and_closes_all_its_flags(
    session: Session,
) -> None:
    """治理里唯一改题库的动作：**藏起来**（不删数据），并把它所有标记一起结掉。"""
    quality.flag_duplicate_questions(session)
    # 同一道题再来一条别的标记（模拟另一个检测也报了它）
    session.add(QuestionFlag(question_id=2, kind="suspect_mount", detail="另一个检测", status="open"))
    session.flush()

    flag_id = next(f.id for f in _flags(session) if f.kind == quality.FLAG_KIND)
    question, closed = quality.hide_question(session, flag_id=flag_id)

    assert session.get(Question, 2).visibility == "hidden"
    assert closed == 2, "这道题上的两条未处理标记都被结掉了"
    assert all(f.status == "resolved" for f in _flags(session))
    assert question.question_id == 2


def test_hidden_question_disappears_from_browsing(session: Session) -> None:
    """藏起来的实际效果：不再出现在公共浏览里（这正是治理想要的结果）。"""
    from app.bank import repository

    quality.flag_duplicate_questions(session)
    flag_id = _flags(session)[0].id
    quality.hide_question(session, flag_id=flag_id)
    session.flush()

    visible = {q.id for q in repository.public_visible_questions(session)}
    assert 2 not in visible and 1 in visible


def test_hide_unknown_flag_is_404(session: Session) -> None:
    with pytest.raises(NotFound):
        quality.hide_question(session, flag_id=999)


def test_a_flag_cannot_dangle_in_the_first_place(session: Session) -> None:
    """`question_flags.question_id` 是**真外键** —— 标记不可能指向一道不存在的题。

    所以"悬空标记"不是一种需要常态处理的输入（`hide_question` 里那一支只是防御）。
    这条断言把它钉住：将来若有人去掉外键，这里会先红。
    """
    from sqlalchemy.exc import IntegrityError

    quality.flag_duplicate_questions(session)
    session.add(QuestionFlag(question_id=999, kind="conflict", detail="悬空", status="open"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_hide_handles_a_flag_whose_question_vanished(session: Session, monkeypatch) -> None:
    """那一支防御路径也要能给出明确回应（不是 500）。

    正常路径下取不到 None（外键挡着），所以这里**明确地把它造出来** ——
    "不可达所以不测"的结果通常是它哪天真的可达时没人知道该怎么回应。
    """
    quality.flag_duplicate_questions(session)
    flag_id = _flags(session)[0].id

    real_get = Session.get

    def fake_get(self, entity, ident, *args, **kwargs):
        from app.db.models import Question as Q

        if entity is Q:
            return None
        return real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(Session, "get", fake_get)
    with pytest.raises(NotFound):
        quality.hide_question(session, flag_id=flag_id)


def test_open_flags_lists_only_unhandled_ones(session: Session) -> None:
    quality.flag_duplicate_questions(session)
    assert len(quality.open_flags(session)) == 1
    quality.close(session, flag_id=quality.open_flags(session)[0].id)
    assert quality.open_flags(session) == []
