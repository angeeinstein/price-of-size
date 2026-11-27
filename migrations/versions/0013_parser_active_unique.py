"""parser: at most one active version per domain

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-27 20:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # should a domain have several active versions, the newest stays active
    op.execute(
        """
        UPDATE parser p SET active = false
        WHERE p.active AND EXISTS (
          SELECT 1 FROM parser q
          WHERE q.domain = p.domain AND q.active
            AND (q.version, q.created_at, q.id) > (p.version, p.created_at, p.id))
        """
    )
    op.create_index(
        "uq_parser_active_domain",
        "parser",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index("uq_parser_active_domain", table_name="parser")
