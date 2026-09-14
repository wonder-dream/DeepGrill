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
    """ADR-0003 的硬约束**已被修订成"标注不拦"** —— 这条测试记录新行为。

    > 修订理由（真调模型之后）：把它实现成准入检查时它**四次误报**，每次都在丢
    > 正确输出（汉字片段、标题行、考察点正文里的 `synchronized`、四维分维度名）。
    > 根因是"总结里的每个词是否在数据里"这件事**词法匹配做不到** —— 总结的本质
    > 就是改写与归纳。详见 `service.summary_names_are_grounded` 的 docstring。

    所以现在：**模型写的总结一律保留**（它读起来明显优于降级文案），
    疑似数据外的名词只记进 `report.signals.ungrounded_terms` 供页面标注。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(),
        _summary_reply("你在 Kubernetes 调度上很强，缓存穿透也讲得清楚。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "llm", "现在不再因 grounding 而降级"
    assert "Kubernetes" in report.summary, "总结要保留（它仍是给用户看的文案）"
    assert "Kubernetes" in report.signals.ungrounded_terms, "但必须被标出来"
    # 落库 + 读回来都要带着这个信号（否则页面上标不出来）
    stored = service.get_report(session, interview)
    assert stored.signals.ungrounded_terms == report.signals.ungrounded_terms


def test_fallback_summary_is_assembled_from_data(session: Session) -> None:
    """模型失败时的兜底文案：**由数据组装、且比原来那句更有信息量**。

    本项目对比过三条路线（组装式 / 受限生成 / 自由生成）之后，选的兜底形态是
    组装式 —— 它从不编造、零 token、完全可复现，而"模型失败"恰恰是最不该再冒险
    的时刻。这条测试钉住它的**内容形状**（而不是只断言"非空串"）。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=90, completeness=80, clarity=70, depth=40),
        FakeReply(error=LLMCallError("总结服务挂了")),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.summary_source == "fallback"
    # 组装式会点出"最低的那一维"（这里 depth 40），而不是只报一个总分
    assert "depth" in report.summary
    assert "40" in report.summary
    assert "volatile" in report.summary


def test_summary_check_catches_a_contradicted_weak_dimension(session: Session) -> None:
    """**ADR-0003 真正担心的那种矛盾**：结论性断言与数据不一致。

    ADR 举的例子是"评语说没答到内存屏障，总结却写并发基础扎实"。在报告层面，
    同一类错的可判定形态是**"哪一维最弱"说反了** —— 数据里 depth 最低，
    总结却说 accuracy 是短板。这条能被程序抓住（不需要词表，只需比对断言与数据）。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=90, completeness=85, clarity=80, depth=40),
        _summary_reply("整体不错，accuracy 是最低的一项，需要重点提升。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.signals.contradictions, "说了反的最弱维度必须被发现"
    assert "depth" in report.signals.contradictions[0]
    assert report.summary_source == "llm", "仍然是标注不拦"


def test_summary_check_accepts_a_correct_weak_dimension(session: Session) -> None:
    """**误报守卫**：说对了就不该报警 —— 这条检查在本项目里已经误报过四次。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=90, completeness=85, clarity=80, depth=40),
        _summary_reply("整体不错，depth 是最低的一项，需要重点提升。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.signals.contradictions == []
    assert report.signals.unknown_numbers == []


def test_summary_check_accepts_chinese_dimension_names(session: Session) -> None:
    """总结用中文说维度名（"深度是短板"）也要认 —— 报告里存的是英文，两套都得对。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=90, completeness=85, clarity=80, depth=40),
        _summary_reply("概念没问题，深度是短板。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)
    assert report.signals.contradictions == []

    llm2 = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=90, completeness=85, clarity=80, depth=40),
        _summary_reply("概念没问题，准确性是短板。"),
    )
    interview2 = _play(session, hits=None, llm=llm2)
    report2 = service.finish_interview(session, interview=interview2, llm=llm2)
    assert report2.signals.contradictions, "说「准确性」最弱而数据是 depth —— 该被抓到"


def test_summary_check_catches_numbers_that_are_not_in_the_data(session: Session) -> None:
    """数字核查：**数字是闭集词汇**，所以这条能做得精确（不像名词）。"""
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "未命中"), (3, "未命中")]),
        _eval_reply(accuracy=90, completeness=85, clarity=80, depth=40),
        _summary_reply("这场大概 95 分，比上次的 88 分进步了。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert set(report.signals.unknown_numbers) == {"95", "88"}


def test_summary_check_accepts_numbers_from_the_data(session: Session) -> None:
    """引用数据里的数字（含"命中/被考"的两半）与它们的差，都不算编造。

    90/85/80/40 按 `.3/.3/.2/.2` 合成 = **77**（不是 74 —— 我第一版这里就写错了，
    而检查当场把它当成"数据里没有的数字"报了出来）。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "命中"), (3, "未命中")]),
        _eval_reply(accuracy=90, completeness=85, clarity=80, depth=40),
        _summary_reply("总分 77；四维是 90、85、80、40；掌握度从 0/0 到 2/3。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.signals.unknown_numbers == [], (
        f"这些数字都来自数据，不该报警：{report.signals.unknown_numbers}"
    )


def test_summary_check_catches_a_wrong_total(session: Session) -> None:
    """**附带能力**：总结报了一个与四维分对不上的总分，会被当成未知数字抓出来。

    这条不是刻意设计的，是"数字必须来自数据（含其函数）"的自然结果 ——
    但它抓的正是"结论与数据不一致"这一类，所以值得钉住。
    """
    llm = FakeLLM().queue(
        _round_reply([(1, "命中"), (2, "命中"), (3, "未命中")]),
        _eval_reply(accuracy=90, completeness=85, clarity=80, depth=40),
        _summary_reply("总分 74，还有提升空间。"),
    )
    interview = _play(session, hits=None, llm=llm)
    report = service.finish_interview(session, interview=interview, llm=llm)

    assert report.signals.unknown_numbers == ["74"], (
        "四维分合成是 77，说 74 就是与数据不一致"
    )


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
