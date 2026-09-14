"""报告的测试：拼装正确性、快照边界、总结的降级。

三条最值钱的性质：

· **报告是拼装的、可复现的**（ADR-0003）：同一场面试算两次得到同样的数字，
  且数字与 `evaluations` / `attempts` 里的原始记录一致
· **快照的是依据，不是结论**：题干改掉之后，历史报告仍显示**当时**的题干，
  而分数跟着 `evaluations`（结论是事实，永不重算）
· **总结失败要降级、要标明来源**，不能静默给一句空话
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import (
    Criterion,
    Domain,
    Interview,
    KnowledgePoint,
    KnowledgePointEdge,
    Question,
    User,
)
from app.interview import rules, service as interview_service
from app.report import service
from app.llm import LLMCallError
from migrations._runner import migrate
from tests.fakes import FakeLLM, FakeReply

ME = 2


@pytest.fixture
def session(tmp_dir: Path) -> Session:
    db = tmp_dir / "report.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        s.add(User(id=ME, email="me@local", username="me", password_hash="h", role="user"))
        s.add(Domain(id=1, name="Java 并发"))
        # JMM 是 volatile 的前置（用来测"该补的前置"）
        s.add_all(
            [
                KnowledgePoint(id=1, domain_id=1, name="JMM 内存模型", status="confirmed"),
                KnowledgePoint(id=2, domain_id=1, name="volatile", status="confirmed"),
            ]
        )
        s.flush()
        s.add(KnowledgePointEdge(from_point_id=1, to_point_id=2, kind="prerequisite"))
        for seq, text in enumerate(
            ["可见性与有序性", "底层内存屏障", "与 synchronized 的适用场景区别"], start=1
        ):
            s.add(Criterion(id=seq, point_id=2, seq=seq, text=text, shared=0))
        s.flush()
        s.add(
            Question(
                id=1,
                kind="knowledge",
                stem="说说 volatile 的作用",
                difficulty=3,
                primary_point_id=2,
                origin="seed",
                visibility="public",
            )
        )
        s.commit()
        yield s


def _round_reply(hits, followup="继续", finish=True):
    return FakeReply(
        data={
            "hits": [{"criterion_id": c, "status": s} for c, s in hits],
            "followup": followup,
            "should_finish": finish,
        }
    )


def _eval_reply(accuracy=80, completeness=70, clarity=90, depth=60, review="评语"):
    return FakeReply(
        data={
            "scores": {
                "accuracy": accuracy,
                "completeness": completeness,
                "clarity": clarity,
                "depth": depth,
            },
            "review": review,
        }
    )


def _summary_reply(text="这场面试说明并发基础还需补 JMM。"):
    return FakeReply(data={"summary": text})


def _play(session: Session, *, hits, llm):
    ts = interview_service.start_drill(session, user_id=ME, question_id=1)
    interview_service.submit_answer(session, ts=ts, answer_text="我的回答", llm=llm)
    interview = session.get(Interview, ts.interview_id)
    return interview


def test_report_is_assembled_from_data(session: Session) -> None:
    """①③ 来自 `evaluations` 与 `attempts`，数字必须与原始记录一致（ADR-0003）。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=80, completeness=80, clarity=80, depth=80),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.total_score == 80
    assert len(report.items) == 1
    item = report.items[0]
    assert item.total_score == 80
    assert item.review == "评语"
    assert item.hits == {"1": "命中", "2": "未命中", "3": "未命中"}
    assert item.missed == ["2", "3"]
    # ③ 的依据被快照了
    from app.db.models import ReportItem

    snap = session.query(ReportItem).one()
    assert snap.snap_stem == "说说 volatile 的作用"
    assert snap.snap_point_name == "volatile"
    assert snap.snap_criteria["criteria"] == [
        "可见性与有序性",
        "底层内存屏障",
        "与 synchronized 的适用场景区别",
    ]


