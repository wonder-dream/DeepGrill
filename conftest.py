import pytest

from app.db import get_session, init_db


@pytest.fixture
def db():
    """每个测试独立的内存 SQLite。"""
    init_db("sqlite:///:memory:")
    with get_session() as session:
        yield session
