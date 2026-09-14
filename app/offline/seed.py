"""演示/开发用的种子数据。

**它不生成任何"题库资产"** —— 生产上的题目来自 `tools/import_v1.py`（v1 全量导入，
§未决 2）与 LLM 生成。这里只放**几十条手写的最小数据**，用途是让"这条链能不能跑通"
在没有网络、没有 API key 的机器上也能验证。

放在 `app/offline/` 而不是别处，理由是它属于**跨领域的写入动作**：它要同时写
`bank` 的题与 `knowledge` 的知识点，而领域之间不许互相 import（ADR-0005）——
跨领域的动作归编排层（`offline`）。ADR-0010 也把 `offline` 记为"跨领域动作的
固定去处"。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.account import repository as account_repository
from app.bank import repository
from app.db.models import Criterion, Domain, InviteCode, KnowledgePoint, Question, User

logger = logging.getLogger(__name__)

SEED_EMAIL = "demo@local"

#: 演示账号的口令。它是**公开常量**（README 里写着），只用于本地演示库。
SEED_PASSWORD = "deepgrill-demo"

#: 种子里那张可用的邀请码。演示时用它注册一个新账号。
SEED_INVITE = "DEEPGRILL-DEMO"


def repository_invite(session: Session, code: str) -> InviteCode | None:
    """取一张邀请码 —— 走账号域的仓储，不自己 select（表的所有权在 `account`）。"""
    return account_repository.find_invite(session, code)

#: 与 `tools/_compare_schema.py` 无关，纯粹是演示题库的形状：
#: 领域 → 知识点 → 考察点，题挂在知识点上。故意跨三个领域，
#: 好让"按知识点筛选""掌握度矩阵"这些页面有东西可看。
SEED: dict[str, dict[str, list[str]]] = {
    "Java 并发": {
        "volatile": [
            "保证可见性与有序性，不保证原子性",
            "底层靠内存屏障实现",
            "与 synchronized 的适用场景区别",
        ],
        "线程池参数": [
            "核心线程数与最大线程数的区别",
            "队列满之后的拒绝策略",
            "为什么不要用无界队列",
        ],
    },
    "RAG 检索": {
        "向量召回": [
            "召回与重排是两个阶段，各自解决什么问题",
            "向量相似度的度量方式与归一化",
        ],
        "分块策略": [
            "固定长度切块的固有缺陷",
            "按语义或结构切块的做法",
        ],
    },
    "系统设计": {
        "缓存一致性": [
            "缓存与数据库的更新顺序",
            "为什么先更新数据库再删缓存",
            "失效与更新两种策略的取舍",
        ],
        "限流算法": [
            "令牌桶与漏桶的区别",
            "固定窗口的临界问题",
            "分布式限流的实现要点",
        ],
    },
}

QUESTIONS: list[tuple[str, str, int, str]] = [
    ("volatile", "说说 volatile 的作用，它能保证原子性吗？", 2, "knowledge"),
    ("volatile", "volatile 为什么能保证可见性？底层怎么实现的？", 4, "knowledge"),
    ("线程池参数", "线程池的核心线程数和最大线程数有什么区别？", 2, "knowledge"),
    ("线程池参数", "队列满了以后线程池会怎么做？有几种拒绝策略？", 3, "knowledge"),
    ("向量召回", "RAG 里召回和重排分别解决什么问题？只用召回行不行？", 3, "design"),
    ("分块策略", "文档切块怎么切？固定长度切块有什么问题？", 3, "design"),
    ("缓存一致性", "缓存和数据库怎么保证一致性？你选哪种更新顺序，为什么？", 4, "design"),
    ("限流算法", "设计一个限流器：令牌桶和漏桶你选哪个，为什么？", 4, "design"),
]


def seed(session: Session) -> dict[str, int]:
    """写入种子数据（**幂等**：已存在就跳过）。

    幂等不是锦上添花：这个命令会被反复跑（演示、CI、换机器），而"跑两次得到
    两份数据"会让演示看起来像坏了。判据用 `stem` 与 `name` —— 种子数据的唯一性
    就靠它们，而生产导入的题有自己的去重通道（ADR-0002：重复的定义是"指向同一
    知识点"）。
    """
    created = {"domains": 0, "points": 0, "criteria": 0, "questions": 0, "users": 0, "invites": 0}

    if session.execute(select(User).where(User.email == SEED_EMAIL)).scalar_one_or_none() is None:
        from app.security import hash_password

        session.add(
            User(
                email=SEED_EMAIL,
                username="demo",
                # 演示账号的口令是**公开常量**，与用户名一起写在 README 里 ——
                # 它只用于本地演示库。生产库的口令来自环境，占位口令由决策 58
                # 的启动检查拦下（那条还没实现）。
                password_hash=hash_password(SEED_PASSWORD),
                role="user",
            )
        )
        created["users"] += 1

    # 一张可用的邀请码：否则演示时**注册这条路根本走不通**（决策 6：注册 = 邀请码），
    # 而"注册走不通"会被误读成"注册功能坏了"。
    if repository_invite(session, SEED_INVITE) is None:
        session.add(InviteCode(code=SEED_INVITE))
        created["invites"] += 1
        session.flush()

    point_by_name: dict[str, KnowledgePoint] = {}
    for domain_name, points in SEED.items():
        domain = session.execute(
            select(Domain).where(Domain.name == domain_name)
        ).scalar_one_or_none()
        if domain is None:
            domain = Domain(name=domain_name)
            session.add(domain)
            session.flush()
            created["domains"] += 1

        for point_name, criteria in points.items():
            point = session.execute(
                select(KnowledgePoint).where(
                    KnowledgePoint.name == point_name,
                    KnowledgePoint.domain_id == domain.id,
                )
            ).scalar_one_or_none()
            if point is None:
                point = KnowledgePoint(
                    domain_id=domain.id,
                    name=point_name,
                    # 人审过才是 confirmed；种子数据是手写的，所以直接 confirmed
                    status="confirmed",
                    origin="manual",
                )
                session.add(point)
                session.flush()
                created["points"] += 1

            existing = {
                c.seq
                for c in session.execute(
                    select(Criterion).where(Criterion.point_id == point.id)
                ).scalars()
            }
            for seq, text in enumerate(criteria, start=1):
                if seq not in existing:
                    session.add(Criterion(point_id=point.id, seq=seq, text=text, shared=0))
                    created["criteria"] += 1

            point_by_name[point_name] = point

    for point_name, stem, difficulty, kind in QUESTIONS:
        point = point_by_name[point_name]
        # 走 bank 的仓储，不自己 select(Question) —— 题目查询只有那一条通道
        # （AGENTS.md §3.5）。这条规则是本文件被自己的结构测试抓到一次之后
        # 才真正落实的：`app/bank/test_repository.py` 会扫全 `app/`。
        if repository.stem_exists(session, stem):
            continue
        session.add(
            Question(
                kind=kind,
                stem=stem,
                difficulty=difficulty,
                primary_point_id=point.id,
                origin="seed",
                visibility="public",
                answer_tier="long_tail",
            )
        )
        created["questions"] += 1

    session.commit()
    logger.info("种子数据：%s", created)
    return created
