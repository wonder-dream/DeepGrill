"""「生成题」这条内容来源的测试（决策 4 / 5）。

公共池此前只能靠 v1 导入与用户晋升长大，而基线写着内容来源三类里有**生成题（LLM）**。
这一笔补齐它。四件事必须同时成立，缺一条这条路就会长出一批脏数据：

· **入口是已确认的知识点**：生成题不产生新判据，判分读的是那个点**人审过的**判据 ——
  所以它天然绕不过骨架（决策 46）
· **过门禁才插入**（决策 5）：系统写的题也一样跑长度 / 题型 / 去重 / 考察点四条检查
· **幂等**：题干已存在就跳过（离线任务会被重跑，"重跑一次多出十道一样的题"最难收拾）
· **不静默**：模型失败、门禁挡下、知识点没有判据 —— 三种都要进报告
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
from app.offline import generation
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "generation.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(Domain(id=1, name="Java 并发"))
        # 一个已确认的知识点（有判据）—— 补题的目标
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile 的内存语义",
                             status="confirmed", origin="proposed"))
        # 一个草稿点（私有题集的锚点那种）—— 不该被补题
        s.add(KnowledgePoint(id=2, domain_id=1, name="私有题集（用户 9）",
                             status="draft", origin="manual"))
        # 一个已确认但**没有判据**的点 —— 补题应当拒绝并说清原因
        s.add(KnowledgePoint(id=3, domain_id=1, name="没有判据的点",
                             status="confirmed", origin="proposed"))
        s.flush()
        s.add(Criterion(id=1, point_id=1, seq=1, text="说出了可见性", shared=0))
        s.add(Criterion(id=2, point_id=1, seq=2, text="说出了内存屏障", shared=0))
        # 一道**已存在**的公共题（用来验"重复跳过"）
        s.add(Question(id=1, kind="knowledge", stem="说说 volatile 的可见性是怎么保证的",
                       difficulty=3, primary_point_id=1, origin="seed", visibility="public"))
        s.commit()
        yield s


def _point(session: Session, point_id: int = 1) -> KnowledgePoint:
    """取一个知识点（带断言）。

    `session.get()` 的类型是 `X | None`，下一行就当非空用 —— 加一句断言之后，
    失败会发生在**那一行**，而不是很远处一句 `AttributeError: 'NoneType'`。
    """
    point = session.get(KnowledgePoint, point_id)
    assert point is not None, f"夹具里应当有知识点 {point_id}"
    return point


def _reply(*stems: str, kind: str = "knowledge", difficulty: int = 3) -> FakeReply:
    return FakeReply(
        data={
            "questions": [
                {
                    "stem": stem,
                    "kind": kind,
                    "difficulty": difficulty,
                    "reference_answer": "",
                    "covers": [1, 2],
                }
                for stem in stems
            ]
        }
    )


def _questions(session: Session) -> list[Question]:
    return list(session.execute(select(Question).order_by(Question.id)).scalars())


# ---------------------------------------------------------------------------
# 候选池：只收"缺题的已确认知识点"
# ---------------------------------------------------------------------------
def test_only_confirmed_points_are_candidates(session: Session) -> None:
    needs = generation.points_needing_questions(session)
    ids = {need.point.id for need in needs}
    assert 1 in ids, "已确认且缺题的点要进来"
    assert 2 not in ids, "草稿点（私有题集的锚点）不该被补公共题"


def test_points_with_enough_questions_are_skipped(session: Session) -> None:
    for index in range(generation.TARGET_PER_POINT):
        session.add(Question(id=100 + index, kind="knowledge", stem=f"已有题 {index}",
                             difficulty=3, primary_point_id=1, origin="seed",
                             visibility="public"))
    session.flush()
    assert 1 not in {need.point.id for need in generation.points_needing_questions(session)}


def test_needs_are_sorted_by_how_much_is_missing(session: Session) -> None:
    """缺得多的排在前面 —— CLI 限流（`--limit`）时先补最缺的。"""
    needs = generation.points_needing_questions(session)
    missing = [need.missing for need in needs]
    assert missing == sorted(missing, reverse=True)


def test_private_questions_do_not_count_as_coverage(session: Session) -> None:
    """**只有公共题算数**：私有题再多也不该让一个知识点看起来"已经有题了"。"""
    session.add(Question(id=200, kind="knowledge", stem="私有题", difficulty=3,
                         primary_point_id=1, origin="generated", owner_user_id=1,
                         visibility="private"))
    session.flush()
    need = next(n for n in generation.points_needing_questions(session) if n.point.id == 1)
    assert need.have == 1, "公共题只有那一道种子题"


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def test_generated_questions_land_in_the_public_pool(session: Session) -> None:
    llm = FakeLLM().queue(_reply("volatile 能保证原子性吗，为什么", "内存屏障是怎么起作用的"))
    report = generation.generate_for_point(session, point=_point(session),
                                           count=2, llm=llm)
    assert report.created == 2, report.summary()

    created = [q for q in _questions(session) if q.origin == "generated"]
    assert len(created) == 2
    for question in created:
        assert question.owner_user_id is None, "进公共池"
        assert question.visibility == "public"
        assert question.primary_point_id == 1, "挂在目标知识点上（生成题不产生新判据）"
        assert question.answer_tier == "long_tail", "没给参考答案 → 长尾档（只有判据）"


def test_generated_question_with_a_reference_answer_is_common_tier(session: Session) -> None:
    """给了参考答案 → 泛用档（决策 12 的两档）。"""
    reply = FakeReply(data={"questions": [{
        "stem": "volatile 与 synchronized 的区别是什么",
        "kind": "knowledge", "difficulty": 3,
        "reference_answer": "可见性与有序性 vs 互斥。", "covers": [1],
    }]})
    generation.generate_for_point(session, point=_point(session),
                                  count=1, llm=FakeLLM().queue(reply))
    created = [q for q in _questions(session) if q.origin == "generated"][0]
    assert created.answer_tier == "common"
    assert created.reference_answer


def test_duplicate_stems_are_skipped(session: Session) -> None:
    """**幂等**：题干已存在就跳过 —— 离线任务会被重跑。"""
    llm = FakeLLM().queue(_reply("说说 volatile 的可见性是怎么保证的"))
    report = generation.generate_for_point(session, point=_point(session),
                                           count=1, llm=llm)
    assert report.created == 0 and report.skipped_duplicate == 1
    assert len(_questions(session)) == 1, "库里还是只有原来那一道"


def test_running_twice_creates_nothing_new(session: Session) -> None:
    reply = _reply("volatile 能保证原子性吗，为什么")
    generation.generate_for_point(session, point=_point(session),
                                  count=1, llm=FakeLLM().queue(reply))
    second = generation.generate_for_point(
        session, point=_point(session), count=1,
        llm=FakeLLM().queue(_reply("volatile 能保证原子性吗，为什么")),
    )
    assert second.created == 0 and second.skipped_duplicate == 1


# ---------------------------------------------------------------------------
# 门禁（决策 5：系统写的题一样过闸）
# ---------------------------------------------------------------------------
def test_questions_that_fail_the_gate_are_not_inserted(session: Session) -> None:
    """太短的题干过不了闸 —— **不插入**，而且要留下"为什么"。"""
    llm = FakeLLM().queue(_reply("volatile", "volatile 能保证原子性吗，为什么"))
    report = generation.generate_for_point(session, point=_point(session),
                                           count=2, llm=llm)
    assert report.created == 1
    assert len(report.rejected) == 1 and "门禁" in report.rejected[0] or report.rejected
    stems = {q.stem for q in _questions(session)}
    assert "volatile" not in stems, "过不了闸的题不许留在库里"


def test_gate_still_applies_the_duplicate_check_within_one_batch(session: Session) -> None:
    """同一批里出现两道一样的题：第二道**进不来**（题干存在性检查在前，门禁在后）。

    两道闸分工：`stem_exists` 挡"库里已经有"的（便宜，先跑），门禁的去重挡
    "这一批里自己重复"的（题目插入之后才看得见）。这里两道闸都会碰到，结果是
    **库里只多一道**。
    """
    llm = FakeLLM().queue(_reply("同一条题干出现了两次，这一条够长吗", "同一条题干出现了两次，这一条够长吗"))
    report = generation.generate_for_point(session, point=_point(session),
                                           count=2, llm=llm)
    assert report.created == 1
    assert report.skipped_duplicate + len(report.rejected) == 1
    assert len([q for q in _questions(session) if q.stem.startswith("同一条题干")]) == 1


def test_a_question_without_difficulty_gets_a_sane_default(session: Session) -> None:
    """模型给了脏难度（或没给）→ 钳进 1-5，别让一条脏输出毁掉整批（§4.3）。"""
    reply = FakeReply(data={"questions": [
        {"stem": "volatile 的可见性靠什么保证，说说机制", "kind": "knowledge",
         "difficulty": 99, "covers": [1]},
        {"stem": "另一道关于 volatile 的题，也要够长才行", "kind": "knowledge",
         "difficulty": None, "covers": [1]},
    ]})
    generation.generate_for_point(session, point=_point(session),
                                  count=2, llm=FakeLLM().queue(reply))
    levels = {q.difficulty for q in _questions(session) if q.origin == "generated"}
    assert levels == {5, 3}


# ---------------------------------------------------------------------------
# 不静默
# ---------------------------------------------------------------------------
def test_model_failure_is_reported_and_creates_nothing(session: Session) -> None:
    llm = FakeLLM().queue(FakeReply(error=LLMCallError("模型挂了")))
    report = generation.generate_for_point(session, point=_point(session),
                                           count=1, llm=llm)
    assert report.llm_failed is True
    assert "模型挂了" in report.note
    assert report.created == 0
    assert len(_questions(session)) == 1, "什么都没插进去"


def test_a_point_without_criteria_is_refused_with_a_reason(session: Session) -> None:
    """**写不出考察点就不是知识点**（判据①）：没有判据的题无法被客观评判。"""
    report = generation.generate_for_point(session, point=_point(session, 3),
                                           count=1, llm=FakeLLM())
    assert report.created == 0
    assert "考察点" in report.note
    assert report.rejected, "要留下为什么"


def test_report_counts_tokens_for_the_ledgerless_offline_call(session: Session) -> None:
    """离线调用没有"该付费的用户"，所以用量进**任务报告**（§3.8：能回答花在哪）。"""

    class CountingLLM(FakeLLM):
        def __init__(self, **kw) -> None:
            super().__init__(**kw)
            self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0}

        def chat(self, *args, **kwargs):
            reply = super().chat(*args, **kwargs)
            self.usage_total["prompt_tokens"] += 120
            self.usage_total["completion_tokens"] += 80
            return reply

    llm = CountingLLM().queue(_reply("volatile 与内存屏障的关系是什么"))
    report = generation.generate_for_point(session, point=_point(session),
                                           count=1, llm=llm)
    assert report.tokens == 200
    assert "200 token" in report.summary()


# ---------------------------------------------------------------------------
# 批量与任务
# ---------------------------------------------------------------------------
def test_generate_for_missing_walks_every_point(session: Session) -> None:
    llm = FakeLLM()
    for _ in range(len(generation.points_needing_questions(session))):
        llm.queue(_reply("一道够长的生成题，用来验证批量这条路"))
    reports = generation.generate_for_missing(session, llm=llm)
    assert len(reports) == len(generation.points_needing_questions(session)) + 1 - 1
    assert sum(r.created for r in reports) >= 1


def test_the_cli_passes_settings_to_get_llm(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 那条路也一样：`get_llm` 必须收到一个 **Settings 实例**。

    `get_llm(settings: Settings = Depends(get_settings))` 是给 FastAPI 依赖注入写的 ——
    直接 `get_llm()` 拿到的是那个 `Depends` 对象，而它的报错发生在**属性访问**那一刻
    （`'Depends' object has no attribute 'llm_api_key'`），离真正的错处很远。
    实测这条路与任务那条路都踩过同一个坑（CLI 那处是**真跑命令时**才暴露的），
    所以两处各有一条测试。

    ⚠️ 替身**要求**收到 Settings：不检查参数的话，"忘了传"这件事就测不出来。
    """
    from app.cli import cmd_generate
    from app.config import Settings

    db_path = Path(str(session.get_bind().url.database))
    seen: dict[str, object] = {}

    def fake_get_llm(settings=None, **kwargs):
        seen["settings"] = settings
        return FakeLLM().queue(_reply("volatile 的可见性靠什么保证呢，说说看"))

    monkeypatch.setattr("app.deps.get_llm", fake_get_llm)
    code = cmd_generate(Settings(database_path=db_path), point=1, count=1, enqueue=False)

    assert code == 0
    assert isinstance(seen["settings"], Settings), (
        "必须显式传 Settings —— 否则拿到的是 `Depends` 对象，而报错发生在很远的地方"
    )


