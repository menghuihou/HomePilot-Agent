"""长期记忆统一契约与后端工厂（第 7 周第 2 步骨架）。

Agent 只依赖本模块导出的 Protocol 与 DTO；Mem0、Graph 等具体 SDK
必须留在对应 backend 内部，避免供应商接口泄漏进业务子图。
"""

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.memory.database import DatabaseMemoryBackend
from app.memory.graph import GraphMemoryBackend
from app.memory.mem0_backend import Mem0Backend
from app.schemas import MemoryItem, MemoryQuery


class MemoryBackend(Protocol):
    """长期记忆后端的稳定契约；具体实现负责 scope 隔离和幂等语义。"""

    async def store(self, item: MemoryItem) -> MemoryItem:
        """按 scope + key upsert；重复写入覆盖值，不产生第二条记录。"""
        ...

    async def retrieve(self, query: MemoryQuery) -> list[MemoryItem]:
        """按 scope / key 检索；无结果返回空列表，不返回 None。"""
        ...

    async def forget(self, query: MemoryQuery) -> int:
        """删除命中的记忆并返回数量；未命中返回 0。"""
        ...


class MemoryBackendConfigError(ValueError):
    """后端名称或必填配置非法；应尽早失败，而不是运行时静默换后端。"""


class MemoryBackendUnavailableError(RuntimeError):
    """后端依赖或外部服务不可用；调用方必须收到明确错误。"""


def build_memory_backend(
    settings: Settings | None = None,
    database_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> MemoryBackend:
    """按配置创建记忆后端；database 允许测试注入独立 session factory。"""
    config = settings or get_settings()
    backend = config.memory_backend
    if backend == "database":
        if database_session_factory is None:
            # 延迟导入：内存后端单测不应要求真实数据库连接已初始化。
            from app.database import session_factory as default_session_factory

            database_session_factory = default_session_factory
        return DatabaseMemoryBackend(database_session_factory)
    if backend == "mem0":
        return Mem0Backend.from_settings(config)
    if backend == "graph":
        return GraphMemoryBackend.from_settings(config)
    raise MemoryBackendConfigError(f"未知记忆后端：{backend}")


__all__ = [
    "DatabaseMemoryBackend",
    "GraphMemoryBackend",
    "Mem0Backend",
    "MemoryBackend",
    "MemoryBackendConfigError",
    "MemoryBackendUnavailableError",
]
