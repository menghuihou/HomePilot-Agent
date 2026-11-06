"""add approvals table

Revision ID: d4e5f6a7b8c9
Revises: b5c6d7e8f9a0
Create Date: 2026-11-05 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增审批记录表：高风险操作挂起、决策与执行轨迹。

    partial unique index 兜底「同一会话最多一条 pending 审批」的不变量。
    """
    op.create_table(
        "approvals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.String(length=100), nullable=False),
        sa.Column("home_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("target_device_id", sa.BigInteger(), nullable=True),
        sa.Column("risk_level", sa.String(length=10), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_approvals_pending_per_thread",
        "approvals",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text("decision IS NULL"),
    )


def downgrade() -> None:
    """删除 pending 唯一索引与审批表。"""
    op.drop_index("uq_approvals_pending_per_thread", table_name="approvals")
    op.drop_table("approvals")
