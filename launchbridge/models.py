"""ORM models: raw events, the dedup ledger, deliveries, attempts, replays, rejections."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EventStatus(StrEnum):
    ACCEPTED = "accepted"
    DEDUPLICATED = "deduplicated"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    FAILED = "failed"
    REPLAYED = "replayed"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Event(Base):
    """Every accepted inbound request, including ones later flagged as duplicates."""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(80), nullable=False)
    signed_at: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=EventStatus.ACCEPTED)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    deliveries: Mapped[list[Delivery]] = relationship(back_populates="event")

    __table_args__ = (Index("ix_events_source_received", "source", "received_at"),)


class ProcessedEvent(Base):
    """Dedup ledger keyed by (source, event key); rows expire after a TTL."""

    __tablename__ = "processed_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    signature: Mapped[str] = mapped_column(String(80), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "event_key", name="uq_processed_events_source_key"),
        UniqueConstraint("signature", name="uq_processed_events_signature"),
        Index("ix_processed_events_processed_at", "processed_at"),
    )


class Delivery(Base):
    """One attempt series of an event to one destination."""

    __tablename__ = "deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DeliveryStatus.PENDING
    )
    series: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    replay_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    event: Mapped[Event] = relationship(back_populates="deliveries")
    attempt_log: Mapped[list[DeliveryAttempt]] = relationship(
        back_populates="delivery", order_by="DeliveryAttempt.attempt_number"
    )

    __table_args__ = (
        Index("ix_deliveries_status_next", "status", "next_attempt_at"),
        Index("ix_deliveries_event", "event_id"),
        Index("ix_deliveries_idempotency", "idempotency_key"),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    delivery: Mapped[Delivery] = relationship(back_populates="attempt_log")

    __table_args__ = (Index("ix_delivery_attempts_delivery", "delivery_id"),)


class Replay(Base):
    """Audit trail: who replayed which failed delivery, and the delivery it produced."""

    __tablename__ = "replays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    original_delivery_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    new_delivery_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SignatureRejection(Base):
    __tablename__ = "signature_rejections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_signature_rejections_source_at", "source", "rejected_at"),)
