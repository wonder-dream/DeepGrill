"""知识层构建管道的测试（决策 26 / 44 / 46）。

三条最要紧的性质：

· **判据① 是可执行的**："写不出考察点就不是知识点"由 `Candidate.unusable_reason`
  挡住，被挡下的候选进 `skipped`（**不静默丢弃**）
· **挂不上不新建**（决策 46）：模型给 `null` 或给一个不存在的 id，题就留在待定池，
  **绝不允许它悄悄改知识地图**
· **挂载不覆盖已确认的挂载**：改挂载是自修复的职责，一次性装配不该顺手改掉
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Domain, KnowledgePoint, Question
from app.llm import LLMCallError
from app.offline import knowledge_pipeline as kp
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "kp.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        for qid, stem in (
            (1, "说说 volatile 的作用，它能保证原子性吗？"),
            (2, "volatile 为什么能保证可见性？底层怎么实现的？"),
            (3, "线程池的核心线程数和最大线程数有什么区别？"),
        ):
            s.add(
                Question(id=qid, kind="knowledge", stem=stem, difficulty=3,
                         origin="seed", visibility="public")
            )
        s.commit()
        yield s


def _cand(name: str, criteria: list[str], qids: list[int] | None = None) -> dict:
    return {"name": name, "definition": f"考 {name}", "exclusions": "不考别的",
            "criteria": criteria, "question_ids": qids or [1]}


def test_propose_extracts_and_folds_duplicates(session: Session) -> None:
    """① 提候选 + 折叠同名（归一化："volatile " 与 "Volatile" 是同一条）。"""
    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(
        FakeReply(data={"points": [
            _cand("volatile", ["可见性与有序性", "底层内存屏障"], [1]),
            _cand("Volatile ", ["不保证原子性"], [2]),          # 归一化后同名
            _cand("线程池参数", ["核心与最大线程数", "拒绝策略"], [3]),
        ]})
    )
    result = kp.propose_points(questions, llm=llm)

    assert not result.llm_failed
    names = sorted(c.name for c in result.candidates)
    assert names == ["volatile", "线程池参数"], "同名候选要折成一条"
    volatile = next(c for c in result.candidates if kp.normalize_name(c.name) == "volatile")
    assert sorted(volatile.question_ids) == [1, 2], "折叠要并集题目"
    assert set(volatile.criteria) == {"可见性与有序性", "底层内存屏障", "不保证原子性"}


def test_normalize_name_is_idempotent_and_shape_insensitive() -> None:
    assert kp.normalize_name("Volatile") == kp.normalize_name(" volatile ")
    assert kp.normalize_name("线程池（参数）") == kp.normalize_name("线程池参数")
    assert kp.normalize_name("A-B") == kp.normalize_name("a b")


def test_candidate_without_enough_criteria_is_not_a_point(session: Session) -> None:
    """**判据① 的机器可判部分**：写不出考察点就不是知识点。"""
    bad = kp.Candidate(name="Java 并发", criteria=["理解并发"])
    assert bad.unusable_reason is not None
    assert "考察点" in bad.unusable_reason

    good = kp.Candidate(name="volatile", criteria=["可见性与有序性", "底层内存屏障"])
    assert good.unusable_reason is None


def test_apply_review_creates_confirmed_points_and_criteria(session: Session) -> None:
    """人审通过 → 建 `confirmed` 知识点 + 考察点（人审过才是 confirmed，决策 26）。"""
    candidates = [
        kp.Candidate(name="volatile", definition="考 volatile 的语义",
                     criteria=["可见性与有序性", "底层内存屏障"], question_ids=[1]),
        kp.Candidate(name="线程池参数", criteria=["核心与最大线程数", "拒绝策略"], question_ids=[3]),
    ]
    result = kp.apply_review(
        session,
        candidates=candidates,
        decisions=[kp.Decision(0, "approve"), kp.Decision(1, "approve")],
        domain_name="Java 并发",
    )
    session.commit()

    assert result.created_points == 2
    points = session.execute(select(KnowledgePoint)).scalars().all()
    assert {p.name for p in points} == {"volatile", "线程池参数"}
    assert all(p.status == "confirmed" for p in points)
    assert all(p.origin == "proposed" for p in points)
    volatile = next(p for p in points if p.name == "volatile")
    criteria = session.execute(select(Criterion).where(Criterion.point_id == volatile.id)).scalars().all()
    assert len(criteria) == 2
    # 题目被挂上了（只填空的）
    assert session.get(Question, 1).primary_point_id == volatile.id


def test_apply_review_reports_what_machine_rules_blocked(session: Session) -> None:
    """被判据① 挡下的候选进 `skipped` —— **不静默丢弃**。"""
    candidates = [kp.Candidate(name="Java 并发", criteria=["理解并发"], question_ids=[1])]
    result = kp.apply_review(
        session, candidates=candidates,
        decisions=[kp.Decision(0, "approve")], domain_name="Java 并发",
    )
    assert result.created_points == 0
    assert result.skipped and "Java 并发" in result.skipped[0]


def test_apply_review_can_rename_and_merge(session: Session) -> None:
    """人审能改名、能合并 —— 合并时被并的题目要保全。"""
    candidates = [
        kp.Candidate(name="volatile", criteria=["可见性", "内存屏障"], question_ids=[1]),
        kp.Candidate(name="volatile 语义", criteria=["不保证原子性"], question_ids=[2]),
    ]
    result = kp.apply_review(
        session,
        candidates=candidates,
        decisions=[
            kp.Decision(0, "approve", name="volatile"),
            kp.Decision(1, "merge_into", merge_into=0),
        ],
        domain_name="Java 并发",
    )
    session.commit()

    assert result.created_points == 1 and result.merged == 1
    point = session.execute(select(KnowledgePoint)).scalars().one()
    assert point.name == "volatile"
    # 被合并那条的题也要挂到目标上（题目不能丢）
    assert session.get(Question, 1).primary_point_id == point.id
    assert session.get(Question, 2).primary_point_id == point.id


def test_apply_review_reject_creates_nothing(session: Session) -> None:
    candidates = [kp.Candidate(name="volatile", criteria=["可见性", "内存屏障"], question_ids=[1])]
    result = kp.apply_review(
        session, candidates=candidates,
        decisions=[kp.Decision(0, "reject")], domain_name="Java 并发",
    )
    session.commit()
    assert result.created_points == 0 and result.rejected == 1
    assert session.execute(select(KnowledgePoint)).scalars().all() == []


def test_mount_assigns_only_known_points(session: Session) -> None:
    session.add(Domain(id=1, name="Java 并发"))
    session.add(KnowledgePoint(id=7, domain_id=1, name="volatile", status="confirmed"))
    session.commit()

    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(
        FakeReply(data={"assignments": [
            {"question_id": 1, "point_id": 7, "confidence": "high"},
            {"question_id": 2, "point_id": 7, "confidence": "medium"},
        ]})
    )
    result = kp.mount_questions(session, questions=questions[:2], llm=llm)
    session.commit()

    assert result.mounted == 2
    assert session.get(Question, 1).primary_point_id == 7


def test_mount_leaves_unknown_targets_for_review_and_never_creates_points(session: Session) -> None:
    """**决策 46**：挂不上就进待定池，**绝不允许自动新建知识点**。"""
    session.add(Domain(id=1, name="Java 并发"))
    session.add(KnowledgePoint(id=7, domain_id=1, name="volatile", status="confirmed"))
    session.commit()

    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(
        FakeReply(data={"assignments": [
            {"question_id": 1, "point_id": 999},   # 不存在的知识点
            {"question_id": 2, "point_id": None},  # 模型自己说挂不上
            # 题 3 干脆没出现在结果里
        ]})
    )
    result = kp.mount_questions(session, questions=questions, llm=llm)
    session.commit()

    assert result.mounted == 0
    assert result.left_for_review == 3
    assert len(session.execute(select(KnowledgePoint)).scalars().all()) == 1, "不许新建知识点"
    assert all(q.primary_point_id is None for q in session.execute(select(Question)).scalars())


def test_mount_does_not_overwrite_existing_mounts(session: Session) -> None:
    """改挂载是**自修复**的职责；一次性装配不该顺手改掉已确认的挂载。"""
    session.add(Domain(id=1, name="Java 并发"))
    session.add(KnowledgePoint(id=7, domain_id=1, name="volatile", status="confirmed"))
    session.add(KnowledgePoint(id=8, domain_id=1, name="线程池参数", status="confirmed"))
    session.commit()
    q = session.get(Question, 1)
    assert q is not None
    q.primary_point_id = 8
    session.commit()

    llm = FakeLLM().queue(FakeReply(data={"assignments": [{"question_id": 1, "point_id": 7}]}))
    result = kp.mount_questions(session, questions=[q], llm=llm)
    session.commit()

    assert result.mounted == 0, "已挂载的题不该被重新挂"
    assert session.get(Question, 1).primary_point_id == 8


def test_mount_failure_sends_the_batch_to_review(session: Session) -> None:
    session.add(Domain(id=1, name="Java 并发"))
    session.add(KnowledgePoint(id=7, domain_id=1, name="volatile", status="confirmed"))
    session.commit()

    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(FakeReply(error=LLMCallError("模型挂了")))
    result = kp.mount_questions(session, questions=questions, llm=llm)

    assert result.mounted == 0
    assert result.left_for_review == len(questions), "整批进待定池，而不是静默丢掉"
    assert all(q.primary_point_id is None for q in session.execute(select(Question)).scalars())


def test_mount_without_confirmed_points_does_nothing(session: Session) -> None:
    """一个已确认的知识点都没有时**不调模型** —— 没有可挂的目标，调了也是浪费。"""
    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM()
    result = kp.mount_questions(session, questions=questions, llm=llm)
    assert llm.calls == []
    assert result.mounted == 0


def test_propose_failure_is_reported_not_swallowed(session: Session) -> None:
    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(FakeReply(error=LLMCallError("模型挂了")))
    result = kp.propose_points(questions, llm=llm)
    assert result.llm_failed is True
    assert result.note


def test_prerequisite_edges_are_deliberately_not_implemented() -> None:
    """决策 48：从名字推边基本在猜，**故意留空**。

    这条测试的意义是让"为什么没有前置边"这件事**可执行地被看见** ——
    将来有人实现它时，这条会红，于是他必须去读 `derive_edges` 的 docstring
    （那里写了决策 48 要求的两类有依据的边）。
    """
    assert kp.derive_edges() == []


# ---------------------------------------------------------------------------
# 巨簇拆分（决策 81）
# ---------------------------------------------------------------------------
def test_oversized_clusters_are_never_returned() -> None:
    """**上限是硬约束**：一个知识点最多影响多少道题的掌握度。

    单链接聚类会传递闭包（A~B、B~C 就把 A 和 C 拴在一起），实测链出过 655 道题的
    大团。这一团互相之间的余弦都很高、拆不开，于是只能按顺序切块 ——
    但**绝不能原样返回一个超限的簇**。
    """
    refs = [f"r{i}" for i in range(250)]
    ordered = {ref: [1.0, 0.0] for ref in refs}   # 全部同向：任何阈值都拆不开
    groups = kp._split_oversized(ordered, [refs], max_size=60)
    assert len(groups) > 1, "250 条必须被切开"
    assert all(len(g) <= 60 for g in groups), [len(g) for g in groups]
    assert sorted(r for g in groups for r in g) == sorted(refs), "切开但一条都不能丢"


def test_a_genuinely_big_cluster_is_split_by_lower_level_clustering() -> None:
    """能靠**抬阈值再聚**拆开的，就该按语义拆，而不是按顺序切。

    两团互相正交的向量（同一团内余弦 1.0、跨团 0.0）：在 0.60 下它们本来就会被分开，
    这里直接喂一个"已经被链在一起"的 120 条大簇，看它能不能拆回两团。
    """
    left = {f"a{i}": [1.0, 0.0] for i in range(60)}
    right = {f"b{i}": [0.0, 1.0] for i in range(60)}
    ordered = {**left, **right}
    groups = kp._split_oversized(ordered, [list(ordered)], max_size=60)
    assert sorted(len(g) for g in groups) == [60, 60]
    assert {frozenset(g) for g in groups} == {frozenset(left), frozenset(right)}


def test_splitting_recurses_until_every_piece_fits() -> None:
    """**三级链条**：一条 240 条的长链要在两层里拆完，不是只拆一层。

    四个 60 条的小团，相邻两团的余弦分别是 0.75 / 0.68 / 0.62（夹角精心摆过），
    非相邻的都低于 0.60 —— 于是：

    * 0.60：整条链连通 → 一个 240 的簇
    * 0.65：`{g1,g2,g3}`（180，仍超限）与 `{g4}`
    * 0.70：`{g1,g2}`（120，仍超限）与 `{g3}`
    * 0.75：还拆不开 → 按顺序切块

    这条测试存在的唯一原因是**上一层只会拆一层**这一点测不出来（去掉递归的变异在那
    个输入上语义等价，抓不住）；有了三级链条，去掉递归就会留下 180 与 120 的超限簇。
    """
    import math

    angles = [0.0, 41.41, 88.57, 140.25]   # cos 相邻 ≈ 0.75 / 0.68 / 0.62
    ordered: dict[str, list[float]] = {}
    for index, angle in enumerate(angles):
        vector = [math.cos(math.radians(angle)), math.sin(math.radians(angle))]
        for i in range(60):
            ordered[f"g{index}_{i}"] = vector

    groups = kp._split_oversized(ordered, [list(ordered)], max_size=60)
    assert all(len(g) <= 60 for g in groups), sorted((len(g) for g in groups), reverse=True)
    assert len(groups) == 4, f"240 条应当拆成 4 块：{[len(g) for g in groups]}"
    assert sorted(r for g in groups for r in g) == sorted(ordered), "一条都不能丢"


# ---------------------------------------------------------------------------
# 关联知识点（决策 24 / 80）：一道综合题可以同时挂在多个点上
# ---------------------------------------------------------------------------
def _points(session: Session, *ids: int) -> None:
    session.add(Domain(id=1, name="D"))
    for pid in ids:
        session.add(
            KnowledgePoint(id=pid, domain_id=1, name=f"点{pid}", status="confirmed")
        )
    session.commit()


def test_mounting_can_attach_related_points(session: Session) -> None:
    """**决策 80**：挂载不再只写一个点。

    在这之前 `prompts/offline/mount_questions.md` 的契约是"每题给出**一个**
    `point_id`"，于是 `question_points`（决策 24 的关联轴）在所有写入路径上都只有
    0/1 行 —— 而「一道综合题同时更新多个知识点的掌握度」那条验收判据要求 ≥2。
    """
    from app.db.models import QuestionPoint

    _points(session, 7, 8)
    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(
        FakeReply(data={"assignments": [
            {"question_id": 1, "point_id": 7, "related_point_ids": [8]},
        ]})
    )
    result = kp.mount_questions(session, questions=questions[:1], llm=llm)
    session.commit()

    assert result.mounted == 1
    assert result.details[0]["related_point_ids"] == [8]
    rows = session.execute(select(QuestionPoint)).scalars().all()
    assert [(r.question_id, r.point_id) for r in rows] == [(1, 8)]


def test_related_points_are_cleaned_before_they_are_written(session: Session) -> None:
    """未知 id 丢掉（**绝不新建**，与主知识点同一条纪律）、重复的丢掉、超过 3 个截断。

    上限不是洁癖：每多一个关联点就是多一套考察点进判分，而且会让那个点的掌握度
    **凭空多一格** —— 模型偶尔会把半个清单抄回来。
    """
    from app.db.models import KnowledgePoint as K
    from app.db.models import QuestionPoint

    _points(session, 7, 8, 9, 10, 11)
    questions = session.execute(select(Question)).scalars().all()
    llm = FakeLLM().queue(
        FakeReply(data={"assignments": [
            {"question_id": 1, "point_id": 7,
             "related_point_ids": [7, 8, 8, 999, "x", 9, 10, 11]},
        ]})
    )
    kp.mount_questions(session, questions=questions[:1], llm=llm)
    session.commit()

    written = {r.point_id for r in session.execute(select(QuestionPoint)).scalars()}
    assert written == {8, 9, 10}, f"去重 / 丢未知 / 截到 3 个：{written}"
    assert len(session.execute(select(K)).scalars().all()) == 5, "不许新建知识点"


def test_a_related_point_brings_its_criteria_into_scoring(session: Session) -> None:
    """**整件事的重点**：关联点的考察点要进判分。

    只写 `question_points` 而不把关联点的判据算进来，格子照样只亮一个 ——
    挂载挂上了，掌握度矩阵上什么都不会发生。
    """
    from app.bank import repository as bank_repository
    from app.db.models import Criterion, QuestionPoint
    from app.db.models import KnowledgePoint as K

    session.add(Domain(id=1, name="D"))
    session.add(K(id=7, domain_id=1, name="缓存一致性", status="confirmed"))
    session.add(K(id=8, domain_id=1, name="限流算法", status="confirmed"))
    # 先 flush 知识点再插考察点：映射是命令式的（没有 relationship），
    # 不 flush 就是 FOREIGN KEY constraint failed（同一坑踩过多次）
    session.flush()
    session.add(Criterion(point_id=7, seq=1, text="更新顺序", shared=0))
    session.add(Criterion(point_id=8, seq=1, text="令牌桶与漏桶", shared=0))
    session.flush()
    question = session.get(Question, 1)
    assert question is not None
    question.primary_point_id = 7
    session.add(QuestionPoint(question_id=1, point_id=8))
    session.commit()

    texts = [c.text for c in bank_repository.criteria_of_question(session, question)]
    assert texts == ["更新顺序", "令牌桶与漏桶"], "关联点的判据没进判分"


def test_merge_points_moves_everything_and_deletes_the_source(session: Session) -> None:
    """决策 83：事后合并两个知识点 —— 题、关联、考察点都要跟着走，源点删掉。

    只搬题会留下"关联指向一个不存在的点"；只搬题与关联会留下孤儿考察点
    （历史 `attempts.hits` 存的是 criterion id，删了它历史就断链）。
    """
    from app.db.models import Criterion, KnowledgePoint, QuestionPoint

    session.add(Domain(id=1, name="D"))
    session.add(KnowledgePoint(id=71, domain_id=1, name="源点", status="confirmed"))
    session.add(KnowledgePoint(id=72, domain_id=1, name="目标点", status="confirmed"))
    session.flush()
    session.add(Criterion(point_id=71, seq=1, text="源的判据一", shared=0))
    session.add(Criterion(point_id=71, seq=2, text="源的判据二", shared=0))
    question = session.get(Question, 1)
    assert question is not None
    question.primary_point_id = 71
    session.add(QuestionPoint(question_id=2, point_id=71))
    session.add(QuestionPoint(question_id=1, point_id=72))
    # ⚠️ **同一个题号在两边都有** —— 这才是"搬关联会撞 `(question_id, point_id)` 主键"
    # 那条分支的真正触发条件。第一版只有"不同题号"，于是"去掉去重分支"的变异**抓不住**
    # （实测：3 条变异里漏掉的就是它）。
    session.add(QuestionPoint(question_id=2, point_id=72))
    session.commit()

    dry = kp.merge_points(session, source_id=71, target_id=72, dry_run=True)
    assert (dry.questions, dry.related, dry.criteria) == (1, 1, 2)
    assert dry.deleted is False and session.get(KnowledgePoint, 71) is not None, "演练不许改数"

    report = kp.merge_points(session, source_id=71, target_id=72)
    session.commit()
    assert report.deleted is True
    assert session.get(KnowledgePoint, 71) is None, "源点要删掉"
    assert session.get(Question, 1).primary_point_id == 72
    related = sorted(
        (r.question_id, r.point_id) for r in session.execute(select(QuestionPoint)).scalars()
    )
    assert related == [(1, 72), (2, 72)], f"关联要并过去且不撞主键：{related}"
    assert all(
        c.point_id == 72 for c in session.execute(select(Criterion)).scalars()
    ), "考察点要跟着走"


def test_merge_points_refuses_nonsense(session: Session) -> None:
    from app.db.models import KnowledgePoint
    from app.errors import InvalidInput

    session.add(Domain(id=1, name="D"))
    session.add(KnowledgePoint(id=71, domain_id=1, name="点", status="confirmed"))
    session.commit()
    with pytest.raises(InvalidInput):
        kp.merge_points(session, source_id=71, target_id=71)
    with pytest.raises(InvalidInput):
        kp.merge_points(session, source_id=999, target_id=71)
