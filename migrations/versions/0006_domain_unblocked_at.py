"""domain_state.unblocked_at

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-27 13:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "domain_state", sa.Column("unblocked_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("domain_state", "unblocked_at")
