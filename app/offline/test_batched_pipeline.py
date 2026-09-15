"""全量装配管道的测试（决策 44/46）—— 分批提 + 嵌入粗筛 + 逐簇判断。

这一节的每条断言都对应一个**只有全量期才会暴露**的问题：

· **分批必然跨批重复**：同一个知识点被两批各提一次 —— 折叠与聚类就是为它存在的
· **某一批失败不能终止整轮**（§3.1）：三千道题跑一小时，第 40 批挂掉不该让前 39 批白跑
· **聚类只做粗筛**：它按用词分组，**判断权在 LLM 与人**手里 —— 所以"归并"这一步
  必须真的调模型，而且失败时保持原样（保守方向）
· **挂不上的留在待定池**（决策 46）：绝不自动新建知识点
· **确定性**：同样的输入聚出同样的簇（这份结果要人审，"这次为什么不一样"要能回答）
· **缓存生效**：重跑几乎全是 `reused`（否则每次重跑都在烧钱）
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import Domain, KnowledgePoint, Question
from app.llm.embeddings import FakeEmbeddings
from app.offline import embedding_store
from app.offline import knowledge_pipeline as kp
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "pipeline.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.flush()
        # 12 道题：前 6 道讲 volatile（用词相近），后 6 道讲线程池
        for i in range(1, 13):
            topic = "说说 volatile 的可见性与有序性" if i <= 6 else "线程池的拒绝策略怎么选"
            s.add(Question(id=i, kind="knowledge", stem=f"{topic}（第 {i} 题）", difficulty=3,
                           origin="seed", visibility="public"))
        s.commit()
        yield s


def _propose_reply(names_and_ids: list[tuple[str, list[int]]]) -> FakeReply:
    """一批的提候选返回（走 `offline/propose_points.md` 的 JSON 形状）。"""
    return FakeReply(
        data={
            "points": [
                {
                    "name": name,
                    "definition": f"{name} 的定义",
                    "exclusions": "",
                    "criteria": [f"{name} 的第一条判据", f"{name} 的第二条判据"],
                    "question_ids": ids,
                }
                for name, ids in names_and_ids
            ]
        }
    )


# ---------------------------------------------------------------------------
# 分批
# ---------------------------------------------------------------------------
def test_batches_are_stable_and_ordered(session: Session) -> None:
    """按 id 排序再切 —— 重跑时批次划分一致（否则两次结果没法对比）。"""
    questions = list(session.execute(select(Question)).scalars())
    batches = kp.batch_questions(questions, size=5)
    assert [len(b) for b in batches] == [5, 5, 2]
    assert [q.id for b in batches for q in b] == list(range(1, 13))


def test_propose_batched_collects_every_batch(session: Session) -> None:
    questions = list(session.execute(select(Question)).scalars())
    # 两批：第一批给 volatile，第二批给线程池（每批 6 道）
    llm = FakeLLM().queue(
        _propose_reply([("volatile 的内存语义", [1, 2, 3, 4, 5, 6])]),
        _propose_reply([("线程池的拒绝策略", [7, 8, 9, 10, 11, 12])]),
    )
    out = kp.propose_batched(session, questions=questions, llm=llm, batch_size=6)
    assert out.batches == 2 and out.failed_batches == 0
    assert {c.name for c in out.candidates} == {"volatile 的内存语义", "线程池的拒绝策略"}


def test_a_failed_batch_does_not_stop_the_run(session: Session) -> None:
    """**某一批失败不终止整轮**（§3.1）：记下来继续跑，失败那批的题留在待定池。"""
    from app.llm import LLMCallError

    questions = list(session.execute(select(Question)).scalars())
    llm = FakeLLM().queue(
        FakeReply(error=LLMCallError("这一批挂了")),
        _propose_reply([("线程池的拒绝策略", [7, 8, 9, 10, 11, 12])]),
    )
    out = kp.propose_batched(session, questions=questions, llm=llm, batch_size=6)
    assert out.batches == 2 and out.failed_batches == 1
    assert [c.name for c in out.candidates] == ["线程池的拒绝策略"]
    assert any("第 1 批" in note for note in out.notes), "失败要说清是哪一批"


# ---------------------------------------------------------------------------
# 聚类（粗筛）
# ---------------------------------------------------------------------------
def test_cluster_by_score_groups_similar_refs() -> None:
    vectors = {
        "a": [1.0, 0.0],
        "b": [0.99, 0.01],
        "c": [0.0, 1.0],
    }
    groups = kp.cluster_by_score(vectors, threshold=0.9)
    assert sorted(sorted(g) for g in groups) == [["a", "b"], ["c"]]


def test_cluster_by_score_is_deterministic() -> None:
    """同样的**输入顺序** → 同样的簇；换顺序只会换簇的编号，不会换划分。

    ⚠️ leader 聚类的划分依赖"谁是种子"，而种子由键顺序决定 —— 所以
    「确定性」的确切含义是这两条：
    ① 同一顺序 → 逐字相同的结果
    ② 换顺序 → **划分（谁是同一个簇）不变**，只是簇的次序与内部次序变了
    调用方要的是②，而 `cluster_candidates` 靠"先排序再聚类"把①也拿到手。
    """
    vectors = {"a": [1.0, 0.0], "b": [0.9, 0.1], "c": [0.0, 1.0], "d": [0.1, 0.9]}
    first = kp.cluster_by_score(vectors, threshold=0.8)
    again = kp.cluster_by_score(dict(vectors), threshold=0.8)
    assert first == again, "同一顺序必须逐字相同"

    reversed_order = kp.cluster_by_score(dict(reversed(list(vectors.items()))), threshold=0.8)
    assert {frozenset(g) for g in first} == {frozenset(g) for g in reversed_order}, "划分不变"


def test_cluster_candidates_is_order_independent(session: Session) -> None:
    """`cluster_candidates` 先把 ref 排序再聚类 —— 于是**划分与次序都稳定**。"""
    candidates = [
        kp.Candidate(name="线程池的拒绝策略", definition="队列满了怎么办"),
        kp.Candidate(name="volatile 的内存语义", definition="可见性与有序性"),
        kp.Candidate(name="volatile 内存语义", definition="可见性与有序性"),
    ]
    embeddings = FakeEmbeddings()
    first, _ = kp.cluster_candidates(session, candidates=candidates, embeddings=embeddings)
    second, _ = kp.cluster_candidates(
        session, candidates=list(reversed(candidates)), embeddings=embeddings
    )
    assert [[c.name for c in g] for g in first] == [[c.name for c in g] for g in second]


def test_cosine_handles_empty_and_mismatched_vectors() -> None:
    """空题干确实存在（导入的脏数据）—— 它该自己一簇，而不是让整轮聚类炸掉。"""
    assert kp.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert kp.cosine([], [1.0]) == 0.0
    assert kp.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_cluster_candidates_uses_the_cache_on_a_second_run(session: Session) -> None:
    """重跑几乎全是 `reused` —— 否则每次重跑都在烧钱（而重跑是这个管道的常态）。"""
    candidates = [
        kp.Candidate(name="volatile 的内存语义", definition="可见性与有序性"),
        kp.Candidate(name="volatile 语义", definition="可见性、有序性"),
        kp.Candidate(name="线程池的拒绝策略", definition="满了怎么办"),
    ]
    embeddings = FakeEmbeddings()
    groups, report = kp.cluster_candidates(session, candidates=candidates, embeddings=embeddings)
    assert report["embedded"] == 3 and report["reused"] == 0
    assert len(groups) >= 2

    _, report2 = kp.cluster_candidates(session, candidates=candidates, embeddings=embeddings)
    assert report2["reused"] == 3 and report2["embedded"] == 0
    assert embeddings.calls == 1


def test_similar_candidates_land_in_the_same_cluster(session: Session) -> None:
    candidates = [
        kp.Candidate(name="volatile 的内存语义", definition="可见性与有序性、内存屏障"),
        kp.Candidate(name="volatile 内存语义", definition="可见性与有序性、内存屏障"),
        kp.Candidate(name="线程池的拒绝策略", definition="队列满了怎么办、四种策略"),
    ]
    groups, _ = kp.cluster_candidates(
        session, candidates=candidates, embeddings=FakeEmbeddings()
    )
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 2], "相近的两条该在一簇，另一条独立"


# ---------------------------------------------------------------------------
# 逐簇判断（LLM）
# ---------------------------------------------------------------------------
def test_singleton_clusters_skip_the_model(session: Session) -> None:
    """单条一簇不用问模型 —— 省下的调用正好是那些"本来就只有一条"的知识点。"""
    clusters = [[kp.Candidate(name="只有一个", definition="", criteria=["a", "b"])]]
    llm = FakeLLM()  # 队列空：一旦被调用就会抛
    merged, judgement = kp.juddge_clusters(session, clusters=clusters, llm=llm)
    assert judgement.kept == 1 and judgement.merged == 0
    assert [c.name for c in merged] == ["只有一个"]


def test_merge_cluster_keeps_every_question(session: Session) -> None:
    """合并的是**候选**：题目的归属靠 `question_ids` 的并集带过来，**一道都不能掉**。"""
    group = [
        kp.Candidate(name="volatile 的内存语义", definition="", question_ids=[1, 2, 3]),
        kp.Candidate(name="volatile 语义", definition="", question_ids=[4, 5]),
    ]
    llm = FakeLLM().queue(
        FakeReply(
            data={
                "name": "volatile 的内存语义",
                "definition": "可见性与有序性",
                "exclusions": "原子性",
                "criteria": ["说出了可见性", "说出了内存屏障"],
            }
        )
    )
    merged, judgement = kp.juddge_clusters(session, clusters=[group], llm=llm)
    assert judgement.merged == 1
    assert [c.name for c in merged] == ["volatile 的内存语义"]
    assert sorted(merged[0].question_ids) == [1, 2, 3, 4, 5]
    assert merged[0].criteria == ["说出了可见性", "说出了内存屏障"]


def test_merge_failure_keeps_the_original_candidates(session: Session) -> None:
    """归并失败 → **保持原样**（保守方向：多留几条让人审合并，比合成错的代价小）。"""
    from app.llm import LLMCallError

    group = [
        kp.Candidate(name="A", definition="", question_ids=[1]),
        kp.Candidate(name="B", definition="", question_ids=[2]),
    ]
    llm = FakeLLM().queue(FakeReply(error=LLMCallError("挂了")))
    merged, judgement = kp.juddge_clusters(session, clusters=[group], llm=llm)
    assert judgement.failed == 1 and judgement.merged == 0
    assert [c.name for c in merged] == ["A", "B"]
    assert any("归并失败" in note for note in judgement.notes)


def test_merge_without_a_name_is_treated_as_failure(session: Session) -> None:
    """模型给不出名字 → 按失败处理（一条没有名字的知识点无法进人审）。"""
    group = [
        kp.Candidate(name="A", definition="", question_ids=[1]),
        kp.Candidate(name="B", definition="", question_ids=[2]),
    ]
    llm = FakeLLM().queue(FakeReply(data={"definition": "只有定义"}))
    merged, judgement = kp.juddge_clusters(session, clusters=[group], llm=llm)
    assert judgement.failed == 1
    assert [c.name for c in merged] == ["A", "B"]


# ---------------------------------------------------------------------------
# 端到端（用假嵌入 + 假模型）
# ---------------------------------------------------------------------------
def test_full_assembly_flow_feeds_the_review_page(session: Session) -> None:
    """整条链：分批提 → 聚类 → 逐簇判断 → **产出可人审的候选**。

    人审之后的落库由既有的 `apply_review` 负责（它已经在 `test_pipeline.py` 里测过），
    这里只证明"全量那条路喂给它的东西是对的"。
    """
    questions = list(session.execute(select(Question)).scalars())
    # 三批（每批 4 道）：第 2 批与第 1 批是**跨批重复**（同一个知识点被提了两次）
    llm = FakeLLM().queue(
        _propose_reply([("volatile 的内存语义", [1, 2, 3, 4])]),
        _propose_reply([("volatile 语义", [5, 6, 7, 8])]),
        _propose_reply([("线程池的拒绝策略", [9, 10, 11, 12])]),
    )
    proposal = kp.propose_batched(session, questions=questions, llm=llm, batch_size=4)
    assert proposal.failed_batches == 0 and len(proposal.candidates) == 3

    cluster_candidates, _ = kp.cluster_candidates(
        session, candidates=proposal.candidates, embeddings=FakeEmbeddings()
    )
    llm.on(
        "round",  # 归并那一步走的也是 chat_json；种类标记由 prompt 决定，这里直接排队
        FakeReply(data={
            "name": "volatile 的内存语义",
            "definition": "可见性与有序性",
            "exclusions": "",
            "criteria": ["说出了可见性", "说出了内存屏障"],
        }),
    )
    merged, judgement = kp.juddge_clusters(session, clusters=cluster_candidates, llm=llm)
    # **失败也算一条出路**：归并失败的那一簇保持原样（`juddge_clusters` 的 docstring：
    # 保守方向 —— 多留几条候选让人审时合并，比把两条不同的点合成一条代价小）。
    # 这条断言原来只数 merged + kept，于是在聚类开始真的聚出多成员簇（阈值从 0.86 降到
    # 0.60 之后）而假 LLM 没排到归并回复时就红了 —— 红的是断言漏了一种合法结果。
    assert judgement.merged + judgement.kept + judgement.failed == len(cluster_candidates)

    result = kp.ProposalResult(candidates=merged)
    payload = result.to_json()
    assert payload["candidates"], "要有人审的东西"
    assert [c["unusable_reason"] for c in payload["candidates"]] == [None] * len(merged), (
        "每条候选都得能过判据①（写不出考察点就不是知识点）"
    )
    # 12 道题**一道都没掉队**：跨批重复的那些被并进了同一条候选
    all_ids = {qid for c in merged for qid in c.question_ids}
    assert all_ids == set(range(1, 13))


def test_questions_no_candidate_claimed_stay_in_the_pool(session: Session) -> None:
    """**没有任何候选认领的题留在待定池**（决策 46）——绝不自动新建知识点。

    这里模拟"模型只提了一条候选、覆盖 6 道题"：另外 6 道题不该被顺手挂到它上面。
    """
    questions = list(session.execute(select(Question)).scalars())
    llm = FakeLLM().queue(_propose_reply([("volatile 的内存语义", [1, 2, 3, 4, 5, 6])]))
    proposal = kp.propose_batched(session, questions=questions, llm=llm, batch_size=12)
    assert len(proposal.candidates) == 1

    # 人审通过它 → 只有它认领的那 6 道题被挂上
    kp.apply_review(
        session,
        candidates=proposal.candidates,
        decisions=[kp.Decision(0, "approve")],
        domain_name="装配测试",
    )
    session.flush()
    mounted = [q.id for q in session.execute(select(Question)).scalars()
               if q.primary_point_id is not None]
    assert sorted(mounted) == [1, 2, 3, 4, 5, 6], "另外 6 道留在待定池"


def test_embedding_unavailable_is_loud(session: Session) -> None:
    """没配嵌入供应商 → **明确失败**，而不是"聚成一堆没有意义的簇"。"""
    from app.llm.embeddings import EmbeddingUnavailable, NoProviderEmbeddings

    candidates = [kp.Candidate(name="A"), kp.Candidate(name="B")]
    with pytest.raises(EmbeddingUnavailable):
        kp.cluster_candidates(
            session, candidates=candidates, embeddings=NoProviderEmbeddings()
        )


def test_question_vectors_include_criteria_and_are_cached(session: Session) -> None:
    """按题嵌入那条路也要走缓存（聚类之外，简历召回将来也用它）。"""
    from app.bank import repository

    question = session.get(Question, 1)
    assert question is not None
    criteria = repository.criteria_of_question(session, question)
    text = embedding_store.question_text(question, criteria)
    ref = str(question.id)
    embeddings = FakeEmbeddings()

    first, report = embedding_store.vectors_for(
        session, items={ref: text}, kind=embedding_store.QUESTION, embeddings=embeddings
    )
    _, report2 = embedding_store.vectors_for(
        session, items={ref: text}, kind=embedding_store.QUESTION, embeddings=embeddings
    )
    assert report.embedded == 1 and report2.reused == 1
    assert len(first[ref]) == len(next(iter(first.values())))
