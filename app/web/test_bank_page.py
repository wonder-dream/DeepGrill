"""题库页面的测试：**页面真的出得来**，且不可见的题不泄露存在性。

路由层的测试只断言 HTTP 行为（状态码、页面里有没有那段文字）—— 业务规则由
`app/bank/test_repository.py` 测。这样路由变薄这件事才有测试兜着。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.models import Criterion, Domain, KnowledgePoint, Question
from app.main import create_app
from migrations._runner import migrate


@pytest.fixture
def seeded(tmp_dir: Path) -> Path:
    db = tmp_dir / "web.db"
    migrate(db)
    from app.db import create_db_engine, create_session_factory

    with create_session_factory(create_db_engine(db))() as s:
        s.add(Domain(id=1, name="Java 并发"))
        point = KnowledgePoint(domain_id=1, name="volatile", status="confirmed")
        s.add(point)
        s.flush()
        s.add(Criterion(point_id=point.id, seq=1, text="保证可见性，不保证原子性"))
        s.add(
            Question(
                kind="knowledge",
                stem="说说 volatile 的作用",
                difficulty=2,
                primary_point_id=point.id,
                origin="seed",
                visibility="public",
            )
        )
        s.add(
            Question(
                kind="knowledge",
                stem="这道题被隐藏了",
                difficulty=2,
                primary_point_id=point.id,
                origin="seed",
                visibility="hidden",
            )
        )
        s.commit()
    return db


@pytest.fixture
def client(seeded: Path):
    with TestClient(create_app(Settings(database_path=seeded))) as c:
        yield c


def test_bank_list_shows_public_question(client: TestClient) -> None:
    r = client.get("/bank")
    assert r.status_code == 200
    assert "说说 volatile 的作用" in r.text
    assert "共 1 道" in r.text, "hidden 的题不该计入公共列表"


def test_bank_list_hides_hidden_question(client: TestClient) -> None:
    assert "这道题被隐藏了" not in client.get("/bank").text


def test_bank_detail_shows_criteria(client: TestClient) -> None:
    r = client.get("/bank/1")
    assert r.status_code == 200
    assert "保证可见性，不保证原子性" in r.text
    assert "volatile" in r.text
    # 考察点必须是**列表项**，不是掉进别的分支（模板里有个"还没有考察点"的兜底段落）
    assert "还没有考察点" not in r.text


def test_hidden_question_detail_is_404_not_leaked(client: TestClient) -> None:
    """不可见的题必须 404，**且响应里不能出现它的题干** —— 那等于泄露内容。"""
    r = client.get("/bank/2")
    assert r.status_code == 404
    assert "这道题被隐藏了" not in r.text


def test_unknown_question_is_404(client: TestClient) -> None:
    assert client.get("/bank/9999").status_code == 404
