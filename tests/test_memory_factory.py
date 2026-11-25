"""第 7 周第 2 步：记忆后端配置切换与边界 DTO 的静态契约测试。"""

import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.memory import (
    DatabaseMemoryBackend,
    GraphMemoryBackend,
    Mem0Backend,
    MemoryBackendConfigError,
    build_memory_backend,
)
from app.schemas import (
    Citation,
    MemoryItem,
    MemoryQuery,
    MemoryScope,
    RetrievalHit,
    RetrievalResult,
)


def _settings(**overrides: str) -> Settings:
    """构造不读取本地 .env 的最小 Settings，避免测试受开发者配置影响。"""
    values = {
        "database_url": "postgresql+asyncpg://user:pass@localhost:5432/test",
        "database_url_sync": "postgresql+psycopg://user:pass@localhost:5432/test",
        "memory_backend": "database",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_database_is_default_memory_backend():
    backend = build_memory_backend(_settings(), database_session_factory=object())

    assert isinstance(backend, DatabaseMemoryBackend)


def test_unknown_memory_backend_is_rejected():
    settings = _settings()
    settings.__dict__["memory_backend"] = "redis"

    with pytest.raises(MemoryBackendConfigError, match="未知记忆后端"):
        build_memory_backend(settings)


def test_mem0_backend_requires_api_key():
    with pytest.raises(ValidationError, match="MEM0_API_KEY"):
        _settings(memory_backend="mem0")


def test_mem0_backend_can_be_built_with_api_key():
    settings = _settings(memory_backend="mem0", mem0_api_key="test-key")

    assert isinstance(build_memory_backend(settings), Mem0Backend)


def test_graph_backend_requires_endpoint():
    with pytest.raises(ValidationError, match="GRAPH_MEMORY_ENDPOINT"):
        _settings(memory_backend="graph")


def test_graph_backend_is_explicit_skeleton_when_configured():
    settings = _settings(
        memory_backend="graph", graph_memory_endpoint="http://graph.local"
    )
    backend = build_memory_backend(settings)
    query = MemoryQuery(scope=MemoryScope(home_id=1))

    assert isinstance(backend, GraphMemoryBackend)
    assert backend.endpoint == "http://graph.local"
    with pytest.raises(NotImplementedError, match="scheduled after week 7"):
        asyncio.run(backend.retrieve(query))


def test_memory_item_rejects_empty_key_and_value():
    scope = MemoryScope(home_id=1)

    with pytest.raises(ValidationError):
        MemoryItem(scope=scope, key="", value="主灯")
    with pytest.raises(ValidationError):
        MemoryItem(scope=scope, key="alias:客厅灯", value="")


def test_retrieval_result_projects_citations_from_hits():
    citation = Citation(
        source_id=1,
        chunk_id=12,
        source_key="lighting_knowledge",
        title="灯泡损坏",
        chunk_index=12,
        device_type="light",
        knowledge_version="2026.11-seed-v1",
        score=0.9,
    )
    result = RetrievalResult(
        query="客厅灯不亮",
        hits=[RetrievalHit(citation=citation, chunk_text="灯泡寿命到期...")],
    )

    assert result.citations == [citation]
