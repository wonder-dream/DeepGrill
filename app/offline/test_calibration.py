"""标定工具的测试（§未决 6 / 决策 72）。

这个工具的价值全在**数字和出处对不对**上，所以测试盯的是三类东西：

· **口径**：只数公共题（私有题不该让一个点看起来"有题了"）
· **分位/直方图的边界**：报出来的值必须是样本里真有的（不插值）
· **§它必须是只读的**：`--live` 那条路会 `start_drill` / `explain`，两者都写库 ——
  所以它跑在**库的副本**上。这条契约靠"跑完之后真库一行没变"来钉住，
  而不是靠注释说"我们不写"。
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Attempt,
    Domain,
    Evaluation,
    Interview,
    KnowledgePoint,
    Question,
    QuotaLedger,
    Session_,
    User,
)
from app.offline import calibration
from migrations._runner import migrate
from tests.fakes import FakeLLM, round_reply


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "calibrate.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        # 迁移里已经建了 id=1 的 owner（`owner@local`），所以这里从 2 开始
        s.add(User(id=2, email="a@b.c", username="阿甲", password_hash="x"))
        s.add(Domain(id=1, name="Java 并发"))
        s.add(KnowledgePoint(id=1, domain_id=1, name="volatile", status="confirmed"))
        s.add(KnowledgePoint(id=2, domain_id=1, name="synchronized", status="confirmed"))
        s.flush()
        # 点 2 只有一道私有题 —— 它不该让点 2 看起来"已经有题了"
        s.add(Question(id=1, kind="knowledge", stem="volatile 保证什么", difficulty=3,
                       primary_point_id=1, origin="seed"))
        s.add(Question(id=2, kind="knowledge", stem="内存屏障有哪几种", difficulty=5,
                       primary_point_id=1, origin="seed"))
        s.add(Question(id=3, kind="knowledge", stem="volatile 保证什么", difficulty=3,
                       primary_point_id=1, origin="seed"))
        s.add(Question(id=4, kind="design", stem="你怎么用它", difficulty=2,
                       primary_point_id=2, origin="generated", owner_user_id=2,
                       visibility="private"))

        # 两个题会话：一个 3 轮（含 1 轮追问）、一个 1 轮
        s.flush()
        s.add(Interview(id=1, user_id=2, mode="drill", plan={}, status="finished",
                        quota_charged=1))
        s.add(Session_(id=1, interview_id=1, question_id=1, seq=1, max_rounds=3,
                       status="finished"))
        s.add(Session_(id=2, interview_id=1, question_id=2, seq=2, max_rounds=3,
                       status="finished"))
        # 显式 flush：Attempt / Evaluation 与 Session_ 之间没有 `relationship()`（映射是
        # 命令式的），所以 SQLAlchemy 不知道谁该先插 —— 不 flush 就是外键报错，
        # 而且报错位置指在最后那条 INSERT 上（实测为此排查了两轮）。
        s.flush()
        for round_no, followup in ((1, 0), (2, 1), (3, 1)):
            s.add(Attempt(session_id=1, round_no=round_no, is_followup=followup))
        s.add(Attempt(session_id=2, round_no=1, is_followup=0))
        s.add(Evaluation(session_id=1, total_score=55.0, status="ok"))
        # ⚠️ 一个题会话**只有一行** `evaluations`（迁移 0005 的唯一索引，决策 89）——
        # 所以"失败的那条不进分位"这件事只能靠**另一个题会话**来表达。原来这里给
        # session 2 插了两行（88 成功 + 90 失败），那是 0005 之前的形状。
        s.add(Evaluation(session_id=2, total_score=90.0, status="failed"))
        s.commit()
        yield s


def _find(report: calibration.Report, keyword: str) -> calibration.Section:
    for section in report.sections:
        if keyword in section.title:
            return section
    raise AssertionError(f"报告里没有「{keyword}」这一节：{[s.title for s in report.sections]}")


def _body(section: calibration.Section) -> str:
    return "\n".join(section.lines)


# ---------------------------------------------------------------------------
# 口径
# ---------------------------------------------------------------------------
def test_only_public_questions_count_as_coverage(session: Session) -> None:
    """点 2 只有私有题 —— 它必须仍然算「缺题」，否则会停止补题。"""
    section = calibration.point_coverage(session)
    assert "已确认知识点 2 个" in _body(section)
    assert "少于 3 道（`TARGET_PER_POINT`）的 1 个" in _body(section)


def test_the_coverage_section_reports_the_unmounted_pool(session: Session) -> None:
    session.add(Question(id=9, kind="design", stem="还没挂的题", difficulty=3, origin="seed"))
    session.commit()
    assert "待定池（`primary_point_id` 为空）的公共题 1 道" in _body(
        calibration.point_coverage(session)
    )


def test_the_difficulty_histogram_matches_the_rows(session: Session) -> None:
    """难度分布只数公共题：那道私有的 design（难度 2）不该进去。"""
    body = _body(calibration.difficulty_mix(session))
    assert "公共题 3 道" in body
    assert "    难度 3: 67%" in body
    assert "    难度 5: 33%" in body
    assert "难度 2" not in body, "私有题被算进难度分布了"


def test_the_round_section_counts_followups(session: Session) -> None:
    body = _body(calibration.round_shape(session))
    assert "题会话 2 个" in body
    assert "追问轮 2 次" in body
    assert "库里的 `max_rounds` 取值 [3]" in body


# ---------------------------------------------------------------------------
# 分位与直方图的边界
# ---------------------------------------------------------------------------
def test_a_quantile_returns_a_value_from_the_sample(session: Session) -> None:
    """报出来的分位必须是**样本里真有的那个值** —— 线性插值会给出库里不存在的分数
    （`[10,20,30,40]` 的 P25 会变成 17.5），而"17.5 分"这种门槛没人能解释。"""
    assert calibration._quantile([55.0, 88.0], 0.25) == 55.0
    assert calibration._quantile([55.0, 88.0], 0.75) == 88.0
    assert calibration._quantile([10.0, 20.0, 30.0, 40.0], 0.25) == 20.0
    assert calibration._quantile([10.0], 0.9) == 10.0


def test_the_score_section_ignores_failed_evaluations(session: Session) -> None:
    """失败的那条 90 分不能进分位 —— 它不是一次成功的判分（`status='failed'`）。"""
    body = _body(calibration.score_quantiles(session))
    assert "成功判分 1 条" in body
    assert "90.0" not in body


def test_bars_merge_everything_above_the_top_bucket(session: Session) -> None:
    counts = {1: 2, 30: 5}
    lines = calibration._bars(Counter(counts))
    assert len(lines) == 2, "档位没合并 —— 长尾会把图拉成一条线"


def test_the_sample_sizes_are_printed(session: Session) -> None:
    """每个数字都要能回答"这是在哪一批数据上算的"。"""
    assert "抽样：" in _body(calibration.stem_similarity(session))
    assert "字面相似度 ≥ 0.9" in _body(calibration.stem_similarity(session))


# ---------------------------------------------------------------------------
# 报告本身
# ---------------------------------------------------------------------------
def test_the_report_names_every_number_it_calibrates(session: Session) -> None:
    report = calibration.collect(session, db="测试库")
    titles = [s.title for s in report.sections]
    assert len(titles) == 6, f"§11 的六个可算的项应当各有一节：{titles}"
    text = report.render()
    assert "测试库" in text
    for title in titles:
        assert f"## {title}" in text


def test_every_section_says_how_to_read_its_number(session: Session) -> None:
    """每条数字都要带上"它该怎么读 / 为什么不能直接当结论"—— 否则报告会被当成建议值。"""
    report = calibration.collect(session, db="测试库")
    for section in report.sections:
        assert section.note, f"{section.title} 缺「怎么读」的说明"
        assert f"⚠️ {section.note}" in report.render()


def test_the_cost_section_goes_last(session: Session) -> None:
    extra = calibration.Section("⑦ 单位成本（--live）→ 额度点系数", ["  x"])
    report = calibration.collect(session, db="测试库", extra=[extra])
    assert report.sections[-1] is extra, "成本节是结论，该排在样本后面"


# ---------------------------------------------------------------------------
# 它是只读的
# ---------------------------------------------------------------------------
def test_live_costs_skips_loudly_without_a_key(tmp_dir: Path) -> None:
    from app.config import Settings

    # 显式传空串：本机的 `.env` 里有真 key，不覆盖的话这条测试会去连真服务
    section = calibration.live_costs(
        Settings(database_path=tmp_dir / "none.db", llm_api_key="")
    )
    assert "跳过" in _body(section)
    assert "DEEPGRILL_LLM_API_KEY" in _body(section)


def test_the_live_path_runs_on_a_copy_and_leaves_the_real_db_alone(
    session: Session, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**这条是整节最重要的断言。**

    `--live` 会 `start_drill` + `explain`，两者都写库；而标定工具在线上必须只读。
    所以它先把库复制一份再跑。判据不看注释、看结果：跑完之后真库里的面试行、
    额度流水一行都不许变。
    """
    from app.config import Settings

    class CountingLLM(FakeLLM):
        def __init__(self, **kw) -> None:
            super().__init__(**kw)
            self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0,
                                "reasoning_tokens": 0, "calls": 0,
                                "missing_usage_calls": 0}

        def chat(self, *args, **kwargs):
            reply = super().chat(*args, **kwargs)
            self.usage_total["prompt_tokens"] += 700
            self.usage_total["completion_tokens"] += 1300
            return reply

    llm = CountingLLM().queue(round_reply(), "先背可见性，再谈内存屏障。")
    db = session.get_bind().url.database
    monkeypatch.setattr("app.deps.get_llm", lambda *a, **kw: llm)

    assert db is not None
    real = Path(str(db))
    before = _snapshot(real)

    section = calibration.live_costs(
        Settings(database_path=real, llm_api_key="test-key")
    )

    assert "跳过" not in _body(section), _body(section)
    assert "一轮追问（1 个额度点）" in _body(section)
    assert "2000 token" in _body(section), _body(section)
    assert _snapshot(real) == before, "标定工具改了真实库 —— 它必须是只读的"


