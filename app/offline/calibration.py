"""阈值标定：把 `docs/v1行为规格.md` §11 那张表里的数字与库里的**真实分布**对上。

§未决 6 挂着的就是这件事：「那些 v1 拍的数字要在 v2 里重新测」，而"重新测"此前
没有任何留痕 —— 谁测的、测的是哪一版库、样本多少，全在脑子里。这正是
`docs/INDEX.md` 开头那个"13 个不同的测试数"的复发路径。

## 它为什么是一个工具，而不是"再看一眼数据"

· **每个数字的判据不同**：有的看**分布**（难度 1-5 是不是两端稀疏）、有的看**分位**
  （"高分 / 薄弱"门槛）、有的只能给**下界**（去重阈值：真判据是嵌入余弦，而库里
  现在只有假嵌入 —— 字面相似度只是"同一道题"的必要条件）。报告把这三类区别写在
  各自的小节里，而不是混成一个看起来像结论的"建议值"。
· **样本量必须跟着数字走**：`CLUSTER_THRESHOLD` 是在 60 道题上拍的，而库里现在有
  三千道 —— 拿 3007 道的库去印证一个 60 道的阈值，得先说清是哪一批。

## ⚠️ 它只读，不改任何常量

结论由人写进台账（决策 70）。一个"自动改常量"的标定器是最糟的形态：
它会在没人看的时候把阈值改掉，而改动的依据是一批可能已经过期的数据。

```
python -m app.cli calibrate            # 只读库，出分布
python -m app.cli calibrate --live     # 额外真调两次模型，测单位成本（在库的副本上）
```
"""

from __future__ import annotations

import difflib
import os
import shutil
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.bank.promotion import normalize_stem
from app.db.models import Attempt, Evaluation, Explanation, KnowledgePoint, Session_
from app.offline import generation

#: 同一个知识点里最多取几道题来两两比题干。2C2G（AGENTS §3.6）：**不做全库两两比较** ——
#: 三千道题的两两组合是 450 万对，而这里要看的只是"同点内有没有近乎重复的题"。
POINT_SAMPLE_LIMIT = 40

#: 字面相似度到多少算"值得人看一眼"。它**不是** `CLUSTER_THRESHOLD` 的替代 ——
#: 那个阈值比的是嵌入余弦，两者不同量纲（见模块文档）。
LITERAL_NEAR = 0.90

#: 直方图的合并档：超过它的都并进最后一档（否则长尾会把图拉成一条线）。
TOP_BUCKET = 12

#: `--live` 用的一段合成回答（约 200 字）。输入侧是真 prompt、真题干、真判据，
#: 只有回答是人造的 —— 它决定的是**输出**长度，而输出长度这一项本来就靠 `max_tokens`
#: 兜着。写死是为了让两次测量可复现（否则测的是"这次答了多长"）。
SAMPLE_ANSWER = (
    "volatile 保证的是可见性与有序性，不保证原子性。写操作会立刻刷进主内存，"
    "读操作会强制从主内存重新读取，所以一个线程改了值另一个线程能看见。"
    "底层靠内存屏障实现：写前插 StoreStore、写后插 StoreLoad，读前插 LoadLoad、"
    "读后插 LoadStore，禁止指令重排穿过屏障。但它不保证复合操作的原子性，"
    "比如 i++ 是读改写三步，还是要用锁或者 AtomicInteger。"
    "另外它和 synchronized 的区别是：前者只保证可见性，后者还保证互斥。"
)


# ---------------------------------------------------------------------------
# 报告的形状
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Section:
    """一节报告：结论 + 数据 + 一句"这条数字该怎么读"。"""

    title: str
    lines: list[str]
    note: str = ""


@dataclass
class Report:
    db: str
    sections: list[Section] = field(default_factory=list)

    def render(self) -> str:
        out = [
            f"阈值标定报告 —— {self.db}",
            "只读工具：它不改任何常量，结论由人写进 docs/v2范围基线.md 的台账。",
            "",
        ]
        for section in self.sections:
            out.append(f"## {section.title}")
            out.extend(section.lines)
            if section.note:
                out.append(f"   ⚠️ {section.note}")
            out.append("")
        return "\n".join(out)


