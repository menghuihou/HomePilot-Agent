"""use partial unique key for active knowledge chunks

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-11-25 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许同一 key/version 保留 retired 历史，但 active 行仍全局唯一。"""
    op.drop_constraint("uq_knowledge_chunks_key", "knowledge_chunks", type_="unique")
    op.create_index(
        "uq_knowledge_chunks_key",
        "knowledge_chunks",
        ["source_id", "chunk_key", "knowledge_version"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    """恢复表级唯一约束；若已有同 key retired 历史需先归档清理。"""
    op.drop_index("uq_knowledge_chunks_key", table_name="knowledge_chunks")
    op.create_unique_constraint(
        "uq_knowledge_chunks_key",
        "knowledge_chunks",
        ["source_id", "chunk_key", "knowledge_version"],
    )
