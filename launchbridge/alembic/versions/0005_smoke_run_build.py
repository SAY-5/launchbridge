"""build identifier on reported smoke runs

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("smoke_runs", sa.Column("git_sha", sa.String(64)))


def downgrade() -> None:
    op.drop_column("smoke_runs", "git_sha")
