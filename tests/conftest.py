"""pytest 全局配置：在导入应用前把数据库切换为内存 SQLite。"""

import os

os.environ.setdefault(
    "DATABASE_URL", "sqlite+pysqlite:///:memory:"
)
os.environ.setdefault("BASE_URL", "http://testserver")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database():
    """每个测试用例独立建表/删表，保证非法请求不留记录的断言可靠。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
