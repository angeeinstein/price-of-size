"""llm_extraction: tier-4 LLM extraction per raw fetch

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-27 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_extraction",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("fetch_id", sa.UUID(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="wanted", nullable=False),
        sa.Column("missing", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('wanted','ok','invalid','refused')", name="llm_extraction_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fetch_id", "prompt_version", name="uq_llm_extraction_fetch"),
    )
    op.create_index(
        "ix_llm_extraction_wanted",
        "llm_extraction",
        ["created_at"],
        postgresql_where=sa.text("status = 'wanted'"),
    )


def downgrade() -> None:
    op.drop_index("ix_llm_extraction_wanted", table_name="llm_extraction")
    op.drop_table("llm_extraction")
