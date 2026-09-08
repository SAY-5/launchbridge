"""Inbound event recording: signature replay guard, dedup ledger insert, delivery fan-out."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from launchbridge import metrics
from launchbridge.destinations import DestinationRegistry
from launchbridge.models import (
    Delivery,
    DeliveryStatus,
    Event,
    EventStatus,
    ProcessedEvent,
    SignatureRejection,
)
from launchbridge.signing import content_hash

MAX_EVENT_KEY_LENGTH = 255


class SignatureReplayedError(Exception):
    """The exact same signature was already accepted: a replayed request."""


@dataclass
class IngestResult:
    event_id: uuid.UUID
    deduplicated: bool
    delivery_ids: list[uuid.UUID] = field(default_factory=list)


def derive_event_key(payload: object, body: bytes, header_event_id: str | None) -> str:
    """Prefer an explicit event id header, then a payload `id`, then the content hash."""
    if header_event_id:
        return f"id:{header_event_id}"[:MAX_EVENT_KEY_LENGTH]
    if isinstance(payload, dict) and payload.get("id") not in (None, ""):
        return f"id:{payload['id']}"[:MAX_EVENT_KEY_LENGTH]
    return f"hash:{content_hash(body)}"


def parse_payload(body: bytes) -> dict | list | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict | list) else None


def idempotency_key(event_id: uuid.UUID, destination: str) -> str:
    return f"{event_id}:{destination}"


def ingest_event(
    session: Session,
    *,
    source: str,
    body: bytes,
    headers: dict[str, str],
    signature: str,
    signed_at: int,
    now: datetime,
    registry: DestinationRegistry,
) -> IngestResult:
    """Record an inbound request and enqueue deliveries unless it is a duplicate.

    Commits on success. Raises SignatureReplayedError (after rolling back) when the
    signature was seen before.
    """
    replayed = session.scalar(
        select(ProcessedEvent.id).where(ProcessedEvent.signature == signature)
    )
    if replayed is not None:
        raise SignatureReplayedError(signature)

    payload = parse_payload(body)
    event_key = derive_event_key(payload, body, headers.get("x-event-id"))
    event = Event(
        source=source,
        event_key=event_key,
        content_hash=content_hash(body),
        signature=signature,
        signed_at=signed_at,
        payload=payload,
        raw_body=body.decode("utf-8", errors="replace"),
        headers=headers,
        status=EventStatus.ACCEPTED,
        received_at=now,
    )
    session.add(event)
    session.flush()

    ledger = (
        pg_insert(ProcessedEvent)
        .values(
            source=source,
            event_key=event_key,
            signature=signature,
            event_id=event.id,
            processed_at=now,
        )
        .on_conflict_do_nothing(index_elements=["source", "event_key"])
        .returning(ProcessedEvent.id)
    )
    try:
        inserted = session.execute(ledger).scalar()
    except IntegrityError as exc:
        session.rollback()
        raise SignatureReplayedError(signature) from exc

    if inserted is None:
        event.status = EventStatus.DEDUPLICATED
        session.commit()
        metrics.EVENTS_DEDUPLICATED.labels(source=source).inc()
        return IngestResult(event_id=event.id, deduplicated=True)

    deliveries = [
        Delivery(
            event_id=event.id,
            destination=dest.name,
            idempotency_key=idempotency_key(event.id, dest.name),
            status=DeliveryStatus.PENDING,
            series=1,
            attempts=0,
            max_attempts=dest.retry.max_attempts,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        for dest in registry.for_source(source)
    ]
    session.add_all(deliveries)
    session.commit()
    metrics.EVENTS_RECEIVED.labels(source=source).inc()
    return IngestResult(
        event_id=event.id, deduplicated=False, delivery_ids=[d.id for d in deliveries]
    )


def record_rejection(session: Session, source: str, reason: str, now: datetime) -> None:
    session.add(SignatureRejection(source=source, reason=reason, rejected_at=now))
    session.commit()
    metrics.SIGNATURE_REJECTIONS.labels(source=source, reason=reason).inc()
