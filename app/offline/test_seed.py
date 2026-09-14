"""种子数据与冷启动顺序的测试（决策 72 / §未决 10）。

种子只做两件事：让"这条链能不能跑通"在没有 key 的机器上可验证，以及**说明先铺哪个
领域**。第二件事原来不存在 —— 而"先铺哪个"是个必须有依据的决定，不是默认值。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import create_db_engine, create_session_factory
from app.db.models import Domain, KnowledgePoint, Question
from app.offline import seed as seed_module
from migrations._runner import migrate


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "seed.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        yield s


def test_seed_is_idempotent(session: Session) -> None:
    """重跑种子不该翻倍 —— 它是"让链路可跑"的，不是"每次多一批数据"。"""
    first = seed_module.seed(session)
    assert first["domains"] == 3 and first["points"] == 6
    assert first["questions"] == len(seed_module.QUESTIONS)

    second = seed_module.seed(session)
    assert second["domains"] == 0 and second["points"] == 0
    assert second["questions"] == 0
    assert len(session.execute(select(Question)).scalars().all()) == len(
        seed_module.QUESTIONS
    )


def test_every_seeded_question_is_mounted(session: Session) -> None:
    """种子题**必须**挂在知识点上 —— 否则演示库一打开就是 8 道待定题。"""
    seed_module.seed(session)
    rows = session.execute(select(Question)).scalars().all()
    assert rows
    assert all(q.primary_point_id is not None for q in rows)


def test_cold_start_order_follows_the_measured_corpus(session: Session) -> None:
    """顺序是**实测出来的**（待定池里 RAG 46% / 系统设计 39% / Java 8%），不是拍脑袋。

    所以这条测试盯的是：报告里的排名与 `COLD_START` 一致，且**没被列进去的领域
    排在最后** —— 库里有新领域时不该打乱已有的顺序，也不该被漏掉。
    """
    seed_module.seed(session)
    # 把顺序本身也钉在这里：这条测试证明**报告跟着常量走**，证明不了常量是对的
    # （实测数据在 3007 道待定题上，单元测试造不出来）。所以改这个顺序时，
    # 你必须先看一眼这个断言的注释、再重跑一次 `python -m app.cli calibrate`。
    assert seed_module.COLD_START == ("RAG 检索", "系统设计", "Java 并发"), (
        "改动冷启动顺序要重新实测（决策 72）：待定池里 RAG 46% / 系统设计 39% / Java 8%"
    )

    session.add(Domain(name="安卓逆向"))  # 一个不在冷启动顺序里的领域
    session.commit()

    lines = seed_module.cold_start_report(session)
    ranked = [line.split(" ——")[0] for line in lines if "——" in line]
    assert len(ranked) == 4
    assert ranked[0].endswith(seed_module.COLD_START[0])
    assert ranked[1].endswith(seed_module.COLD_START[1])
    assert ranked[2].endswith(seed_module.COLD_START[2])
    assert "不在冷启动顺序里" in ranked[3]
    assert "安卓逆向" in ranked[3]


def test_the_report_says_what_is_still_unmounted(session: Session) -> None:
    """待定池的数字必须在场 —— 它才是"还有多少活要干"的那一行。"""
    seed_module.seed(session)
    session.add(
        Question(kind="knowledge", stem="还没挂的题", difficulty=3, origin="seed")
    )
    session.commit()
    assert any("待定池" in line and "1 道" in line for line in seed_module.cold_start_report(session))


def test_the_report_counts_only_confirmed_points_as_ready(session: Session) -> None:
    seed_module.seed(session)
    point = session.execute(
        select(KnowledgePoint).where(KnowledgePoint.name == "volatile")
    ).scalar_one()
    assert point.status == "confirmed"
    body = " ".join(seed_module.cold_start_report(session))
    assert "知识点 2（已确认 2）" in body
