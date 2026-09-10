"""Aggregate counts, latency percentiles and the ops overview, read from PostgreSQL."""

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
    SmokeRun,
)

FAILED_SAMPLE = 5


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
    endpoints agree; `since` filters on the event's arrival time and, for the replay, on the
    time it was requested. `smoke` is always the most recent reported run.
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
        "failed": failed_summary(session, since=since),
        "last_replay": last_replay(session, since=since),
        "smoke": last_smoke_run(session),
    }


def failed_summary(session: Session, *, since: datetime | None = None) -> dict:
    """How many deliveries are sitting in `failed`, plus the newest few with their errors."""
    event_filters = [Event.received_at >= since] if since is not None else []
    base = (
        select(Delivery, Event.source)
        .join(Event, Event.id == Delivery.event_id)
        .where(*event_filters, Delivery.status == DeliveryStatus.FAILED)
    )
    count = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.execute(base.order_by(Delivery.updated_at.desc()).limit(FAILED_SAMPLE)).all()
    return {
        "count": int(count),
        "recent": [
            {
                "delivery_id": str(delivery.id),
                "source": source,
                "destination": delivery.destination,
                "attempts": delivery.attempts,
                "last_status_code": delivery.last_status_code,
                "last_error": delivery.last_error,
                "failed_at": delivery.updated_at.isoformat(),
            }
            for delivery, source in rows
        ],
    }


def last_replay(session: Session, *, since: datetime | None = None) -> dict | None:
    """The most recent replay with the delivery it re-ran, or None when nothing was replayed."""
    filters = [Replay.requested_at >= since] if since is not None else []
    row = session.execute(
        select(Replay, Delivery.destination, Event.source)
        .join(Delivery, Delivery.id == Replay.original_delivery_id)
        .join(Event, Event.id == Delivery.event_id)
        .where(*filters)
        .order_by(Replay.requested_at.desc(), Replay.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    replay, destination, source = row
    outcome = session.get(Delivery, replay.new_delivery_id)
    return {
        "at": replay.requested_at.isoformat(),
        "actor": replay.actor,
        "mode": replay.mode,
        "reason": replay.reason,
        "source": source,
        "destination": destination,
        "original_delivery_id": str(replay.original_delivery_id),
        "new_delivery_id": str(replay.new_delivery_id),
        "outcome": outcome.status if outcome else None,
    }


def last_smoke_run(session: Session) -> dict | None:
    """The newest reported smoke run; `status` is green only when nothing failed."""
    run = session.scalars(
        select(SmokeRun).order_by(SmokeRun.ran_at.desc(), SmokeRun.id.desc()).limit(1)
    ).first()
    if run is None:
        return None
    return {
        "status": "red" if run.failed else "green",
        "ran_at": run.ran_at.isoformat(),
        "passed": run.passed,
        "failed": run.failed,
        "skipped": run.skipped,
        "checks": run.passed + run.failed + run.skipped,
        "version": run.version,
        "base_url": run.base_url,
        "duration_ms": run.duration_ms,
    }


def record_smoke_run(
    session: Session,
    *,
    passed: int,
    failed: int,
    skipped: int,
    ran_at: datetime,
    version: str | None = None,
    base_url: str | None = None,
    duration_ms: int | None = None,
) -> SmokeRun:
    """Store one smoke result. Commits."""
    run = SmokeRun(
        passed=passed,
        failed=failed,
        skipped=skipped,
        version=version,
        base_url=base_url,
        duration_ms=duration_ms,
        ran_at=ran_at,
    )
    session.add(run)
    session.commit()
    return run
