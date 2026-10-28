from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类；Alembic 通过 Base.metadata 自动生成迁移。"""


engine = create_async_engine(get_settings().database_url)

session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncSession:
    """FastAPI 依赖：为每个请求提供独立会话，请求结束后自动关闭。"""
    async with session_factory() as session:
        yield session
