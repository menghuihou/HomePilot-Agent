"""DatabaseMemoryBackend 契约测试：upsert、scope 隔离、前缀过滤与删除。"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.memory import DatabaseMemoryBackend, MemoryStoreError
from app.schemas import MemoryItem, MemoryQuery, MemoryScope

MemoryOperation = Callable[[DatabaseMemoryBackend], Awaitable[object]]


def _item(
    home_id: int,
    key: str = "theme",
    value: str = "dark",
    *,
    member_id: int | None = None,
) -> MemoryItem:
    """构造同一语义的记忆项，测试只聚焦 key/value 与 scope 行为。"""
    return MemoryItem(
        scope=MemoryScope(home_id=home_id, member_id=member_id),
        key=key,
        value=value,
    )


def _run_memory_operation(home_id: int, operation: MemoryOperation) -> object:
    """每个用例使用独立引擎，避免 asyncpg 连接池跨事件循环复用。"""
    engine = create_async_engine(get_settings().database_url)

    async def run() -> object:
        backend = DatabaseMemoryBackend(
            async_sessionmaker(engine, expire_on_commit=False)
        )
        try:
            return await operation(backend)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_store_upserts_same_scope_and_key(client, home_id):
    async def operation(backend: DatabaseMemoryBackend):
        first = await backend.store(_item(home_id, value="dark"))
        second = await backend.store(_item(home_id, value="light"))
        results = await backend.retrieve(
            MemoryQuery(scope=MemoryScope(home_id=home_id), key="theme")
        )
        return first, second, results

    first, second, results = _run_memory_operation(home_id, operation)

    assert first.value == "dark"
    assert first.updated_at is not None
    assert second.value == "light"
    assert second.updated_at >= first.updated_at
    assert len(results) == 1
    assert results[0].scope.home_id == home_id
    assert results[0].scope.member_id is None
    assert results[0].key == "theme"
    assert results[0].value == "light"
    assert results[0].source == "chat_tool"


def test_retrieve_is_isolated_by_home(client, home_id, other_home_id):
    async def operation(backend: DatabaseMemoryBackend):
        await backend.store(_item(home_id, value="primary-light"))
        await backend.store(_item(other_home_id, key="theme", value="other-home"))
        own = await backend.retrieve(
            MemoryQuery(scope=MemoryScope(home_id=home_id), key="theme")
        )
        other = await backend.retrieve(
            MemoryQuery(scope=MemoryScope(home_id=other_home_id), key="theme")
        )
        return own, other

    own, other = _run_memory_operation(home_id, operation)

    assert len(own) == 1
    assert own[0].value == "primary-light"
    assert len(other) == 1
    assert other[0].value == "other-home"


def test_member_level_and_home_level_preferences_coexist(client, home_id):
    member_id = client.post(
        f"/homes/{home_id}/members", json={"name": "记忆成员"}
    ).json()["id"]

    async def operation(backend: DatabaseMemoryBackend):
        await backend.store(_item(home_id, value="dark"))
        member_item = _item(home_id, key="theme", value="night", member_id=member_id)
        stored = await backend.store(member_item)
        await backend.store(_item(home_id, value="light"))
        all_items = await backend.retrieve(
            MemoryQuery(scope=MemoryScope(home_id=home_id), key="theme")
        )
        member_items = await backend.retrieve(
            MemoryQuery(
                scope=MemoryScope(home_id=home_id, member_id=member_id), key="theme"
            )
        )
        return stored, all_items, member_items

    stored, all_items, member_items = _run_memory_operation(home_id, operation)

    assert stored.value == "night"
    assert stored.scope.member_id == member_id
    assert len(all_items) == 2
    assert {item.value for item in all_items} == {"light", "night"}
    assert len(member_items) == 1
    assert member_items[0].value == "night"
    assert member_items[0].scope.member_id == member_id


def test_retrieve_supports_key_prefix_and_empty_result(client, home_id):
    async def operation(backend: DatabaseMemoryBackend):
        empty = await backend.retrieve(
            MemoryQuery(scope=MemoryScope(home_id=home_id), key_prefix="alias:")
        )
        forget_count = await backend.forget(
            MemoryQuery(scope=MemoryScope(home_id=home_id), key="not-exists")
        )
        await backend.store(_item(home_id, key="alias:1", value="主灯"))
        await backend.store(_item(home_id, key="alias:2", value="次灯"))
        hits = await backend.retrieve(
            MemoryQuery(
                scope=MemoryScope(home_id=home_id), key_prefix="alias:", limit=10
            )
        )
        return empty, forget_count, hits

    empty, forget_count, hits = _run_memory_operation(home_id, operation)

    assert empty == []
    assert forget_count == 0
    assert len(hits) == 2
    # 同一事务时间戳可能相同，id 倒序提供稳定 tie-break。
    assert hits[0].key == "alias:2"
    assert hits[1].key == "alias:1"


def test_forget_removes_all_matching_memories(client, home_id):
    async def operation(backend: DatabaseMemoryBackend):
        await backend.store(_item(home_id, key="alias:1", value="主灯"))
        await backend.store(_item(home_id, key="alias:2", value="次灯"))
        deleted = await backend.forget(
            MemoryQuery(scope=MemoryScope(home_id=home_id), key_prefix="alias:")
        )
        remaining = await backend.retrieve(
            MemoryQuery(scope=MemoryScope(home_id=home_id), key_prefix="alias:")
        )
        return deleted, remaining

    deleted, remaining = _run_memory_operation(home_id, operation)

    assert deleted == 2
    assert remaining == []


def test_store_rejects_member_from_other_home(client, home_id, other_home_id):
    member_id = client.post(
        f"/homes/{other_home_id}/members", json={"name": "隔壁成员"}
    ).json()["id"]

    async def operation(backend: DatabaseMemoryBackend):
        await backend.store(
            _item(home_id, key="theme", value="dark", member_id=member_id)
        )

    with pytest.raises(MemoryStoreError, match="member not found"):
        _run_memory_operation(home_id, operation)

    listing = client.get(f"/homes/{home_id}/preferences").json()
    assert listing == []
