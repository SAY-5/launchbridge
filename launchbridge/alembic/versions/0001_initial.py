"""initial schema

Revision ID: 0001
Revises: None
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(80), nullable=False),
        sa.Column("signed_at", sa.Integer, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=True),
        sa.Column("raw_body", sa.Text, nullable=False),
        sa.Column("headers", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_events_source_received", "events", ["source", "received_at"])

    op.create_table(
        "processed_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("signature", sa.String(80), nullable=False),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "event_key", name="uq_processed_events_source_key"),
        sa.UniqueConstraint("signature", name="uq_processed_events_signature"),
    )
    op.create_index(
        "ix_processed_events_processed_at", "processed_events", ["processed_at"]
    )

    op.create_table(
        "deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("destination", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("series", sa.Integer, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_status_code", sa.Integer),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "replay_of",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deliveries.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("replayed_at", sa.DateTime(timezone=True)),
        sa.Column("latency_ms", sa.Integer),
    )
    op.create_index("ix_deliveries_status_next", "deliveries", ["status", "next_attempt_at"])
    op.create_index("ix_deliveries_event", "deliveries", ["event_id"])
    op.create_index("ix_deliveries_idempotency", "deliveries", ["idempotency_key"])

    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "delivery_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("status_code", sa.Integer),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("succeeded", sa.Boolean, nullable=False),
    )
    op.create_index("ix_delivery_attempts_delivery", "delivery_attempts", ["delivery_id"])

    op.create_table(
        "replays",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "original_delivery_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "new_delivery_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "signature_rejections",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_signature_rejections_source_at", "signature_rejections", ["source", "rejected_at"]
    )


def downgrade() -> None:
    op.drop_table("signature_rejections")
    op.drop_table("replays")
    op.drop_table("delivery_attempts")
    op.drop_table("deliveries")
    op.drop_table("processed_events")
    op.drop_table("events")