def test_the_live_path_measures_on_a_copy_even_when_the_db_is_busy(
    session: Session, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """副本必须带上**还没 checkpoint 的 WAL 事务** —— 否则测的是半截数据。"""
    from app.config import Settings

    class CountingLLM(FakeLLM):
        def __init__(self, **kw) -> None:
            super().__init__(**kw)
            self.usage_total = {"prompt_tokens": 1, "completion_tokens": 1,
                                "reasoning_tokens": 0, "calls": 0,
                                "missing_usage_calls": 0}

    monkeypatch.setattr("app.deps.get_llm", lambda *a, **kw: CountingLLM().queue(
        round_reply(), "讲解正文"
    ))
    db = Path(str(session.get_bind().url.database))
    # 这一行**还没 commit**：它此时只存在于 WAL 里
    session.add(Question(id=77, kind="knowledge", stem="还在 WAL 里的题", difficulty=3,
                         origin="seed"))
    section = calibration.live_costs(Settings(database_path=db, llm_api_key="k"))
    assert "跳过" not in _body(section), _body(section)


def test_no_temp_copy_is_left_behind(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    class CountingLLM(FakeLLM):
        def __init__(self, **kw) -> None:
            super().__init__(**kw)
            self.usage_total = {"prompt_tokens": 1, "completion_tokens": 1,
                                "reasoning_tokens": 0, "calls": 0,
                                "missing_usage_calls": 0}

    monkeypatch.setattr("app.deps.get_llm", lambda *a, **kw: CountingLLM().queue(
        round_reply(), "讲解正文"
    ))
    db = Path(str(session.get_bind().url.database))
    calibration.live_costs(Settings(database_path=db, llm_api_key="k"))
    assert not (Path(".tmp") / f"calibrate-{os.getpid()}").exists(), (
        "副本没删掉 —— 每跑一次留一份库，磁盘会被悄悄吃满"
    )


def test_a_failure_is_printed_not_raised(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """真调模型会失败（网络、额度、格式）。CLI 工具该**打出来**，而不是抛上去。"""
    from app.config import Settings

    def boom(*args, **kwargs):
        raise RuntimeError("连接被重置")

    monkeypatch.setattr("app.deps.get_llm", boom)
    db = Path(str(session.get_bind().url.database))
    section = calibration.live_costs(Settings(database_path=db, llm_api_key="k"))
    assert "失败：RuntimeError: 连接被重置" in _body(section)
    assert section.note, "失败也要说清「别拿这一节的数去改系数」"


def _snapshot(db: Path) -> tuple[int, int, int]:
    """真库的三个计数（面试行 / 题会话 / 额度流水）—— 用来证明它没被写过。"""
    with create_session_factory(create_db_engine(db))() as s:
        return tuple(  # type: ignore[return-value]
            int(
                s.execute(select(func.count()).select_from(model)).scalar_one()
            )
            for model in (Interview, Session_, QuotaLedger)
        )
