"""晋升与自动门禁的测试（决策 5、9）。

门禁的失败方式是**静默的**那种：它可能悄悄放过一道不该进公共池的题，或者悄悄
拦下一道好题（用户不知道为什么、也没有申诉路径）。所以这里的断言分两类：

· **每一条检查都要能单独变红**（把该让过的放过、该拦的拦住）
· **失败时什么都不改** —— 题留在私有题集里，用户可以改完再试

另有一条容易被忽略的：**过闸之后题进的是待定池**（`primary_point_id=None`），
而不是"立刻可用"。理由写在 `app/bank/promotion.py` 的模块 docstring 里。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.bank import promotion
from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Domain, KnowledgePoint, Question, User
from app.errors import Forbidden, InvalidInput
from migrations._runner import migrate

ME = 2
OTHER = 3


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "promo.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(User(id=ME, email="me@local", username="me", password_hash="x", role="user"))
        s.add(User(id=OTHER, email="o@local", username="o", password_hash="x", role="user"))
        s.add(Domain(id=1, name="私有题集"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="私有题集（用户 2）",
                             status="draft", origin="manual"))
        # 一个**人审过**的公共知识点：题 #2 挂在它上面，于是它不在待定池里
        s.add(KnowledgePoint(id=2, domain_id=1, name="并发基础",
                             status="confirmed", origin="proposed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="要点一", shared=0))
        s.flush()
        # 一道"形状合格"的私有题：题干够长、有考察点、题型允许
        s.add(Question(id=1, kind="design", stem="设计一个支持幂等与熔断的下单系统",
                       difficulty=4, primary_point_id=1, origin="generated",
                       owner_user_id=ME, visibility="private", answer_tier="long_tail"))
        # 一道公共题（用于重复检测），**已挂载** —— 所以它不算待定池
        s.add(Question(id=2, kind="knowledge", stem="说说 volatile 的作用与边界",
                       difficulty=3, primary_point_id=2, origin="seed", visibility="public"))
        s.commit()
        yield s


def _q(session: Session, qid: int = 1) -> Question:
    question = session.get(Question, qid)
    assert question is not None, f"夹具里应当有题目 {qid}"
    return question


def _check(result: promotion.GateResult, name: str) -> promotion.GateCheck:
    return next(c for c in result.checks if c.name == name)


# ---------------------------------------------------------------------------
# 门禁：每一条单独验
# ---------------------------------------------------------------------------
def test_a_well_formed_question_passes(session: Session) -> None:
    result = promotion.run_gate(session, question=_q(session), user_id=ME)
    assert result.passed, result.summary()
    assert {c.name for c in result.checks} == {
        "owned", "stem_length", "kind", "criteria", "not_duplicate"
    }


def test_someone_elses_question_fails_the_ownership_check(session: Session) -> None:
    result = promotion.run_gate(session, question=_q(session), user_id=OTHER)
    assert not result.passed
    assert not _check(result, "owned").ok


def test_short_stem_is_refused(session: Session) -> None:
    _q(session).stem = "volatile"
    session.flush()
    result = promotion.run_gate(session, question=_q(session), user_id=ME)
    assert not _check(result, "stem_length").ok
    assert "字" in _check(result, "stem_length").why, "要说清该多长（用户据此能改）"


def test_duplicate_stem_is_refused(session: Session) -> None:
    _q(session).stem = "说说 volatile 的作用与边界 "  # 只差一个空格
    session.flush()
    result = promotion.run_gate(session, question=_q(session), user_id=ME)
    assert not _check(result, "not_duplicate").ok
    assert "#2" in _check(result, "not_duplicate").why, "要说清和哪一道重复"


def test_hidden_public_question_does_not_block_promotion(session: Session) -> None:
    """已下架的题不参与去重 —— 否则一道被藏起来的题会永远挡着别人晋升。"""
    _q(session, 2).visibility = "hidden"
    _q(session).stem = "说说 volatile 的作用与边界"
    session.flush()
    assert promotion.run_gate(session, question=_q(session), user_id=ME).passed


def test_kind_outside_the_public_set_is_refused(session: Session) -> None:
    """公共题库只收 knowledge / design（决策 20）—— 用一个绕开 CHECK 的题验。

    （DB 的 CHECK 已经保证 `kind` 只能是那两个值，所以这里直接改门禁的白名单，
    验的是"判据本身在跑"，而不是"库能不能存住非法值"。）
    """
    original: tuple[str, ...] = promotion.PUBLIC_KINDS
    # 单元素元组要写成 `(x,)` —— 少了那个逗号它就是个字符串，而 `in` 仍然能跑（于是测试静默地没测到东西）
    promotion.PUBLIC_KINDS = ("knowledge",)
    try:
        result = promotion.run_gate(session, question=_q(session), user_id=ME)
        assert not _check(result, "kind").ok
    finally:
        promotion.PUBLIC_KINDS = original


def test_question_without_criteria_is_refused(session: Session) -> None:
    _q(session).primary_point_id = None
    session.flush()
    result = promotion.run_gate(session, question=_q(session), user_id=ME)
    assert not _check(result, "criteria").ok


def test_gate_is_read_only(session: Session) -> None:
    """`run_gate` 只读 —— 页面可以随时预演（"我这样改能过吗"），不该有副作用。"""
    before = _q(session).to_dict()
    promotion.run_gate(session, question=_q(session), user_id=ME)
    session.flush()
    assert _q(session).to_dict() == before


# ---------------------------------------------------------------------------
# 晋升：过闸之后改了什么
# ---------------------------------------------------------------------------
def test_promote_moves_the_question_into_the_pending_pool(session: Session) -> None:
    result = promotion.promote(session, question=_q(session), user_id=ME)
    assert result.passed

    q = _q(session)
    assert q.owner_user_id is None, "进公共池：不再属于某个人"
    assert q.visibility == "public"
    assert q.primary_point_id is None, "挂载交给知识层管道（决策 46 的待定池）"
    assert q.origin == "promoted", "来源三类之一（决策 4）"
    assert promotion.pending_pool_size(session) == 1


def test_failed_gate_changes_nothing(session: Session) -> None:
    """**门禁没过就什么都不改** —— 题留在私有题集里，用户改完可以再试。"""
    _q(session).stem = "太短"
    session.flush()
    result = promotion.promote(session, question=_q(session), user_id=ME)
    assert not result.passed
    q = _q(session)
    assert q.owner_user_id == ME and q.visibility == "private"
    assert q.primary_point_id == 1, "连知识点归属都没动"


def test_cannot_promote_someone_elses_question(session: Session) -> None:
    with pytest.raises(Forbidden):
        promotion.promote(session, question=_q(session), user_id=OTHER)


def test_cannot_promote_an_already_public_question(session: Session) -> None:
    """已经在公共池里的题不必再晋升 —— 明确拒绝，而不是静默无事发生。

    （构造一个"作者还是我、但已经公共了"的状态：现实中它来自"晋升后又改回来"
    之类的路径，而这一层守卫要能挡住它。）
    """
    q = _q(session)
    q.visibility = "public"
    session.flush()
    with pytest.raises(InvalidInput):
        promotion.promote(session, question=_q(session), user_id=ME)


def test_promotion_is_idempotent_in_effect(session: Session) -> None:
    """第一次晋升成功；第二次被拒（它已经不是"我的私有题"了），题库状态不变。"""
    promotion.promote(session, question=_q(session), user_id=ME)
    snapshot = _q(session).to_dict()
    with pytest.raises(Forbidden):
        promotion.promote(session, question=_q(session), user_id=ME)
    assert _q(session).to_dict() == snapshot


def test_promote_does_not_touch_other_questions_of_the_same_author(session: Session) -> None:
    """同一个私有锚点下的**其他**题不受影响（它们还挂在那个草稿点上）。"""
    session.add(Question(id=3, kind="design", stem="另一道私有设计题，足够长了吧",
                         difficulty=3, primary_point_id=1, origin="generated",
                         owner_user_id=ME, visibility="private"))
    session.flush()

    promotion.promote(session, question=_q(session), user_id=ME)
    other = _q(session, 3)
    assert other.owner_user_id == ME and other.visibility == "private"
    assert other.primary_point_id == 1


def test_promotion_does_not_charge_quota(session: Session) -> None:
    """晋升是一次纯数据库写入（没有 LLM 调用），所以不扣额度点。"""
    from sqlalchemy import select

    from app.db.models import QuotaLedger

    promotion.promote(session, question=_q(session), user_id=ME)
    assert session.execute(select(QuotaLedger)).first() is None


def test_normalize_stem_treats_punctuation_variants_as_the_same(session: Session) -> None:
    """只差标点/大小写/空白的题就是同一道题 —— 门禁与治理用的是同一个归一化。"""
    assert promotion.normalize_stem("说说 Volatile 的作用。") == promotion.normalize_stem(
        " 说说volatile的作用 "
    )
    assert promotion.normalize_stem("A") != promotion.normalize_stem("B")
