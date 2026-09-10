"""reported smoke suite runs

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "smoke_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("passed", sa.Integer, nullable=False),
        sa.Column("failed", sa.Integer, nullable=False),
        sa.Column("skipped", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.String(32)),
        sa.Column("base_url", sa.String(255)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_smoke_runs_ran_at", "smoke_runs", ["ran_at"])


def downgrade() -> None:
    op.drop_table("smoke_runs")
