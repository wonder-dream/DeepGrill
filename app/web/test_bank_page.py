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


def test_bank_filter_form_is_rendered(client: TestClient) -> None:
    body = client.get("/bank").text
    assert 'name="kind"' in body and 'name="diff"' in body
    assert 'name="domain"' in body and 'name="point"' in body and 'name="q"' in body
    assert "data-points" in body, "级联 JS 要的全量知识点 JSON 应藏在下拉里"


def test_bank_filters_apply(client: TestClient) -> None:
    """四个筛选条件各验一条：题型 / 难度档 / 领域+知识点 / 关键词。"""
    assert "共 0 道" in client.get("/bank?kind=design").text
    assert "共 1 道" in client.get("/bank?kind=knowledge").text
    # 筛选值以**中文**显示在标题上（枚举原值是内部实现，页面印的是 kind_label）
    assert "（知识点题）" in client.get("/bank?kind=knowledge").text
    assert "（项目设计题）" in client.get("/bank?kind=design").text
    assert "共 1 道" in client.get("/bank?diff=easy").text       # 难度 2 ∈ 1-2
    assert "共 0 道" in client.get("/bank?diff=hard").text
    assert "共 1 道" in client.get("/bank?domain=1").text
    assert "共 1 道" in client.get("/bank?point=1").text
    assert "共 0 道" in client.get("/bank?q=不存在的词").text
    assert "共 1 道" in client.get("/bank?q=volatile").text
    # 筛选要跟着翻页走：跳页表单把全部条件带在隐藏域里
    body = client.get("/bank?kind=knowledge&q=volatile&size=10").text
    assert 'name="kind" value="knowledge"' in body and 'name="q" value="volatile"' in body


def test_bank_detail_shows_criteria(client: TestClient) -> None:
    r = client.get("/bank/1")
    assert r.status_code == 200
    assert "保证可见性，不保证原子性" in r.text
    assert "volatile" in r.text
    # 考察点必须是**列表项**，不是掉进别的分支（模板里有个"还没有考察点"的兜底段落）
    assert "还没有考察点" not in r.text
    # 题型与来源印中文（`kind` / `origin` 是内部枚举，不该原样上页面）
    assert "<dd>知识点题</dd>" in r.text
    assert "公共 · 题库预置" in r.text
    assert "seed" not in r.text


def test_hidden_question_detail_is_404_not_leaked(client: TestClient) -> None:
    """不可见的题必须 404，**且响应里不能出现它的题干** —— 那等于泄露内容。"""
    r = client.get("/bank/2")
    assert r.status_code == 404
    assert "这道题被隐藏了" not in r.text


def test_unknown_question_is_404(client: TestClient) -> None:
    assert client.get("/bank/9999").status_code == 404


# ---------------------------------------------------------------------------
# 越界的整数参数（第 1/3/6 轮实测：7 处 500）
# ---------------------------------------------------------------------------
def test_an_out_of_range_question_id_is_a_400_not_a_500(client: TestClient) -> None:
    """`/bank/2**63` 是**输入错误**，不是服务端错误。

    SQLite 的绑定参数是 64 位，驱动会抛 `OverflowError` —— 修之前它是 500。
    """
    r = client.get(f"/bank/{2**63}")
    assert r.status_code == 400
    assert "参数超出范围" in r.text


def test_an_out_of_range_page_is_a_400_not_a_500(client: TestClient) -> None:
    """页号越界同理，而且这一条在 `bank.service.offset_for` 里就挡住了（消息更准）。"""
    r = client.get(f"/bank?page={2**63}")
    assert r.status_code == 400
    assert "页号超出范围" in r.text