def _bars(counts: Counter[int], *, unit: str = "题", width: int = 24) -> list[str]:
    """一张手绘直方图。没有依赖（2C2G 上不装 matplotlib 只为打印几行数字）。"""
    if not counts:
        return ["  （没有数据）"]
    top = max(counts.values())
    out = []
    for key in sorted(counts):
        n = counts[key]
        bar = "█" * max(1, round(n / top * width)) if n else ""
        out.append(f"  {key:>5}  {n:>6} {unit}  {bar}")
    return out


def _quantile(values: Sequence[float], q: float) -> float:
    """最近秩分位（样本小的时候不插值 —— 报一个库里不存在的分数会误导人）。"""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "—"


# ---------------------------------------------------------------------------
# ① 每点题量 —— `generation.TARGET_PER_POINT` 的样本
# ---------------------------------------------------------------------------
def point_coverage(session: Session) -> Section:
    points = list(
        session.execute(
            select(KnowledgePoint).where(KnowledgePoint.status == "confirmed")
        ).scalars()
    )
    counts = bank_repository.point_question_counts(session, [p.id for p in points])
    target = generation.TARGET_PER_POINT
    hist: Counter[int] = Counter(min(counts.get(p.id, 0), TOP_BUCKET) for p in points)
    thin = [p for p in points if counts.get(p.id, 0) < target]
    unmounted = bank_repository.unmounted_public_count(session)

    lines = [
        f"  已确认知识点 {len(points)} 个；公共题少于 {target} 道（`TARGET_PER_POINT`）的 {len(thin)} 个",
        f"  待定池（`primary_point_id` 为空）的公共题 {unmounted} 道",
        *_bars(hist),
    ]
    return Section(
        "① 每点题量 → generation.TARGET_PER_POINT",
        lines,
        note=(
            "它数的只算**公共**题（私有题再多也不代表这个点已经有题了）。"
            f"最后一档是合并档（{TOP_BUCKET} 道及以上）。"
            "`TARGET_PER_POINT` 定的是「低于几道就补」，所以要看的是"
            f"「{target} 道以下占了几成」，而不是平均数。"
        ),
    )


# ---------------------------------------------------------------------------
# ② 题干字面相似度 —— 去重阈值的**下界**
# ---------------------------------------------------------------------------
def stem_similarity(session: Session, *, limit: int = POINT_SAMPLE_LIMIT) -> Section:
    points = list(
        session.execute(
            select(KnowledgePoint).where(KnowledgePoint.status == "confirmed")
        ).scalars()
    )
    ratios: list[float] = []
    near: list[tuple[float, str, str]] = []
    sampled_points = 0
    for point in points:
        rows, _ = bank_repository.list_questions(
            session, None, point_id=point.id, limit=limit
        )
        if len(rows) < 2:
            continue
        sampled_points += 1
        normalized = [normalize_stem(q.stem) for q in rows]
        for i in range(len(normalized)):
            for j in range(i + 1, len(normalized)):
                ratio = difflib.SequenceMatcher(
                    None, normalized[i], normalized[j]
                ).ratio()
                ratios.append(ratio)
                if ratio >= LITERAL_NEAR:
                    near.append((ratio, rows[i].stem[:40], rows[j].stem[:40]))

    hist: Counter[int] = Counter(min(int(r * 10), 10) for r in ratios)
    lines = [
        f"  抽样：{sampled_points} 个知识点、{len(ratios)} 对题干（每点最多 {limit} 道）",
        *_bars(hist, unit="对"),
        f"  字面相似度 ≥ {LITERAL_NEAR}：{len(near)} 对（{_pct(len(near), len(ratios))}）",
    ]
    near.sort(reverse=True)
    for ratio, a, b in near[:5]:
        lines.append(f"    {ratio:.2f}  「{a}」 / 「{b}」")
    return Section(
        "② 题干字面相似度 → 去重阈值的下界",
        lines,
        note=(
            "**这不是 `CLUSTER_THRESHOLD`（0.86）的对照值。** 那个阈值比的是嵌入余弦，"
            "而库里现在装的是假嵌入（ADR-0008：DeepSeek 不提供 embeddings）—— "
            "假向量算出来的余弦只反映字符重合，拿它标定语义阈值等于自己骗自己。"
            "字面相似度能回答的是另一件事：**同一道题的两种写法**长什么样，"
            "所以它是任何语义阈值都必须覆盖的下界。横轴 = 相似度 × 10。"
        ),
    )


