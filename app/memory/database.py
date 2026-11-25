"""PostgreSQL 长期记忆后端骨架；Day 3 会补齐读写与契约测试。"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas import MemoryItem, MemoryQuery


class DatabaseMemoryBackend:
    """复用既有 preferences 表的默认后端，保持本地开发零外部依赖。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def store(self, item: MemoryItem) -> MemoryItem:
        """占位实现；第 4 步会替换为 preferences 表 upsert。"""
        raise NotImplementedError("database memory backend will be added in step 4")

    async def retrieve(self, query: MemoryQuery) -> list[MemoryItem]:
        """占位实现；第 4 步会按 home/member/key 过滤查询。"""
        raise NotImplementedError("database memory backend will be added in step 4")

    async def forget(self, query: MemoryQuery) -> int:
        """占位实现；第 4 步会返回真实删除数量。"""
        raise NotImplementedError("database memory backend will be added in step 4")
