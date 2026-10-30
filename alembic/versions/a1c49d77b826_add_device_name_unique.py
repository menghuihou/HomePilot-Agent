"""add device name unique constraint

Revision ID: a1c49d77b826
Revises: 4b37044807b6
Create Date: 2026-10-30 16:04:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c49d77b826"
down_revision: str | Sequence[str] | None = "4b37044807b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """让数据库兜底同家庭设备名唯一的业务不变量。"""
    op.create_unique_constraint(
        "uq_devices_home_name",
        "devices",
        ["home_id", "name"],
    )


def downgrade() -> None:
    """移除设备名唯一约束。"""
    op.drop_constraint("uq_devices_home_name", "devices", type_="unique")
