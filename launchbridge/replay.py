"""Replay of failed deliveries: a new attempt series with the same idempotency key."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from launchbridge import metrics
from launchbridge.models import Delivery, DeliveryStatus, Event, Replay


class ReplayError(Exception):
    pass


def replay_delivery(
    session: Session,
    delivery: Delivery,
    *,
    actor: str,
    mode: str,
    now: datetime,
    reason: str | None = None,
) -> Delivery:
    """Create a fresh pending delivery for a failed one and mark the original replayed.

    Does not commit; callers batch several replays into one transaction.
    """
    if delivery.status != DeliveryStatus.FAILED:
        raise ReplayError(
            f"delivery {delivery.id} is {delivery.status}, only failed can be replayed"
        )

    replacement = Delivery(
        event_id=delivery.event_id,
        destination=delivery.destination,
        idempotency_key=delivery.idempotency_key,
        status=DeliveryStatus.PENDING,
        series=delivery.series + 1,
        attempts=0,
        max_attempts=delivery.max_attempts,
        next_attempt_at=now,
        replay_of=delivery.id,
        created_at=now,
        updated_at=now,
    )
    session.add(replacement)
    session.flush()

    delivery.status = DeliveryStatus.REPLAYED
    delivery.replayed_at = now
    delivery.updated_at = now
    session.add(
        Replay(
            original_delivery_id=delivery.id,
            new_delivery_id=replacement.id,
            actor=actor,
            mode=mode,
            reason=reason,
            requested_at=now,
        )
    )
    metrics.DELIVERIES_REPLAYED.labels(mode=mode).inc()
    return replacement


def failed_deliveries(
    session: Session,
    *,
    source: str | None,
    since: datetime | None,
    destination: str | None,
    limit: int,
) -> list[Delivery]:
    """Failed deliveries matching the filters, locked for the caller's transaction.

    `FOR UPDATE OF deliveries SKIP LOCKED` is what makes two concurrent bulk replays safe:
    each transaction takes a disjoint set of rows and holds them until it commits, so a
    failed delivery cannot be replaced twice. Rows another admin is already replaying are
    skipped rather than waited for, which keeps the endpoint responsive.
    """
    stmt = (
        select(Delivery)
        .join(Event, Event.id == Delivery.event_id)
        .where(Delivery.status == DeliveryStatus.FAILED)
        .order_by(Delivery.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True, of=Delivery)
    )
    if source:
        stmt = stmt.where(Event.source == source)
    if since is not None:
        stmt = stmt.where(Event.received_at >= since)
    if destination:
        stmt = stmt.where(Delivery.destination == destination)
    return list(session.scalars(stmt).all())


def replay_many(
    session: Session,
    deliveries: list[Delivery],
    *,
    actor: str,
    now: datetime,
    reason: str | None = None,
) -> list[uuid.UUID]:
    """Replay every delivery that is still failed, in one transaction.

    The status is re-read inside the locked transaction: a row that stopped being failed
    since it was selected is skipped instead of raising, so one racing admin does not fail
    the whole batch.
    """
    created = [
        replay_delivery(session, d, actor=actor, mode="bulk", now=now, reason=reason).id
        for d in deliveries
        if d.status == DeliveryStatus.FAILED
    ]
    session.commit()
    return created