# ---------------------------------------------------------------------------
# ③ 难度分布 —— 判分 prompt 的难度校准（§11 点名"中间堆积、两端稀疏"）
# ---------------------------------------------------------------------------
def difficulty_mix(session: Session) -> Section:
    hist: Counter[int] = Counter(bank_repository.difficulty_counts(session))
    total = sum(hist.values())
    lines = [f"  公共题 {total} 道", *_bars(hist, unit="道")]
    for level in sorted(hist):
        lines.append(f"    难度 {level}: {_pct(hist[level], total)}")
    return Section(
        "③ 难度分布 → 判分 prompt 的难度校准",
        lines,
        note=(
            "v1 的实测结论是**中间堆积、两端稀疏**，而判分 prompt 依赖难度做校准；"
            "这批数字来自 v1 导入的题（`origin='seed'`），所以它同时是「导入有没有丢难度」"
            "的对账。"
        ),
    )


# ---------------------------------------------------------------------------
# ④ 轮数 —— v1 的各档上限 4/4/8/12/15 与 v2 的 DEFAULT_MAX_ROUNDS
# ---------------------------------------------------------------------------
def round_shape(session: Session) -> Section:
    from app.interview.service import DEFAULT_MAX_ROUNDS

    per_session = [
        int(n)
        for (_, n) in session.execute(
            select(Attempt.session_id, func.count()).group_by(Attempt.session_id)
        )
    ]
    caps = [
        int(n) for (n,) in session.execute(select(Session_.max_rounds).distinct())
    ]
    followups = int(
        session.execute(
            select(func.count()).select_from(Attempt).where(Attempt.is_followup == 1)
        ).scalar_one()
    )
    hist: Counter[int] = Counter(min(n, TOP_BUCKET) for n in per_session)
    lines = [
        f"  题会话 {len(per_session)} 个；库里的 `max_rounds` 取值 {sorted(caps)}",
        f"  代码里的 `DEFAULT_MAX_ROUNDS` = {DEFAULT_MAX_ROUNDS}；追问轮 {followups} 次"
        f"（占 {_pct(followups, len(per_session))}）",
        *_bars(hist, unit="会话"),
    ]
    return Section(
        "④ 轮数分布 → 各档追问轮数上限",
        lines,
        note=(
            "v1 的 4/4/8/12/15 是**按难度分档**的，而 ADR-0001 把上限改成了编排参数"
            "（`sessions.max_rounds`，不由难度推导）。所以这里要看的是：真实会话在第几轮"
            "停，以及上限有没有被顶到 —— **顶到的比例高，说明上限（而不是模型）在决定收尾**。"
        ),
    )


# ---------------------------------------------------------------------------
# ⑤ 分数分位 —— "高分 / 薄弱"门槛（v1 的 70）
# ---------------------------------------------------------------------------
def score_quantiles(session: Session) -> Section:
    scores = [
        float(s)
        for (s,) in session.execute(
            select(Evaluation.total_score).where(
                Evaluation.status == "ok", Evaluation.total_score.is_not(None)
            )
        )
    ]
    if not scores:
        return Section(
            "⑤ 分数分位 → 高分 / 薄弱门槛（v1 的 70）",
            ["  （库里还没有成功的判分记录）"],
            note="没有真实作答数据时，70 这个门槛只能留用 v1 的值 —— 这一点要写在结论里。",
        )
    lines = [f"  成功判分 {len(scores)} 条"]
    for q in (0.1, 0.25, 0.5, 0.75, 0.9):
        lines.append(f"    P{int(q * 100):<3} = {_quantile(scores, q):.1f}")
    lines.append(f"    均值 = {sum(scores) / len(scores):.1f}")
    lines.append(f"  ≥70 的比例：{_pct(sum(1 for s in scores if s >= 70), len(scores))}")
    return Section(
        "⑤ 分数分位 → 高分 / 薄弱门槛（v1 的 70）",
        lines,
        note=(
            "v1 用同一个 70 同时表达「高分」与「薄弱复习」，而这两件事在 v2 里已经分开了："
            "薄弱点是按**未命中数**排序的（决策 24），不再看总分。所以这个门槛现在只服务"
            "报告里的措辞，**它的标定优先级低于掌握度**。"
        ),
    )