def test_report_is_reproducible(session: Session) -> None:
    """同一场面试算两次得到同样的数字 —— 报告不含随机性，因而可被断言。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=70, completeness=70, clarity=70, depth=70),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    first = service.finish_interview(session, interview=interview, llm=llm)

    # 读回已落库的那份（不重算）
    again = service.get_report(session, interview)
    assert again.total_score == first.total_score
    assert [i.hits for i in again.items] == [i.hits for i in first.items]
    assert again.summary == first.summary


def test_snapshot_keeps_the_original_stem_after_edit(session: Session) -> None:
    """**快照的是依据**：题被改掉之后，历史报告仍显示**当时**的题干。

    而分数是**结论**（历史事实），读 `evaluations`，不快照也不重算。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "命中"), (3, "命中")]),
        _eval_reply(accuracy=90, completeness=90, clarity=90, depth=90),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    service.finish_interview(session, interview=interview, llm=llm)

    question = session.get(Question, 1)
    question.stem = "改过的题干：volatile 与 JMM 的关系"
    session.commit()

    reread = service.get_report(session, interview)
    assert reread.items[0].stem == "说说 volatile 的作用", "历史报告必须显示当时的题干"
    assert reread.items[0].total_score == 90


def test_matrix_change_only_lists_changed_cells(session: Session) -> None:
    """② 只放真的变了的格子；没考过的知识点不该出现在变化里。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未涉及")]),
        _eval_reply(),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert [c.point_name for c in report.changes] == ["volatile"]
    change = report.changes[0]
    assert (change.before_covered, change.before_hit) == (0, 0)
    assert (change.after_covered, change.after_hit) == (2, 1)
    assert change.covered_delta == 2 and change.hit_delta == 1
    assert "JMM 内存模型" not in [c.point_name for c in report.changes]


def test_prerequisite_gap_comes_from_the_graph(session: Session) -> None:
    """④ 该补的前置：**沿前置边走一跳**，不调 LLM（ADR-0003）。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "未命中"), (2, "未命中"), (3, "未涉及")]),
        _eval_reply(),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)
    assert report.prerequisite_gaps == ["JMM 内存模型"]


def test_no_gaps_when_everything_hit(session: Session) -> None:
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "命中"), (3, "命中")]),
        _eval_reply(),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)
    assert report.prerequisite_gaps == []
    assert report.top_problems == []


def test_top_problems_are_ranked_and_grouped_by_point(session: Session) -> None:
    """⑤ 漏得最多的考察点，按知识点归类（"知识点：考察点"）。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "未命中"), (2, "未命中"), (3, "命中")]),
        _eval_reply(),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)
    assert report.top_problems, "有未命中就该有问题清单"
    assert all(p.startswith("volatile：") for p in report.top_problems)


def test_summary_failure_falls_back_and_says_so(session: Session) -> None:
    """⑥ 失败要降级并**标明来源** —— 不能静默给一句空话，也不能让报告打不开。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=60, completeness=60, clarity=60, depth=60),
        FakeReply(error=LLMCallError("总结服务挂了")),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "fallback"
    assert report.summary, "降级也要有一句话"
    assert "60" in report.summary, "降级文案必须由已落库的数据拼出来"
    assert interview.report_summary == report.summary


