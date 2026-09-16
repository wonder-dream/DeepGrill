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


def test_cli_propose_exits_non_zero_on_zero_candidates(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """**回归测试**（bug 27）：模型回了合法 JSON 但没有 `points` 键时，`propose` 要
    非零退出并说清楚 —— 原来它打印"候选 0 条"、rc=0、一句 note 都没有，
    脚本与人都看不出"这一步什么都没产出"。
    """
    from app.cli import cmd_propose
    from app.db import create_db_engine, create_session_factory
    from app.db.models import Question
    from app.offline import knowledge_pipeline as kp
    from app.web import admin_page

    db = tmp_dir / "propose.db"
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        s.add(
            Question(id=1, kind="knowledge", stem="说说 volatile 的作用", difficulty=3,
                     origin="seed", visibility="public")
        )
        s.commit()

    monkeypatch.setattr(admin_page, "PROPOSAL_PATH", tmp_dir / "proposal.json")
    # 模型"回了个合法 JSON 但没有 points 键"：不是调用失败，所以 llm_failed 是 False
    monkeypatch.setattr(
        kp, "propose_points",
        lambda questions, llm: kp.ProposalResult(note="模型返回了 JSON 但没有可用候选"),
    )

    rc = cmd_propose(
        Settings(database_path=db, llm_api_key="fake-key"), "Java 并发", batched=False
    )
    assert rc == 1, "零候选必须非零退出（否则 && 与 CI 都发现不了）"
    assert "没有产出任何候选" in capsys.readouterr().err


def test_cli_merge_points_refuses_an_out_of_range_id(tmp_dir: Path, capsys) -> None:
    """`merge-points 2**63` 要给人话，不是一条 Python 栈（实测 probe33）。

    越界 id 会在驱动里抛 `OverflowError: Python int too large to convert to SQLite
    INTEGER` —— 谁也不知道是自己敲错了数字。合法范围是 SQLite 的 64 位整数。
    """
    from app.cli import cmd_merge_points

    settings = Settings(database_path=tmp_dir / "nope.db")
    assert cmd_merge_points(settings, 2**63, 1) == 2
    assert "超出范围" in capsys.readouterr().err
    assert cmd_merge_points(settings, 1, 1) == 2, "源与目标相同也要当场拒绝"


def test_cli_set_password_replaces_the_placeholder(tmp_dir: Path, capsys) -> None:
    """**上线前置条件**（决策 58）：把 owner 的占位口令换成真口令。

    这条命令以前**不存在** —— `deploy/README.md` 只能写"直接改库即可"，等于让人手算
    一个 scrypt 哈希再 UPDATE。断言点选在"新口令验得通、旧口令验不通、令牌被撤销"：
    三者缺一，线上就还是错的（或者旧会话还活着）。
    """
    from app.account import repository as account_repository
    from app.cli import cmd_set_password
    from app.offline.seed import SEED_EMAIL, SEED_PASSWORD
    from app.security import verify_password

    db = tmp_dir / "pw.db"
    settings = Settings(database_path=db)
    migrate(db)
    # 演示账号由 `seed` 建 —— 它的口令是**仓库里公开写死**的那个（SEED_PASSWORD），
    # 所以上线时它和 owner 的占位口令是同一类问题
    with create_session_factory(create_db_engine(db))() as s:
        seed(s)
        s.commit()
    assert cmd_set_password(settings, "owner@local", "a-real-owner-pw") == 0
    assert "已改" in capsys.readouterr().out

    with create_session_factory(create_db_engine(db))() as s:
        owner = account_repository.find_user_by_email(s, "owner@local")
        assert owner is not None
        assert verify_password("a-real-owner-pw", owner.password_hash)
        assert not owner.password_hash.startswith("PLACEHOLDER__"), "占位口令必须被替换掉"

    # 演示账号也在库里且口令是公开写死的 —— 同一条命令能把它换掉
    assert cmd_set_password(settings, SEED_EMAIL, "another-real-pw") == 0
    with create_session_factory(create_db_engine(db))() as s:
        demo = account_repository.find_user_by_email(s, SEED_EMAIL)
        assert demo is not None
        assert verify_password("another-real-pw", demo.password_hash)
        assert not verify_password(SEED_PASSWORD, demo.password_hash), "公开的演示口令要失效"


def test_cli_set_password_refuses_bad_input(tmp_dir: Path, capsys) -> None:
    """口令太短、账号不存在：都要**明确失败**（rc≠0），而不是改了半个。"""
    from app.account import repository as account_repository
    from app.cli import cmd_set_password

    db = tmp_dir / "pw2.db"
    settings = Settings(database_path=db)
    migrate(db)

    assert cmd_set_password(settings, "owner@local", "short") == 2
    assert "至少 8 位" in capsys.readouterr().err

    assert cmd_set_password(settings, "nobody@local", "long-enough-pw") == 2
    assert "没有这个账号" in capsys.readouterr().err

    with create_session_factory(create_db_engine(db))() as s:
        owner = account_repository.find_user_by_email(s, "owner@local")
        assert owner is not None
        assert owner.password_hash.startswith("PLACEHOLDER__"), "失败的尝试不该改动任何东西"


def test_cli_set_password_revokes_tokens(tmp_dir: Path) -> None:
    """改口令必须**同时踢下线**（复用后台重置那条路，规则只有一份）。"""
    from app.account import repository as account_repository
    from app.cli import cmd_set_password
    from app.db.models import UserToken

    db = tmp_dir / "pw3.db"
    settings = Settings(database_path=db)
    migrate(db)
    with create_session_factory(create_db_engine(db))() as s:
        owner = account_repository.find_user_by_email(s, "owner@local")
        assert owner is not None
        account_repository.issue_token(s, owner.id, "hash-of-live-token")
        s.commit()

    assert cmd_set_password(settings, "owner@local", "a-real-owner-pw") == 0
    with create_session_factory(create_db_engine(db))() as s:
        assert s.execute(select(UserToken)).first() is None, "旧令牌必须全部撤销"


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
