"""题库仓储的测试 —— **本文件是"私有题不许泄露"这条硬规则的守门人**。

为什么值得单独一个文件：AGENTS.md §3.5 说得很直接 —— `questions` 同时装公共题库
与所有人的私有题集，**漏一次 `owner_user_id` 过滤就是数据泄露**。而这类漏的失败
方式是静默的（页面照常渲染，只是多出一行）。

所以这里把四种可见性情形逐条钉住：公共 / 自己的私有 / 别人的私有 / hidden。
并且有一条**结构性**断言：`visible_to()` 之外不许有别的入口（下面
`test_no_other_module_selects_questions_directly`）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.bank import repository, service
from app.db import create_db_engine, create_session_factory
from app.db.models import Question, User
from app.errors import NotFound
from migrations._runner import migrate

#: 迁移会种一个占位 owner（id=1），所以测试用户从 2 起 —— 不占用它，
#: 也就不用去改 0001 里那条 INSERT。
ME = 2
OTHER = 3


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "bank.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        # 私有题的外键指向 users —— 外键是**开着**的（app/db 的 PRAGMA），
        # 所以测试必须真的建出这两个用户，不能只写 owner_user_id=1/2。
        s.add_all(
            [
                User(id=ME, email="me@local", username="me", password_hash="h", role="user"),
                User(id=OTHER, email="other@local", username="o", password_hash="h", role="user"),
            ]
        )
        s.commit()
        yield s


def _add_question(
    session: Session,
    *,
    stem: str,
    owner: int | None = None,
    visibility: str = "public",
    kind: str = "knowledge",
) -> Question:
    q = Question(
        kind=kind,
        stem=stem,
        difficulty=3,
        owner_user_id=owner,
        visibility=visibility,
        origin="seed",
    )
    session.add(q)
    session.commit()
    return q


def test_anonymous_sees_only_public_questions(session: Session) -> None:
    _add_question(session, stem="公共题")
    _add_question(session, stem="我的私有题", owner=ME, visibility="private")

    rows, total = repository.list_questions(session, viewer_id=None)
    assert [q.stem for q in rows] == ["公共题"]
    assert total == 1


def test_owner_sees_own_private_question(session: Session) -> None:
    _add_question(session, stem="公共题")
    _add_question(session, stem="我的私有题", owner=ME, visibility="private")

    rows, _ = repository.list_questions(session, viewer_id=ME)
    assert {q.stem for q in rows} == {"公共题", "我的私有题"}


def test_cannot_see_someone_elses_private_question(session: Session) -> None:
    """**这条是数据泄露的守门人**：别人的私有题连"存在"都不该知道。

    注意它顺手钉住另一件事：`find_question` 对"别人的私有题"返回 None，
    与"不存在"**不可区分** —— 区分它们会把存在性泄露出去。
    """
    theirs = _add_question(session, stem="别人的私有题", owner=OTHER, visibility="private")

    rows, _ = repository.list_questions(session, viewer_id=ME)
    assert rows == []
    assert repository.find_question(session, theirs.id, viewer_id=ME) is None


def test_hidden_and_pending_are_not_in_the_public_list(session: Session) -> None:
    """`hidden`（质量信号差）与 `pending`（晋升待门禁）都不算公共。

    判断依据是 `repository.PUBLIC_VISIBILITY` —— 只有 `public` 进公共列表。
    """
    _add_question(session, stem="正常题")
    _add_question(session, stem="被标 hidden 的题", visibility="hidden")
    _add_question(session, stem="待门禁的题", visibility="pending")

    rows, total = repository.list_questions(session, viewer_id=None)
    assert [q.stem for q in rows] == ["正常题"]
    assert total == 1


def test_owner_still_sees_own_hidden_question(session: Session) -> None:
    """自己的题被标 hidden 后，作者仍要看得到 —— 否则是"题消失且没人知道为什么"。"""
    mine = _add_question(session, stem="我的题被标了 hidden", owner=ME, visibility="hidden")
    assert repository.find_question(session, mine.id, viewer_id=ME) is not None


def test_detail_reads_criteria_not_question_level_good_criteria(session: Session) -> None:
    """**决策 49 的硬规则**：运行时读 `criteria` 表，不读 `questions.good_criteria`。

    这里故意把两者写成不同的内容：如果实现读错了列，断言会拿到 `good_criteria` 的
    文本而失败。违反这条**不会报错**，只会让判分悄悄用上一份没人再维护的旧标准。
    """
    from app.db.models import Criterion, Domain, KnowledgePoint

    session.add(Domain(id=1, name="Java 并发"))
    point = KnowledgePoint(domain_id=1, name="volatile", status="confirmed")
    session.add(point)
    session.flush()  # 取到 id 再建考察点（外键真的开着，不能猜 id）
    session.add(Criterion(point_id=point.id, seq=1, text="保证可见性与有序性，不保证原子性"))
    session.add(Criterion(point_id=point.id, seq=2, text="底层靠内存屏障"))
    session.commit()

    q = _add_question(session, stem="说说 volatile")
    q.primary_point_id = point.id
    q.good_criteria = '["这是离线素材，不是判分依据"]'
    session.commit()

    detail = service.detail(session, q.id, viewer_id=None)
    assert detail.criteria == ["保证可见性与有序性，不保证原子性", "底层靠内存屏障"]
    assert detail.point_name == "volatile"


def test_invisible_question_raises_not_found(session: Session) -> None:
    theirs = _add_question(session, stem="别人的私有题", owner=OTHER, visibility="private")
    with pytest.raises(NotFound):
        service.detail(session, theirs.id, viewer_id=ME)


def test_no_other_module_selects_questions_directly() -> None:
    """**结构性断言**：题目查询只许从 `bank/repository.py` 起手。

    AGENTS.md §3.5 要求"任何题目查询都必须经过统一的查询入口，不允许业务代码直接
    `select(Question)`"。这条规则写进文档容易、忘掉更容易 —— 所以让它在测试里可执行。
    它当场就抓到过一次：`offline/seed.py` 自己 `select(Question)` 查题干是否已存在。

    ⚠️ 判据必须看**语法**，不是文本：第一版用 `"select(Question)" in text`，于是
    合法的 import 行被算成违规；第二版换正则，又被 `offline/seed.py` 里那句
    **注释**（"不自己 select(Question)"）触发。注释里出现这个词是完全正常的 ——
    所以这里走 AST，只看真实的 `Call` 节点。这条规则自己先报了两次假警，
    而「一条满屏误报的规则比没有规则更糟」。

    范围限定 `app/`：`tests/` 与 `tools/` 是测试与一次性脚本，不受这条约束。
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "repository.py" and path.parent.name == "bank":
            continue
        if "test_" in path.name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in {"select", "select_from"} or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "Question":
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, (
        f"这些位置直接以 Question 起手查询 —— 必须走 bank/repository 的入口：{offenders}"
    )
