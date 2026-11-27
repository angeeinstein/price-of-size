"""price_review: the LLM's verdicts on flagged offers

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-27 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_review",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("offer_id", sa.UUID(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("validator_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verdict IN ('valid','placeholder','wrong_price_type','pack_mismatch',"
            "'wrong_product','unclear')",
            name="price_review_verdict",
        ),
        sa.ForeignKeyConstraint(["offer_id"], ["offer.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_price_review_offer_id", "price_review", ["offer_id"])


def downgrade() -> None:
    op.drop_index("ix_price_review_offer_id", table_name="price_review")
    op.drop_table("price_review")
