"""Home Context Resolver 测试：上下文组装、模式缺省与家庭不存在。"""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.context import DEFAULT_HOME_MODE, HOME_MODE_KEY, resolve_home_context
from app.config import get_settings
from app.models import Home
from db.seeds.load_family import SEED_HOME_NAME


def _resolve(home_id: int):
    """在测试进程内直连数据库调用异步 Resolver。

    每次调用新建独立引擎并在同一事件循环内销毁，避免连接池跨
    事件循环复用（TestClient 与 asyncio.run 的循环互不相通）。
    """

    async def _run():
        engine = create_async_engine(get_settings().database_url)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                return await resolve_home_context(home_id, session)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_resolve_returns_rooms_members_and_preferences(client, home_id):
    client.post(f"/homes/{home_id}/rooms", json={"name": "测试客厅"})
    client.post(f"/homes/{home_id}/members", json={"name": "测试成员"})
    client.put(
        f"/homes/{home_id}/preferences",
        json={"pref_key": HOME_MODE_KEY, "pref_value": "sleep"},
    )
    client.put(
        f"/homes/{home_id}/preferences",
        json={"pref_key": "device_alias:1", "pref_value": "主灯"},
    )

    context = _resolve(home_id)

    assert context.home.id == home_id
    assert [room.name for room in context.rooms] == ["测试客厅"]
    assert [member.name for member in context.members] == ["测试成员"]
    assert len(context.preferences) == 2
    assert context.current_mode == "sleep"


def test_current_mode_defaults_to_normal_when_no_preferences(client, home_id):
    context = _resolve(home_id)

    assert context.current_mode == DEFAULT_HOME_MODE
    assert context.preferences == []


def test_resolve_missing_home_raises_404(db_ready):
    with pytest.raises(HTTPException) as exc_info:
        _resolve(999999)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "home not found"


def test_seed_family_context():
    """种子家庭已加载时验证 Resolver 输出；未加载种子数据则跳过。"""

    async def _run():
        engine = create_async_engine(get_settings().database_url)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                home_id = (
                    (
                        await session.execute(
                            select(Home.id).where(Home.name == SEED_HOME_NAME)
                        )
                    )
                    .scalars()
                    .first()
                )
                if home_id is None:
                    pytest.skip(
                        "种子数据未加载，先执行 uv run python -m db.seeds.load_family"
                    )
                return await resolve_home_context(home_id, session)
        finally:
            await engine.dispose()

    context = asyncio.run(_run())

    assert len(context.rooms) == 3
    assert len(context.members) == 2
