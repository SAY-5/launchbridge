"""Aggregate counts and latency percentiles read from PostgreSQL."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from launchbridge.destinations import DestinationRegistry
from launchbridge.models import (
    Delivery,
    DeliveryStatus,
    DestinationState,
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


def _pct(value: object) -> float | None:
    return None if value is None else round(float(value), 1)


def collect_overview(
    session: Session, registry: DestinationRegistry, *, since: datetime | None = None
) -> dict:
    """Per-source and per-destination counters for the ops view, plus breaker states.

    Every number comes from one grouped query over the same rows `/stats` reads, so the two
    endpoints agree; `since` filters on the event's arrival time.
    """
    event_filters = [Event.received_at >= since] if since is not None else []

    by_source = {
        source: {
            "received": int(accepted + deduplicated),
            "accepted": int(accepted),
            "deduplicated": int(deduplicated),
        }
        for source, accepted, deduplicated in session.execute(
            select(
                Event.source,
                func.count().filter(Event.status == EventStatus.ACCEPTED),
                func.count().filter(Event.status == EventStatus.DEDUPLICATED),
            )
            .where(*event_filters)
            .group_by(Event.source)
        )
    }
    rejection_filters = [SignatureRejection.rejected_at >= since] if since is not None else []
    for source, count in session.execute(
        select(SignatureRejection.source, func.count())
        .where(*rejection_filters)
        .group_by(SignatureRejection.source)
    ):
        by_source.setdefault(source, {"received": 0, "accepted": 0, "deduplicated": 0})[
            "rejected"
        ] = int(count)
    for entry in by_source.values():
        entry.setdefault("rejected", 0)

    delivery_rows = session.execute(
        select(
            Event.source,
            Delivery.destination,
            Delivery.status,
            func.count(),
            func.coalesce(func.sum(func.greatest(Delivery.attempts - 1, 0)), 0),
            func.count().filter(Delivery.replay_of.is_not(None)),
        )
        .join(Event, Event.id == Delivery.event_id)
        .where(*event_filters)
        .group_by(Event.source, Delivery.destination, Delivery.status)
    ).all()

    def empty() -> dict:
        return {status.value: 0 for status in DeliveryStatus} | {"retries": 0, "replayed_in": 0}

    by_destination: dict[str, dict] = {d.name: empty() for d in registry.destinations}
    for source, destination, status, count, retries, replays in delivery_rows:
        by_source.setdefault(
            source, {"received": 0, "accepted": 0, "deduplicated": 0, "rejected": 0}
        )
        deliveries = by_source[source].setdefault("deliveries", {})
        deliveries[status] = deliveries.get(status, 0) + int(count)
        dest = by_destination.setdefault(destination, empty())
        dest[status] += int(count)
        dest["retries"] += int(retries)
        dest["replayed_in"] += int(replays)
    for entry in by_source.values():
        deliveries = entry.setdefault("deliveries", {})
        for status in DeliveryStatus:
            deliveries.setdefault(status.value, 0)

    latency = session.execute(
        select(
            Delivery.destination,
            func.percentile_cont(0.5).within_group(Delivery.latency_ms),
            func.percentile_cont(0.95).within_group(Delivery.latency_ms),
        )
        .join(Event, Event.id == Delivery.event_id)
        .where(*event_filters, Delivery.status == DeliveryStatus.DELIVERED)
        .group_by(Delivery.destination)
    ).all()
    percentiles = {name: (p50, p95) for name, p50, p95 in latency}
    states = {row.destination: row for row in session.scalars(select(DestinationState))}
    for name, entry in by_destination.items():
        p50, p95 = percentiles.get(name, (None, None))
        entry["latency_ms"] = {"p50": _pct(p50), "p95": _pct(p95)}
        entry["queue_depth"] = entry["pending"] + entry["in_progress"]
        state = states.get(name)
        entry["breaker"] = state.breaker_state if state else "closed"
        entry["consecutive_failures"] = state.consecutive_failures if state else 0

    totals = {
        "received": sum(s["received"] for s in by_source.values()),
        "accepted": sum(s["accepted"] for s in by_source.values()),
        "deduplicated": sum(s["deduplicated"] for s in by_source.values()),
        "rejected": sum(s["rejected"] for s in by_source.values()),
        "delivered": sum(d["delivered"] for d in by_destination.values()),
        "failed": sum(d["failed"] for d in by_destination.values()),
        "replayed": sum(d["replayed"] for d in by_destination.values()),
        "retries": sum(d["retries"] for d in by_destination.values()),
        "queue_depth": sum(d["queue_depth"] for d in by_destination.values()),
        "breakers_open": sum(1 for d in by_destination.values() if d["breaker"] != "closed"),
    }
    all_p50, all_p95 = session.execute(
        select(
            func.percentile_cont(0.5).within_group(Delivery.latency_ms),
            func.percentile_cont(0.95).within_group(Delivery.latency_ms),
        )
        .join(Event, Event.id == Delivery.event_id)
        .where(*event_filters, Delivery.status == DeliveryStatus.DELIVERED)
    ).one()
    totals["latency_ms"] = {"p50": _pct(all_p50), "p95": _pct(all_p95)}
    return {
        "since": since.isoformat() if since else None,
        "totals": totals,
        "sources": dict(sorted(by_source.items())),
        "destinations": dict(sorted(by_destination.items())),
    }
