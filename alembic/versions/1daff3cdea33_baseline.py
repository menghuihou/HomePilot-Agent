"""baseline

Revision ID: 1daff3cdea33
Revises:
Create Date: 2026-10-28 10:06:33.863505

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "1daff3cdea33"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
