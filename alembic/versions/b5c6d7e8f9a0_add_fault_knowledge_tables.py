"""add fault knowledge tables

Revision ID: b5c6d7e8f9a0
Revises: a1c49d77b826
Create Date: 2026-11-03 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5c6d7e8f9a0"
down_revision: str | Sequence[str] | None = "a1c49d77b826"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp_columns() -> list[sa.Column]:
    """三张表共用的审计列，保持与既有核心表一致。"""
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
    """新增故障知识库三张表：症状、原因、症状-原因映射。"""
    op.create_table(
        "fault_symptoms",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("device_type", sa.String(length=30), nullable=False),
        sa.Column("symptom_key", sa.String(length=50), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "device_type", "symptom_key", name="uq_fault_symptoms_domain_key"
        ),
    )
    op.create_table(
        "fault_causes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("device_type", sa.String(length=30), nullable=False),
        sa.Column("cause_key", sa.String(length=50), nullable=False),
        sa.Column("cause_name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("prevalence", sa.Float(), nullable=False),
        sa.Column("investigation_weight", sa.Integer(), nullable=False),
        sa.Column("fix_action", sa.String(length=50), nullable=True),
        sa.Column("fix_suggestion", sa.String(length=300), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "device_type", "cause_key", name="uq_fault_causes_domain_key"
        ),
    )
    op.create_table(
        "symptom_causes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("symptom_id", sa.BigInteger(), nullable=False),
        sa.Column("cause_id", sa.BigInteger(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["symptom_id"], ["fault_symptoms.id"]),
        sa.ForeignKeyConstraint(["cause_id"], ["fault_causes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symptom_id", "cause_id", name="uq_symptom_causes_pair"),
    )


def downgrade() -> None:
    """按依赖逆序删除三张知识库表。"""
    op.drop_table("symptom_causes")
    op.drop_table("fault_causes")
    op.drop_table("fault_symptoms")