# ---------------------------------------------------------------------------
# ⑥ 新题 / 复习题比例（v1 的 70/30）
# ---------------------------------------------------------------------------
def fresh_vs_review(session: Session) -> Section:
    per_question = [
        int(n)
        for (_, n) in session.execute(
            select(Session_.question_id, func.count()).group_by(Session_.question_id)
        )
    ]
    fresh = sum(1 for n in per_question if n == 1)
    review = sum(1 for n in per_question if n >= 2)
    total = fresh + review
    lines = [
        f"  被考过的题 {total} 道：第一次考 {fresh} 道（{_pct(fresh, total)}）、"
        f"复习 {review} 道（{_pct(review, total)}）",
        f"  重复次数最多：{max(per_question) if per_question else 0} 次",
    ]
    return Section(
        "⑥ 新题 / 复习题比例（v1 的 70/30）",
        lines,
        note=(
            "题库练习不建面试行（`mode='browse'` 没有面试），所以这里只统计**进过面试**"
            "的题 —— 真实使用里「刷题」占大头时，这个比例会系统性偏低。样本量小的时候"
            "不要据此改比例。"
        ),
    )


# ---------------------------------------------------------------------------
# ⑦ 成本侧（--live）—— 额度点系数的推导（§未决 9 / 决策 71）
# ---------------------------------------------------------------------------
def _delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: after.get(k, 0) - before.get(k, 0) for k in after}


