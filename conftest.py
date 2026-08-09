import pytest

from app.db import get_session, init_db


@pytest.fixture
def db():
    """每个测试独立的内存 SQLite。"""
    init_db("sqlite:///:memory:")
    with get_session() as session:
        yield session


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """每个测试清空限速计数（TestClient 共享同一 IP，避免跨测试污染）。"""
    from app.ratelimit import reset

    reset()
    yield
