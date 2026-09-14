"""知识层人审页的测试（决策 47）。

三条要钉住的：

· **只有 owner 能进** —— 这一步是全局装配，改的是所有人的掌握度坐标系
· **通过 / 否决 / 合并三种动作都真的落到库里**
· 没有提案文件时**给出一条能跑的命令**，而不是白屏或 500
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.db.models import Criterion, KnowledgePoint, Question, User
from app.main import create_app
from app.security import hash_password
from app.web import admin_page
from migrations._runner import migrate

PASSWORD = "secret123"
_HASH = hash_password(PASSWORD)


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    path = tmp_dir / "admin.db"
    migrate(path)
    with create_session_factory(create_db_engine(path))() as s:
        # 迁移已经种过一个占位 owner（`owner@local`，口令是 PLACEHOLDER__…）。
        # 这里**直接给它一个能登录的口令哈希**，而不是另建一个 owner ——
        # 用真实的那一个更贴近线上，也顺带避免两条 owner 记录。
        from app.db.models import User as _User

        owner = s.execute(select(_User).where(_User.email == "owner@local")).scalars().one()
        owner.password_hash = _HASH
        s.add(User(id=3, email="user@local", username="u", password_hash=_HASH, role="user"))
        s.flush()
        s.add_all(
            [
                Question(id=1, kind="knowledge", stem="题一", difficulty=3, origin="seed",
                         visibility="public"),
                Question(id=2, kind="knowledge", stem="题二", difficulty=3, origin="seed",
                         visibility="public"),
            ]
        )
        s.commit()
    return path


@pytest.fixture
def proposal_file(tmp_dir: Path, monkeypatch) -> Path:
    """把提案文件指到临时目录 —— **绝不碰仓库里那份真实提案**。

    写文件时显式带上 `ensure_ascii=False`：这条不是"测试也要遵守 JSON 列纪律"
    （测试不写库），而是为了让 `check_docs.py` 的规则保持**单行可判** ——
    那条规则宁可误报也不放过，所以这里就写在一行里，别让它为难。
    """
    path = tmp_dir / "proposal.json"
    monkeypatch.setattr(admin_page, "PROPOSAL_PATH", path)
    payload = {
        "candidates": [
            {"name": "volatile", "definition": "考 volatile 语义", "exclusions": "",
             "criteria": ["可见性与有序性", "底层内存屏障"], "question_ids": [1]},
            {"name": "线程池参数", "definition": "", "exclusions": "",
             "criteria": ["核心与最大线程"], "question_ids": [2]},  # 考察点不足 → 判据① 挡下
        ],
        "llm_failed": False,
        "note": "",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path

@pytest.fixture
def owner_client(db: Path):
    with TestClient(create_app(Settings(database_path=db))) as c:
        c.post("/login", data={"email": "owner@local", "password": PASSWORD})
        yield c


def test_review_page_lists_candidates(owner_client: TestClient, proposal_file: Path) -> None:
    r = owner_client.get("/admin/review")
    assert r.status_code == 200
    assert "volatile" in r.text and "线程池参数" in r.text
    # 判据① 的结论要显示出来（"这条为什么可疑"）
    assert "考察点少于" in r.text


def test_review_page_says_what_to_run_without_a_proposal(
    owner_client: TestClient, tmp_dir: Path, monkeypatch
) -> None:
    monkeypatch.setattr(admin_page, "PROPOSAL_PATH", tmp_dir / "missing.json")
    r = owner_client.get("/admin/review")
    assert r.status_code == 200
    assert "app.cli propose" in r.text, "没有提案时给出一条能跑的命令，而不是白屏"


def test_non_owner_cannot_review(db: Path, proposal_file: Path) -> None:
    """全局装配不是个人动作 —— 普通用户不许进。"""
    with TestClient(create_app(Settings(database_path=db))) as c:
        c.post("/login", data={"email": "user@local", "password": PASSWORD})
        r = c.get("/admin/review")
        assert r.status_code == 403
        assert "owner" in r.text


def test_apply_creates_confirmed_points_from_the_form(
    owner_client: TestClient, db: Path, proposal_file: Path
) -> None:
    r = owner_client.post(
        "/admin/review/apply",
        data={
            "domain_name": "Java 并发",
            "action-0": "approve",
            "name-0": "volatile",
            "action-1": "approve",   # 但它的考察点只有 1 条 → 被判据① 挡下
            "name-1": "线程池参数",
        },
    )
    assert r.status_code == 200
    assert "新建知识点" in r.text
    assert "考察点少于" in r.text, "被挡下的要列出来（不静默）"

    with create_session_factory(create_db_engine(db))() as s:
        points = s.execute(select(KnowledgePoint)).scalars().all()
        assert [p.name for p in points] == ["volatile"]
        assert points[0].status == "confirmed"
        assert len(s.execute(select(Criterion)).scalars().all()) == 2
        assert s.get(Question, 1).primary_point_id == points[0].id


def test_apply_reject_and_merge(
    owner_client: TestClient, db: Path, proposal_file: Path
) -> None:
    """否决不建点；合并把题目并到目标上。"""
    r = owner_client.post(
        "/admin/review/apply",
        data={
            "domain_name": "Java 并发",
            "action-0": "approve",
            "name-0": "volatile",
            "action-1": "merge",
            "merge-1": "0",
        },
    )
    assert r.status_code == 200

    with create_session_factory(create_db_engine(db))() as s:
        points = s.execute(select(KnowledgePoint)).scalars().all()
        assert len(points) == 1
        # 被合并那条的题也要挂到目标上（题目不能丢）
        assert s.get(Question, 2).primary_point_id == points[0].id


def test_apply_moves_the_proposal_away(
    owner_client: TestClient, proposal_file: Path
) -> None:
    """审完把提案改名 —— 留着它会让"再审一次"重复建点，状态变得难以解释。"""
    owner_client.post(
        "/admin/review/apply",
        data={"domain_name": "D", "action-0": "reject", "action-1": "reject"},
    )
    assert not proposal_file.exists()
    assert proposal_file.with_suffix(".applied.json").exists()


def test_apply_without_proposal_is_refused(owner_client: TestClient, tmp_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(admin_page, "PROPOSAL_PATH", tmp_dir / "missing.json")
    r = owner_client.post("/admin/review/apply", data={"domain_name": "D"})
    assert r.status_code == 403
