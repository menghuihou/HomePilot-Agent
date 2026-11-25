"""Mem0Backend 契约测试：fake SDK 序列化、metadata 过滤与删除。"""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.memory import Mem0Backend, MemoryBackendUnavailableError
from app.schemas import MemoryItem, MemoryQuery, MemoryScope


def _settings(**overrides: str) -> Settings:
    """构造最小 Settings，不读取本地 .env。"""
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pass@localhost:5432/test",
        database_url_sync="postgresql+psycopg://user:pass@localhost:5432/test",
        memory_backend="mem0",
        mem0_api_key="test-key",
        **overrides,
    )


def _item(
    home_id: int,
    key: str = "theme",
    value: str = "dark",
    *,
    member_id: int | None = None,
    device_type: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        scope=MemoryScope(
            home_id=home_id,
            member_id=member_id,
            device_type=device_type,
        ),
        key=key,
        value=value,
    )


class FakeMem0Client:
    """覆盖本实现使用的最小 Mem0 API；搜索返回家庭全量，过滤交给后端。"""

    def __init__(self) -> None:
        self.add_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.delete_calls: list[str] = []
        self.memories: list[dict] = []

    def add(self, messages: str, user_id: str, metadata: dict) -> dict:
        self.add_calls.append(
            {"messages": messages, "user_id": user_id, "metadata": metadata}
        )
        memory = {
            "id": f"memory-{len(self.memories) + 1}",
            "memory": messages,
            "metadata": dict(metadata),
            "created_at": datetime.now(UTC),
            "user_id": user_id,
        }
        self.memories.append(memory)
        return dict(memory)

    def get_all(self, user_id: str, limit: int) -> list[dict]:
        self.search_calls.append(
            {"query": "__get_all__", "user_id": user_id, "limit": limit}
        )
        rows = [row for row in self.memories if row["user_id"] == user_id]
        return rows[:limit]

    def search(self, query: str, user_id: str, limit: int) -> list[dict]:
        self.search_calls.append({"query": query, "user_id": user_id, "limit": limit})
        rows = [row for row in self.memories if row["user_id"] == user_id]
        return list(reversed(rows))[:limit]

    def delete(self, memory_id: str) -> dict:
        self.delete_calls.append(memory_id)
        self.memories = [row for row in self.memories if row["id"] != memory_id]
        return {"deleted": memory_id}


def test_mem0_store_serializes_scope_and_never_persists_api_key():
    fake = FakeMem0Client()
    backend = Mem0Backend(_settings(), client=fake)
    item = _item(
        1,
        key="alias:1",
        value="主灯",
        device_type="light",
    )

    stored = asyncio.run(backend.store(item))

    call = fake.add_calls[0]
    assert call["messages"] == "主灯"
    assert call["user_id"] == "home:1"
    assert call["metadata"] == {
        "home_id": 1,
        "member_id": None,
        "device_type": "light",
        "pref_key": "alias:1",
        "source": "chat_tool",
    }
    assert "test-key" not in json.dumps(fake.add_calls)
    assert stored.scope == item.scope
    assert stored.key == item.key
    assert stored.value == "主灯"
    assert stored.updated_at is not None


def test_mem0_store_overwrites_same_scope_and_key():
    fake = FakeMem0Client()
    backend = Mem0Backend(_settings(), client=fake)

    asyncio.run(backend.store(_item(1, value="dark")))
    stored = asyncio.run(backend.store(_item(1, value="light")))
    results = asyncio.run(
        backend.retrieve(MemoryQuery(scope=MemoryScope(home_id=1), key="theme"))
    )

    assert fake.delete_calls == ["memory-1"]
    assert len(fake.add_calls) == 2
    assert stored.value == "light"
    assert len(results) == 1
    assert results[0].value == "light"


def test_mem0_retrieve_filters_by_metadata_and_home():
    fake = FakeMem0Client()
    backend = Mem0Backend(_settings(), client=fake)
    asyncio.run(backend.store(_item(1, value="primary-light")))
    asyncio.run(backend.store(_item(2, value="other-home")))

    own = asyncio.run(
        backend.retrieve(MemoryQuery(scope=MemoryScope(home_id=1), key="theme"))
    )
    other = asyncio.run(
        backend.retrieve(MemoryQuery(scope=MemoryScope(home_id=2), key="theme"))
    )

    assert len(own) == 1
    assert own[0].value == "primary-light"
    assert len(other) == 1
    assert other[0].value == "other-home"


def test_mem0_member_level_and_home_level_preferences_coexist():
    fake = FakeMem0Client()
    backend = Mem0Backend(_settings(), client=fake)
    asyncio.run(backend.store(_item(1, value="dark")))
    asyncio.run(backend.store(_item(1, value="night", member_id=2)))

    all_items = asyncio.run(
        backend.retrieve(MemoryQuery(scope=MemoryScope(home_id=1), key="theme"))
    )
    member_items = asyncio.run(
        backend.retrieve(
            MemoryQuery(scope=MemoryScope(home_id=1, member_id=2), key="theme")
        )
    )

    assert len(all_items) == 2
    assert {item.value for item in all_items} == {"dark", "night"}
    assert len(member_items) == 1
    assert member_items[0].value == "night"
    assert member_items[0].scope.member_id == 2


def test_mem0_forget_supports_prefix_and_returns_zero_when_missing():
    fake = FakeMem0Client()
    backend = Mem0Backend(_settings(), client=fake)
    asyncio.run(backend.store(_item(1, key="alias:1", value="主灯")))
    asyncio.run(backend.store(_item(1, key="alias:2", value="次灯")))
    asyncio.run(backend.store(_item(1, key="theme", value="dark")))

    missing = asyncio.run(
        backend.forget(MemoryQuery(scope=MemoryScope(home_id=1), key="not-exists"))
    )
    deleted = asyncio.run(
        backend.forget(MemoryQuery(scope=MemoryScope(home_id=1), key_prefix="alias:"))
    )
    remaining = asyncio.run(
        backend.retrieve(MemoryQuery(scope=MemoryScope(home_id=1), key_prefix="alias:"))
    )

    assert missing == 0
    assert deleted == 2
    assert sorted(fake.delete_calls) == ["memory-1", "memory-2"]
    assert remaining == []


def test_mem0_rejects_invalid_metadata_even_when_search_returns_it():
    fake = FakeMem0Client()
    fake.add(
        messages="wrong-home",
        user_id="home:1",
        metadata={
            "home_id": 2,
            "member_id": None,
            "pref_key": "theme",
            "source": "seed",
        },
    )
    backend = Mem0Backend(_settings(), client=fake)

    results = asyncio.run(
        backend.retrieve(MemoryQuery(scope=MemoryScope(home_id=1), key="theme"))
    )

    assert results == []


def test_mem0_missing_optional_dependency_is_explicit_at_call_time():
    def raise_import_error() -> object:
        raise ImportError("No module named 'mem0'")

    backend = Mem0Backend(_settings(), client_factory=raise_import_error)

    with pytest.raises(
        MemoryBackendUnavailableError, match="mem0ai python package is not installed"
    ):
        asyncio.run(
            backend.retrieve(MemoryQuery(scope=MemoryScope(home_id=1), key="theme"))
        )
