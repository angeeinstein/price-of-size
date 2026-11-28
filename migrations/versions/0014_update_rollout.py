"""update_rollout: one-click updates of the whole installation

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-28 03:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "update_rollout",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("from_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="coordinator", nullable=False),
        sa.Column("canary_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('coordinator','canary','nodes','done','failed','cancelled')",
            name="update_rollout_status",
        ),
    )
    op.create_index(
        "uq_update_rollout_active",
        "update_rollout",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("status IN ('coordinator','canary','nodes')"),
    )


def downgrade() -> None:
    op.drop_index("uq_update_rollout_active", table_name="update_rollout")
    op.drop_table("update_rollout")
