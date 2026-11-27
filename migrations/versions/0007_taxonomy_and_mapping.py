"""taxonomy names/version, unique external ids; category_mapping

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-27 14:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "taxonomy_node",
        sa.Column(
            "names",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("taxonomy_node", sa.Column("version", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_taxonomy_node_external", "taxonomy_node", ["taxonomy", "external_id"]
    )
    op.create_table(
        "category_mapping",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("category_path", sa.Text(), nullable=False),
        sa.Column("taxonomy_node_id", sa.UUID(), nullable=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "method IN ('manual','llm','rule','import')", name="category_mapping_method"
        ),
        sa.ForeignKeyConstraint(["taxonomy_node_id"], ["taxonomy_node.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_category_mapping_current",
        "category_mapping",
        ["source", "category_path"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )


def downgrade() -> None:
    op.drop_index("uq_category_mapping_current", table_name="category_mapping")
    op.drop_table("category_mapping")
    op.drop_constraint("uq_taxonomy_node_external", "taxonomy_node", type_="unique")
    op.drop_column("taxonomy_node", "version")
    op.drop_column("taxonomy_node", "names")
