"""add knowledge chunks

Revision ID: c1d2e3f4a5b6
Revises: b9c8d7e6f5a4
Create Date: 2026-11-25 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b9c8d7e6f5a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp_columns() -> list[sa.Column]:
    """知识库两表共用的审计列，保持与既有核心表一致。"""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    """新增知识来源与知识块表，建立 RAG 多路检索索引。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("device_type", sa.String(length=30), nullable=False),
        sa.Column("brand", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("knowledge_version", sa.String(length=30), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "source_type", sa.String(length=20), server_default="seed", nullable=False
        ),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_key", "knowledge_version", name="uq_knowledge_sources_key"
        ),
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("knowledge_version", sa.String(length=30), nullable=False),
        sa.Column("chunk_key", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("chunk_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="active", nullable=False
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('active', 'retired')", name="ck_knowledge_chunks_status"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["knowledge_sources.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "chunk_key",
            "knowledge_version",
            name="uq_knowledge_chunks_key",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_tsv",
        "knowledge_chunks",
        ["search_tsv"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_chunks_trgm",
        "knowledge_chunks",
        [sa.text("search_text gin_trgm_ops")],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_chunks_embedding",
        "knowledge_chunks",
        [sa.text("embedding vector_cosine_ops")],
        unique=False,
        postgresql_using="hnsw",
    )
    op.create_index(
        "ix_knowledge_chunks_metadata",
        "knowledge_chunks",
        ["metadata"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    """按索引、子表、父表的顺序回滚知识 RAG 表。"""
    op.drop_index("ix_knowledge_chunks_metadata", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_embedding", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_trgm", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_tsv", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_sources")
