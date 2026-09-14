"""种子数据与 CLI 的测试。

断言点选在**可观察的结果**上（库里的行、第二次跑不翻倍），而不是"函数返回了
什么字典" —— 前者的失效方式才是使用者真正会遇到的。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, Question
from app.offline.seed import QUESTIONS, SEED, seed
from migrations._runner import migrate


@pytest.fixture
def session(tmp_dir: Path):
    db = tmp_dir / "seed.db"
    migrate(db)
    engine = create_db_engine(db)
    with create_session_factory(engine)() as s:
        yield s


def test_seed_creates_a_usable_demo_dataset(session) -> None:
    created = seed(session)
    assert created["questions"] == len(QUESTIONS)
    assert created["points"] == sum(len(v) for v in SEED.values())

    # 每道题都必须有主知识点，且该知识点至少有一条考察点 ——
    # 否则"逐题追问"没有靶子（追问由考察点命中驱动，决策 28）
    questions = session.execute(select(Question)).scalars().all()
    assert questions
    for q in questions:
        assert q.primary_point_id is not None, f"题 {q.id} 没有主知识点"
        n = len(
            session.execute(
                select(Criterion).where(Criterion.point_id == q.primary_point_id)
            ).scalars().all()
        )
        assert n >= 2, f"知识点 {q.primary_point_id} 的考察点太少（{n}），追问无从下手"


def test_seed_is_idempotent(session) -> None:
    """跑两遍不该翻倍 —— 这个命令会被反复跑（演示、CI、换机器）。"""
    first = seed(session)
    before = len(session.execute(select(Question)).scalars().all())
    second = seed(session)
    after = len(session.execute(select(Question)).scalars().all())

    assert first["questions"] > 0
    assert second["questions"] == 0, "第二次不该再建题"
    assert before == after
    assert second["points"] == 0 and second["criteria"] == 0 and second["invites"] == 0


def test_seed_provides_a_usable_invite(session) -> None:
    """种子必须给一张**可用**的邀请码，否则演示时注册这条路走不通（决策 6）。

    "注册走不通"会被误读成"注册功能坏了" —— 这条测试就是为了不让那次误读发生。
    """
    from app.account import repository as account_repo
    from app.offline.seed import SEED_INVITE

    seed(session)
    invite = account_repo.find_invite(session, SEED_INVITE)
    assert invite is not None
    assert account_repo.invite_is_usable(invite)


def test_seed_demo_account_can_actually_log_in(session) -> None:
    """演示账号必须**真的能登录**。

    第一版给它塞了一个占位哈希（`scrypt$demo$demo`），于是"用演示账号进去看看"
    这条路根本走不通 —— 而那正是接手的人做的第一件事。
    """
    from app.account import service as account_service
    from app.offline.seed import SEED_EMAIL, SEED_PASSWORD
    from app.security import verify_password

    seed(session)
    user = account_service.repository.find_user_by_email(session, SEED_EMAIL)
    assert user is not None
    assert verify_password(SEED_PASSWORD, user.password_hash), "演示账号的口令必须能验通过"


def test_cli_seed_refuses_when_not_migrated(tmp_dir: Path) -> None:
    """没迁移的库上跑 seed 要**明确失败并给出那条命令**，而不是建出半个库。"""
    from app.cli import cmd_seed

    settings = Settings(database_path=tmp_dir / "fresh.db")
    with pytest.raises(SystemExit) as e:
        cmd_seed(settings)
    assert e.value.code == 2


def test_cli_seed_and_status(tmp_dir: Path, capsys) -> None:
    from app.cli import cmd_seed, cmd_status

    settings = Settings(database_path=tmp_dir / "cli.db")
    migrate(settings.resolved_database_path())

    assert cmd_seed(settings) == 0
    out = capsys.readouterr().out
    assert "种子数据已写入" in out

    assert cmd_status(settings) == 0
    out = capsys.readouterr().out
    assert "题目: 0" not in out, "status 应当看到种子数据"
    assert "知识点:" in out
