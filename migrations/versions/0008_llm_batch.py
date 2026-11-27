"""llm_batch: Message Batches until their results are collected

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-27 15:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_batch",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("remote_id", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="submitted", nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "requests",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('submitted','ended','failed')", name="llm_batch_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_id"),
    )
    op.create_index("ix_llm_batch_status", "llm_batch", ["status"])


def downgrade() -> None:
    op.drop_index("ix_llm_batch_status", table_name="llm_batch")
    op.drop_table("llm_batch")
