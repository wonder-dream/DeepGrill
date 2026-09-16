"""收藏夹的领域测试（决策 63）。

收藏是"用户自己的指针"，它要同时守住三件事，而每件的失败方式都不一样：

· **幂等** —— 按钮会被人连点。第二下若不幂等，库里会出两行，或抛一个 500
· **可见性** —— 收藏一道看不见的题必须与"这道题不存在"同一种回应，否则
  `POST /bank/{id}/favorite` 就成了探测接口（那道题存不存在，看返回码就知道）
· **指针的寿命** —— 题被标 `hidden` 之后，收藏夹**不再列出它**（`visible_to()`
  的规则），但用户仍然要能把自己那根指针**清掉**（否则收藏里留一行点不进去的）

外加一条跨领域的：**别人的收藏不算我的** —— 收藏夹是每人一份，不是全局的。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.bank import favorites, repository
from app.db import create_db_engine, create_session_factory
from app.db.models import Question, User, UserFavorite
from app.errors import NotFound
from migrations._runner import migrate


@pytest.fixture
def session(tmp_dir: Path) -> Iterator[Session]:
    db = tmp_dir / "fav.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        # id=1 是 `0001` 里那条占位 owner —— 不要重复插入它
        s.add(User(id=2, email="me@local", username="me", password_hash="x", role="user"))
        s.add(User(id=3, email="other@local", username="other", password_hash="x", role="user"))
        s.flush()
        s.add(Question(id=1, kind="knowledge", stem="公共题", difficulty=3,
                       origin="seed", visibility="public"))
        s.add(Question(id=2, kind="knowledge", stem="我的私有题", difficulty=3,
                       origin="generated", owner_user_id=2, visibility="private"))
        s.add(Question(id=3, kind="knowledge", stem="别人的私有题", difficulty=3,
                       origin="generated", owner_user_id=3, visibility="private"))
        s.add(Question(id=4, kind="knowledge", stem="被标隐藏的公共题", difficulty=3,
                       origin="seed", visibility="hidden"))
        s.commit()
        yield s


def _stems(cards) -> set[str]:
    return {c.stem for c in cards}


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------
def test_add_is_idempotent(session: Session) -> None:
    assert favorites.add(session, user_id=2, question_id=1) is True
    assert favorites.add(session, user_id=2, question_id=1) is False, "第二下不该新增"
    assert favorites.count(session, user_id=2) == 1


def test_concurrent_add_does_not_raise(tmp_dir: Path) -> None:
    """**回归测试**：两个连接同时收藏同一道题 —— 一个 True、一个 False，**都不报错**。

    实测形状（"先查再写"那条路）：并发下第二条会撞 `UNIQUE(user_id, question_id)` →
    `IntegrityError` → 500，而"点两下收藏"是最容易被用户撞到的并发（按钮双击、
    两个标签页）。现在幂等由**一条语句**保证（`ON CONFLICT DO NOTHING`），
    所以这里断言的是"两次调用都正常返回，且库里只有一行"。
    """
    import threading

    db = tmp_dir / "fav_concurrent.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        s.add(User(id=2, email="me@local", username="me", password_hash="x", role="user"))
        s.add(Question(id=1, kind="knowledge", stem="公共题", difficulty=3,
                       origin="seed", visibility="public"))
        s.commit()

    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def grab() -> None:
        with create_session_factory(engine)() as s:
            barrier.wait(timeout=10)
            first = favorites.add(s, user_id=2, question_id=1)
            s.commit()
            with lock:
                results.append(first)

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results) == [False, True], f"并发收藏的结果应当是「一次新增一次重复」：{results}"
    with create_session_factory(engine)() as s:
        assert favorites.count(s, user_id=2) == 1, "库里只能有一行"
    engine.dispose()


def test_remove_is_idempotent(session: Session) -> None:
    favorites.add(session, user_id=2, question_id=1)
    assert favorites.remove(session, user_id=2, question_id=1) is True
    assert favorites.remove(session, user_id=2, question_id=1) is False, "本来就没有，不该报错"
    assert favorites.count(session, user_id=2) == 0


def test_add_then_remove_then_add_again(session: Session) -> None:
    """收藏 → 取消 → 再收藏必须能拿到一个新的 True（不是"记住了曾经收藏过"）。"""
    favorites.add(session, user_id=2, question_id=1)
    favorites.remove(session, user_id=2, question_id=1)
    assert favorites.add(session, user_id=2, question_id=1) is True


# ---------------------------------------------------------------------------
# 可见性
# ---------------------------------------------------------------------------
def test_cannot_favorite_someone_elses_private_question(session: Session) -> None:
    with pytest.raises(NotFound):
        favorites.add(session, user_id=2, question_id=3)
    assert favorites.count(session, user_id=2) == 0


def test_cannot_favorite_a_question_that_does_not_exist(session: Session) -> None:
    """不存在与不可见**同一种回应** —— 否则这个接口能用来探测题目 id。"""
    with pytest.raises(NotFound):
        favorites.add(session, user_id=2, question_id=999)
    with pytest.raises(NotFound):
        favorites.add(session, user_id=2, question_id=3)


def test_cannot_favorite_a_hidden_public_question(session: Session) -> None:
    """`hidden` 不进任何浏览面（AGENTS.md §3.5）—— 收藏也不行。"""
    with pytest.raises(NotFound):
        favorites.add(session, user_id=2, question_id=4)


def test_own_private_question_can_be_favorited(session: Session) -> None:
    """自己的私有题（哪怕被标 hidden）自己看得到，也就能收藏。"""
    assert favorites.add(session, user_id=2, question_id=2) is True


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------
def test_browse_returns_only_my_favorites(session: Session) -> None:
    """同一道题可以被两个人各自收藏 —— 但**每个人只看到自己那一份**。"""
    favorites.add(session, user_id=2, question_id=1)
    favorites.add(session, user_id=2, question_id=2)   # 我自己的私有题
    favorites.add(session, user_id=3, question_id=1)

    cards, total = favorites.browse(session, user_id=2)
    assert total == 2 and _stems(cards) == {"公共题", "我的私有题"}
    cards3, total3 = favorites.browse(session, user_id=3)
    assert total3 == 1 and _stems(cards3) == {"公共题"}


def test_hidden_question_disappears_from_the_list(session: Session) -> None:
    """**题被隐藏后，收藏夹不再列出它** —— 收藏是指针，不是"这一行的备份"。

    指针还在（行没删），但它指向的东西不可见了。
    """
    favorites.add(session, user_id=2, question_id=1)
    session.get(Question, 1).visibility = "hidden"
    session.flush()

    cards, total = favorites.browse(session, user_id=2)
    assert total == 0 and cards == []
    assert favorites.count(session, user_id=2) == 1, "指针本身还在（用户可以自己去清）"


def test_pointer_can_still_be_removed_after_the_question_is_hidden(session: Session) -> None:
    """题被隐藏之后**仍然能取消收藏** —— 否则收藏夹里会留一行点不进去的东西。

    （所以 `repository.remove_favorite` 故意不带可见性条件。）
    """
    favorites.add(session, user_id=2, question_id=1)
    session.get(Question, 1).visibility = "hidden"
    session.flush()

    assert favorites.remove(session, user_id=2, question_id=1) is True
    assert favorites.count(session, user_id=2) == 0


def test_dangling_pointer_is_skipped_not_crashing(session: Session) -> None:
    """题真的被删掉之后（外键已经清干净），收藏夹不该因此炸掉。"""
    favorites.add(session, user_id=2, question_id=1)
    session.execute(UserFavorite.__table__.delete().where(UserFavorite.question_id == 1))
    session.flush()
    assert favorites.browse(session, user_id=2) == ([], 0)


def test_browse_pages(session: Session) -> None:
    """分页不是装饰（AGENTS.md §3.6）：收藏可能很多条。"""
    for qid in range(1, 5):
        session.add(Question(id=100 + qid, kind="knowledge", stem=f"题 {qid}", difficulty=3,
                             origin="seed", visibility="public"))
    session.flush()
    for qid in range(101, 105):
        assert favorites.add(session, user_id=2, question_id=qid) is True

    # 每页 20 条，所以 4 条都在第一页；这里验证的是"总数为真、当页不撒谎"
    cards, total = favorites.browse(session, user_id=2, page=1)
    assert total == 4 and len(cards) == 4


# ---------------------------------------------------------------------------
# 列表页的批量标记
# ---------------------------------------------------------------------------
def test_favorited_ids_answers_for_a_whole_page_at_once(session: Session) -> None:
    favorites.add(session, user_id=2, question_id=1)
    assert favorites.favorited_ids(session, user_id=2, question_ids=[1, 2, 3]) == {1}
    assert favorites.favorited_ids(session, user_id=2, question_ids=[]) == set()


def test_is_favorited(session: Session) -> None:
    assert favorites.is_favorited(session, user_id=2, question_id=1) is False
    favorites.add(session, user_id=2, question_id=1)
    assert favorites.is_favorited(session, user_id=2, question_id=1) is True


def test_favorites_never_touch_the_question_row(session: Session) -> None:
    """**收藏不改变题目本身**（决策 63 的原话）。改动一处就说明实现越界了。"""
    before = session.get(Question, 1).to_dict()
    favorites.add(session, user_id=2, question_id=1)
    favorites.remove(session, user_id=2, question_id=1)
    session.flush()
    assert session.get(Question, 1).to_dict() == before


def test_favorites_do_not_appear_as_owned_questions(session: Session) -> None:
    """收藏**不是**"拥有"：它不能顺手把题变成私有题（那会改变可见性）。"""
    favorites.add(session, user_id=2, question_id=1)
    assert repository.owned_ids(session, 2) == [2]
