"""add home mode tables

Revision ID: b9c8d7e6f5a4
Revises: f7a8b9c0d1e2
Create Date: 2026-11-18 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c8d7e6f5a4"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增家庭模式两张表：模式策略（声明式期望状态）与模式历史（迁移快照）。

    home_policies 以 (home_id, mode) 唯一约束兜底「每个家庭每个模式一套
    策略」的不变量；home_modes 以 (home_id, started_at) 索引支撑「当前
    模式 = ended_at IS NULL 的最新记录」查询。
    """
    op.create_table(
        "home_policies",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("home_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=50), nullable=False),
        sa.Column(
            "device_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "priority", sa.Integer(), server_default=sa.text("100"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
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
        sa.ForeignKeyConstraint(["home_id"], ["homes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("home_id", "mode", name="uq_home_policies_home_mode"),
    )
    op.create_table(
        "home_modes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("home_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=50), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["home_id"], ["homes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_home_modes_home_started",
        "home_modes",
        ["home_id", "started_at"],
    )


def downgrade() -> None:
    """删除模式历史索引与两张表（先子表后父表）。"""
    op.drop_index("ix_home_modes_home_started", table_name="home_modes")
    op.drop_table("home_modes")
    op.drop_table("home_policies")
