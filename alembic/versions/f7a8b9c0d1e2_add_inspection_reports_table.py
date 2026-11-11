"""add inspection_reports table

Revision ID: f7a8b9c0d1e2
Revises: d4e5f6a7b8c9
Create Date: 2026-11-11 10:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增巡检报告表：巡检 / 家庭日报留档（评估与趋势预测的数据基础）。"""
    op.create_table(
        "inspection_reports",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("home_id", sa.BigInteger(), nullable=False),
        sa.Column("report_type", sa.String(length=20), nullable=False),
        sa.Column("total_devices", sa.Integer(), nullable=False),
        sa.Column("normal_count", sa.Integer(), nullable=False),
        sa.Column("anomalies", JSONB(), nullable=False),
        sa.Column("pending_approvals", JSONB(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=True),
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
        "ix_inspection_reports_home_created",
        "inspection_reports",
        ["home_id", "created_at"],
    )


def downgrade() -> None:
    """删除巡检报告表。"""
    op.drop_index("ix_inspection_reports_home_created", table_name="inspection_reports")
    op.drop_table("inspection_reports")
