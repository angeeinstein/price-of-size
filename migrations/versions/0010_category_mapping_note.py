"""category_mapping.note; one current classification per product

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-27 17:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("category_mapping", sa.Column("note", sa.Text(), nullable=True))
    # a product had at most one current classification by construction; should a race
    # have left two, the older ones become history before the index enforces it
    op.execute("""
        UPDATE classification SET is_current = false
        WHERE id IN (
          SELECT id FROM (
            SELECT id, row_number() OVER (
              PARTITION BY product_id ORDER BY created_at DESC, id DESC) AS rn
            FROM classification WHERE is_current) t
          WHERE rn > 1)
    """)
    op.create_index(
        "uq_classification_current",
        "classification",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )


def downgrade() -> None:
    op.drop_index("uq_classification_current", table_name="classification")
    op.drop_column("category_mapping", "note")
