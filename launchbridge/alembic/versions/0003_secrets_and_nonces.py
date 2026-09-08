"""per-source rotated secrets and the signature nonce store

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_secrets",
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column("current_secret", sa.String(255), nullable=False),
        sa.Column("previous_secret", sa.String(255)),
        sa.Column("previous_expires_at", sa.DateTime(timezone=True)),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "signature_nonces",
        sa.Column("signature", sa.String(80), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_signature_nonces_seen_at", "signature_nonces", ["seen_at"])


def downgrade() -> None:
    op.drop_table("signature_nonces")
    op.drop_table("source_secrets")
