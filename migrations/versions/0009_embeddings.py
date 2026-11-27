"""embedding.text_hash; taxonomy_embedding

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27 16:40:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("embedding", sa.Column("text_hash", sa.Text(), nullable=True))
    op.create_table(
        "taxonomy_embedding",
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("vector", pgvector.sqlalchemy.Vector(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["node_id"], ["taxonomy_node.id"]),
        sa.PrimaryKeyConstraint("node_id", "model"),
    )


def downgrade() -> None:
    op.drop_table("taxonomy_embedding")
    op.drop_column("embedding", "text_hash")
