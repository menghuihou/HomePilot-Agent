from typing import Any, Generator
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import application


def _sync_conninfo() -> str:
    """把 SQLAlchemy 同步连接串转换为 psycopg 可用的标准 PostgreSQL URI。"""
    return get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@pytest.fixture(scope="session")
def db_ready() -> None:
    """会话开始前确认本地 PostgreSQL 可连接，未启动则整体跳过测试。"""
    try:
        with psycopg.connect(_sync_conninfo(), connect_timeout=3):
            pass
    except psycopg.OperationalError as exc:
        pytest.skip(f"本地 PostgreSQL 未运行，请先执行 docker compose up -d（{exc}）")


@pytest.fixture(scope="session")
def client(db_ready) -> Generator[TestClient, Any, None]:
    """进程内调用 FastAPI 应用，不依赖正在运行的 uvicorn 服务。

    会话级共享同一个 TestClient：TestClient 内部维护独立事件循环，
    共享实例可避免 asyncpg 连接池跨事件循环复用导致连接失败。
    """
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture()
def home_id(client) -> Generator[Any, Any, None]:
    """创建一个测试家庭，测试结束后清理其下全部关联数据。"""
    name = f"test-home-{uuid4().hex[:8]}"
    resp = client.post("/homes", json={"name": name})
    assert resp.status_code == 201, resp.text
    home_id = resp.json()["id"]
    yield home_id
    _cleanup_home(home_id)


def _cleanup_home(home_id: int) -> None:
    """按外键依赖顺序删除测试家庭及其子数据，保证开发库不残留测试数据。"""
    with psycopg.connect(_sync_conninfo()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM device_events
            WHERE device_id IN (SELECT id FROM devices WHERE home_id = %s)
            """,
            (home_id,),
        )
        cur.execute("DELETE FROM devices WHERE home_id = %s", (home_id,))
        cur.execute("DELETE FROM rooms WHERE home_id = %s", (home_id,))
        cur.execute("DELETE FROM members WHERE home_id = %s", (home_id,))
        cur.execute("DELETE FROM homes WHERE id = %s", (home_id,))
        conn.commit()
