"""Aggregate counts and latency percentiles read from PostgreSQL."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from launchbridge.models import (
    Delivery,
    DeliveryStatus,
    Event,
    EventStatus,
    Replay,
    SignatureRejection,
)


def collect_stats(
    session: Session, *, source: str | None = None, since: datetime | None = None
) -> dict:
    event_filters = []
    if source:
        event_filters.append(Event.source == source)
    if since is not None:
        event_filters.append(Event.received_at >= since)

    events = session.execute(
        select(
            func.count().filter(Event.status == EventStatus.ACCEPTED),
            func.count().filter(Event.status == EventStatus.DEDUPLICATED),
        ).where(*event_filters)
    ).one()

    delivery_base = (
        select(Delivery).join(Event, Event.id == Delivery.event_id).where(*event_filters)
    )
    delivery_ids = delivery_base.with_only_columns(Delivery.id).subquery()

    by_status = dict(
        session.execute(
            select(Delivery.status, func.count())
            .where(Delivery.id.in_(select(delivery_ids.c.id)))
            .group_by(Delivery.status)
        ).all()
    )
    retried = session.scalar(
        select(func.coalesce(func.sum(func.greatest(Delivery.attempts - 1, 0)), 0)).where(
            Delivery.id.in_(select(delivery_ids.c.id))
        )
    )
    replayed = session.scalar(
        select(func.count())
        .select_from(Replay)
        .where(Replay.original_delivery_id.in_(select(delivery_ids.c.id)))
    )
    replayed_delivered = session.scalar(
        select(func.count()).where(
            Delivery.id.in_(select(delivery_ids.c.id)),
            Delivery.replay_of.is_not(None),
            Delivery.status == DeliveryStatus.DELIVERED,
        )
    )
    p50, p95 = session.execute(
        select(
            func.percentile_cont(0.5).within_group(Delivery.latency_ms),
            func.percentile_cont(0.95).within_group(Delivery.latency_ms),
        ).where(
            Delivery.id.in_(select(delivery_ids.c.id)),
            Delivery.status == DeliveryStatus.DELIVERED,
        )
    ).one()

    rejection_filters = []
    if source:
        rejection_filters.append(SignatureRejection.source == source)
    if since is not None:
        rejection_filters.append(SignatureRejection.rejected_at >= since)
    rejections = session.scalar(
        select(func.count()).select_from(SignatureRejection).where(*rejection_filters)
    )

    return {
        "events": {
            "accepted": events[0],
            "deduplicated": events[1],
            "received": events[0] + events[1],
        },
        "deliveries": {
            status.value: int(by_status.get(status.value, 0)) for status in DeliveryStatus
        },
        "retries": int(retried or 0),
        "replays": {"requested": int(replayed or 0), "delivered": int(replayed_delivered or 0)},
        "signature_rejections": int(rejections or 0),
        "latency_ms": {
            "p50": None if p50 is None else round(float(p50), 1),
            "p95": None if p95 is None else round(float(p95), 1),
        },
    }