def test_the_cli_enqueue_path_only_enqueues(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--enqueue` 只投任务：它**不该**顺手调模型（那是 worker 的活）。"""
    from sqlalchemy import select as _select

    from app.cli import cmd_generate
    from app.config import Settings
    from app.db import create_db_engine, create_session_factory
    from app.offline import jobs, tasks  # noqa: F401  （注册副作用）

    db_path = Path(str(session.get_bind().url.database))
    calls = {"n": 0}

    def fake_get_llm(settings=None, **kwargs):
        calls["n"] += 1
        return FakeLLM()

    monkeypatch.setattr("app.deps.get_llm", fake_get_llm)
    code = cmd_generate(Settings(database_path=db_path), point=1, count=1, enqueue=True)
    assert code == 0
    assert calls["n"] == 0, "投递不该调模型"

    with create_session_factory(create_db_engine(db_path))() as s:
        kinds = [j.kind for j in s.execute(_select(jobs.Job)).scalars()]
    assert kinds == ["generate_public_questions"]


def test_it_is_registered_as_an_offline_task(session: Session) -> None:
    from app.offline import jobs, tasks  # noqa: F401

    assert "generate_public_questions" in jobs.TASKS
    assert jobs.TASKS["generate_public_questions"].idempotent is True


def test_the_job_reports_a_missing_point(session: Session) -> None:
    from app.offline import jobs

    outcome = jobs.TASKS["generate_public_questions"].run(session, {"point_id": 999})
    assert outcome is not None
    assert outcome["created"] == 0
    assert "不存在" in outcome["message"]


def test_the_job_runs_for_one_point(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """任务那条路走的是同一份实现（`offline.generation`），只是入口不同。"""
    from app.offline import jobs

    fake = FakeLLM().queue(_reply("volatile 的可见性到底靠什么保证呢"))
    monkeypatch.setattr("app.deps.get_llm", lambda *a, **kw: fake)
    outcome = jobs.TASKS["generate_public_questions"].run(
        session, {"point_id": 1, "count": 1}
    )
    assert outcome is not None
    assert outcome["created"] == 1
    assert outcome["points"][0]["point_name"] == "volatile 的内存语义"
    assert outcome["points"][0]["created"] == 1
