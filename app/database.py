from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类；Alembic 通过 Base.metadata 自动生成迁移。"""
