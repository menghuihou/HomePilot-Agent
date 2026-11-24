from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类；Alembic 通过 Base.metadata 自动生成迁移。"""


engine = create_async_engine(
    get_settings().database_url,
    # PG 容器重启后池中死连接会让首个请求 500：取连接前轻量探测，
    # 死连接透明重建（本机往返 <1ms，成本可忽略）。
    pool_pre_ping=True,
)

session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, Any]:
    """FastAPI 依赖：为每个请求提供独立会话，请求结束后自动关闭。"""
    async with session_factory() as session:
        yield session


def checkpoint_conninfo() -> str:
    """AsyncConnectionPool 需要 psycopg 原生 URI：去掉 SQLAlchemy 方言前缀。"""
    return get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


async def build_checkpoint_pool():
    """审批续跑共用的 Postgres 连接池（autocommit；saver setup 必需）。

    池跟随进程生命周期，由调用方负责关闭（生产环境即进程退出）。
    """
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        checkpoint_conninfo(), open=False, kwargs={"autocommit": True}
    )
    await pool.open()
    return pool