def test_summary_must_not_invent_names(session: Session) -> None:
    """ADR-0003 的硬约束：总结里不许出现数据里没有的东西 —— **而且是强制的**。

    > 总结提到的每个知识点名，都必须能在 ② 或 ④ 的结果里找到。
    > **这条可程序化检查**（跑一次字符串匹配即可）。

    所以这里断言的不是"我们能发现它编了"，而是**系统真的把它拦下来了**：
    编造的总结必须被丢弃、降级为确定性文案，并标明来源。这是 v2 里少数可以写
    确定性测试的 LLM 相关环节。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(),
        _summary_reply("你在 Kubernetes 调度上很强，缓存穿透也讲得清楚。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "fallback", "编造的总结必须被拦下"
    assert "Kubernetes" not in report.summary


def test_grounding_accepts_terms_from_the_criteria_text(session: Session) -> None:
    """**回归测试**：总结里出现**考察点正文里的**技术名词必须被接受。

    实测踩过（真调模型时才暴露）：考察点是「与 synchronized 的适用场景区别」，
    而总结里写 `synchronized` 是**完全正确**的引用 —— 第一版检查只认知识点名，
    于是把它判成"编造"，**把一段好总结丢掉了**（那次模型写的总结质量明显高于
    降级文案）。修法是让"合法名字集合"包含考察点正文里的拉丁词。

    这类误报在本项目里已经第三次出现（前两次在 `check_docs.py` 规则 18 与题库的
    结构断言），所以它值得一条专门的测试。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(),
        _summary_reply("volatile 的可见性答到了，但 synchronized 的适用场景没讲清。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "llm", "引用考察点里的名词不该被判成编造"
    assert "synchronized" in report.summary


def test_grounding_accepts_the_four_dimension_names(session: Session) -> None:
    """**回归测试**：总结里引用四维分的维度名必须被接受。

    实测踩过（真调模型时才暴露）：总结写「accuracy 65、completeness 60、clarity 55、
    depth 40」—— 那是**照抄报告自己的数据**，却被判成"编造的技术名词"，于是又一次
    把好总结丢掉。

    这已经是这条检查第四次误报（前三次：`synchronized` 来自考察点、`## 它做什么`
    这类标题行、以及最初的汉字片段）。四次都指向同一件事：**白名单必须覆盖报告
    自己会引用的全部词汇**，否则它拦下的就是正常输出。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(),
        _summary_reply("这题 accuracy 80、completeness 70、clarity 90、depth 60，深度是短板。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "llm", "照抄报告里的维度名不该被判成编造"
    assert "accuracy" in report.summary


def test_grounded_summary_is_accepted(session: Session) -> None:
    """反过来：只用数据里有的名字写的总结必须被采纳 —— 否则上一版检查会把
    正常文案也拦掉（那就成了"一条满屏误报的规则"）。"""
    grounded = "这场面试里 volatile 的可见性你答到了，底层内存屏障还没答到。"
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(),
        _summary_reply(grounded),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "llm"
    assert report.summary == grounded


def test_grounding_check_accepts_partial_names(session: Session) -> None:
    """名字的片段也算提到（"JMM 内存模型" 说成"JMM"或"内存模型"）。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "未命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(),
        _summary_reply("建议先补 JMM，再看 volatile。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "llm"
    assert report.summary == "建议先补 JMM，再看 volatile。"


def test_grounding_check_does_not_flag_ordinary_chinese(session: Session) -> None:
    """**回归测试**：正常的中文文案不该被拦下。

    第一版把"2-4 个汉字连续片段"都当候选名去比对，于是「这场面试说明并发基础
    还需补 JMM」里的每个片段都被判成"编造的知识点"，整段文案全被丢弃 ——
    正是"一条满屏误报的规则比没有规则更糟"在本项目里的第三次重演。
    现在只查拉丁词；这条测试守着那个边界。
    """
    ordinary = "这场面试说明并发基础还需要补，建议先看内存模型那一块。"
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(),
        _summary_reply(ordinary),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "llm", "普通中文文案不该被当成编造"
    assert report.summary == ordinary


def test_finish_persists_body_and_summary(session: Session) -> None:
    """**生成时机**：主体与总结都在 finish 时算好并落库（ADR-0003）。

    断言点选在 `interviews` 那一行上 —— 因为"打开报告"走的是读库那条路，
    不是重算。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "命中"), (3, "未命中")]),
        _eval_reply(accuracy=75, completeness=75, clarity=75, depth=75),
        _summary_reply("存下来的一段总结"),
    )
    interview = _play(session, hits=None, llm=llm)
    service.finish_interview(session, interview=interview, llm=llm)

    assert interview.status == "finished"
    assert interview.ended_at is not None
    assert interview.report_summary == "存下来的一段总结"
    body = interview.report_body
    assert body["total_score"] == 75
    assert body["changes"][0]["point_name"] == "volatile"


def test_finish_is_idempotent(session: Session) -> None:
    """收尾被调两次（用户重复点结束）不该产生两倍的报告行。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(),
        _summary_reply(),
        _eval_reply(),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    service.finish_interview(session, interview=interview, llm=llm)
    service.finish_interview(session, interview=interview, llm=llm)

    from app.db.models import ReportItem

    assert session.query(ReportItem).count() == 1
    assert len(service.get_report(session, interview).items) == 1


def test_failed_evaluation_still_produces_a_report(session: Session) -> None:
    """某题判分失败时报告仍要出得来（那一题 status='failed'），不能整场 500。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        FakeReply(error=LLMCallError("判分挂了")),
        _summary_reply(),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.items[0].status == "failed"
    assert report.total_score is None, "没有一道题判成功时总分应为 None（而不是 0）"
    assert report.summary