def live_costs(settings, *, answer: str = SAMPLE_ANSWER) -> Section:
    """真调两次模型，测**一个额度点值多少 token**（§未决 9）。

    ⚠️ 它跑在**库的临时副本**上：`start_drill` / `explain` 都会写库，而标定工具在
    线上必须是只读的。副本用完即删 —— 目录用 `Path.mkdir()` 建，**不用
    `tempfile.mkdtemp()`**：那在本机会给出"仅属主可访问"的权限，紧接着往里写文件
    就是 `PermissionError`，而错误信息看起来像沙箱拒绝（AGENTS §六.4）。
    """
    from app.db import create_db_engine, create_session_factory
    from app.db.models import QuotaLedger, User
    from app.deps import get_llm
    from app.interview import service as interview_service
    from app.knowledge import explanation

    if not settings.llm_api_key:
        return Section(
            "⑦ 单位成本（--live）→ 额度点系数",
            ["  跳过：没有配 DEEPGRILL_LLM_API_KEY"],
            note="这一节的数字**只能靠真调用**得到（token 用量是服务商给的，不能推算）。",
        )

    source = settings.resolved_database_path()
    work = Path(".tmp") / f"calibrate-{os.getpid()}"
    engine = None
    try:
        work.mkdir(parents=True, exist_ok=True)
        copy = work / "copy.db"
        # 用 SQLite 自己的备份通道，不用文件复制：开发库开着 WAL，直接拷主文件会丢
        # 掉还没 checkpoint 的事务。`closing` 而不是 `with` —— `with conn` 只管事务，
        # 不管关闭。
        with closing(sqlite3.connect(str(source))) as src, closing(
            sqlite3.connect(str(copy))
        ) as dst:
            src.backup(dst)

        engine = create_db_engine(copy)
        with create_session_factory(engine)() as session:
            llm = get_llm(settings)
            user = session.execute(select(User).order_by(User.id).limit(1)).scalar_one_or_none()
            if user is None:
                return Section(
                    "⑦ 单位成本（--live）→ 额度点系数",
                    ["  跳过：副本里一个用户都没有（先跑 python -m app.cli seed）"],
                )
            # 副本里清掉两样东西：额度流水（否则可能一开场就 QuotaExhausted）与
            # 讲解缓存（否则那次调用命中缓存、测出来是 0 token）。
            session.execute(delete(QuotaLedger).where(QuotaLedger.user_id == user.id))
            session.execute(delete(Explanation))
            question_rows, _ = bank_repository.list_questions(session, None, limit=1)
            if not question_rows:
                return Section(
                    "⑦ 单位成本（--live）→ 额度点系数",
                    ["  跳过：副本里没有公共题"],
                )
            question = question_rows[0]

            before = dict(llm.usage_total)
            ts = interview_service.start_drill(
                session, user_id=user.id, question_id=question.id
            )
            interview_service.submit_answer(session, ts=ts, answer_text=answer, llm=llm)
            session.commit()
            drill = _delta(before, llm.usage_total)

            before = dict(llm.usage_total)
            explanation.explain(session, question=question, llm=llm)
            session.commit()
            explain = _delta(before, llm.usage_total)

        round_tokens = drill["prompt_tokens"] + drill["completion_tokens"]
        explain_tokens = explain["prompt_tokens"] + explain["completion_tokens"]
        lines = [
            f"  一轮追问（1 个额度点）：prompt {drill['prompt_tokens']} + "
            f"completion {drill['completion_tokens']} = **{round_tokens} token**"
            f"（其中推理 {drill['reasoning_tokens']}）",
            f"  一次讲解（不扣额度点）：prompt {explain['prompt_tokens']} + "
            f"completion {explain['completion_tokens']} = **{explain_tokens} token**",
            "  一场模拟面试 = 题数 × 每题的轮数：",
        ]
        # 系数从代码里读，不在这里写死 —— 否则报告与实际扣费会各自漂
        from app.account.service import COST, DAILY_UNITS
        from app.interview.service import DEFAULT_MAX_ROUNDS, INTERVIEW_QUESTION_COUNT

        price = COST["interview"]
        lines.append(f"  （现行系数：面试 {price} / 追问 {COST['drill']}；每日 {DAILY_UNITS} 点）")
        for count in sorted({2, 3, INTERVIEW_QUESTION_COUNT}):
            est = count * (DEFAULT_MAX_ROUNDS + 1) * round_tokens
            lines.append(
                f"    {count} 道 × {DEFAULT_MAX_ROUNDS} 轮 ≈ {est} token"
                f" → 每个额度点 {est // price} token"
            )
        ratio = INTERVIEW_QUESTION_COUNT * (DEFAULT_MAX_ROUNDS + 1)
        lines.append(
            f"  参考比值：一场面试的 token ÷ 一轮追问的 token ≈ {ratio}"
            f"（而额度点的比值是 {price} : {COST['drill']}）"
        )
        daily = est + max(0, DAILY_UNITS - price) * round_tokens
        lines.append(
            f"  每日上限 {DAILY_UNITS} 点 ≈ 一场面试 + {max(0, DAILY_UNITS - price)} 轮追问"
            f" ≈ {int(daily)} token/天/人"
        )
        return Section(
            "⑦ 单位成本（--live）→ 额度点系数",
            lines,
            note=(
                "回答的文本是合成的（见 `SAMPLE_ANSWER`），所以输出长度是一次**样本**"
                "而不是平均值 —— 这也是不自动改系数的理由之一。"
            ),
        )
    except Exception as e:  # noqa: BLE001 —— CLI 工具：把失败**打出来**就是"不静默"
        return Section(
            "⑦ 单位成本（--live）→ 额度点系数",
            [f"  失败：{type(e).__name__}: {e}"],
            note="成本数字没测到，别用这一节的任何数去改系数。",
        )
    finally:
        # ⚠️ 必须先 `dispose()` 再删目录：Windows 不让删还被连接池握着的文件，
        # 而 `ignore_errors=True` 会把这件事**吞掉** —— 于是每跑一次留一份库，
        # 磁盘被悄悄吃满，且没有任何提示（实测就是这么发现的）。
        if engine is not None:
            engine.dispose()
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def collect(session: Session, *, db: str, extra: Iterable[Section] = ()) -> Report:
    """把只读的六节拼成报告；`extra`（`--live` 的成本节）排在最后 —— 它是结论，
    前半是它依赖的样本。"""
    report = Report(db=db)
    report.sections.extend(
        [
            point_coverage(session),
            stem_similarity(session),
            difficulty_mix(session),
            round_shape(session),
            score_quantiles(session),
            fresh_vs_review(session),
        ]
    )
    report.sections.extend(extra)
    return report
