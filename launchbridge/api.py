"""HTTP routes."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from launchbridge import __version__, metrics
from launchbridge.auth import require_api_key
from launchbridge.config import Settings, get_settings
from launchbridge.db import get_session, utcnow
from launchbridge.destinations import DestinationRegistry
from launchbridge.ingest import (
    SignatureReplayedError,
    derive_event_key,
    ingest_event,
    parse_payload,
    record_rejection,
)
from launchbridge.logging import get_logger
from launchbridge.models import Delivery, DeliveryStatus, DestinationState, Event, Replay
from launchbridge.replay import (
    ReplayError,
    failed_deliveries,
    replay_delivery,
    replay_many,
)
from launchbridge.routing import event_type_of
from launchbridge.schemas import (
    BreakerOut,
    BulkReplayOut,
    DeliveryDetail,
    DeliveryList,
    DeliveryOut,
    DestinationList,
    DestinationOut,
    DryRunDestination,
    DryRunOut,
    EventList,
    EventOut,
    Health,
    Readiness,
    ReplayAuditList,
    ReplayAuditOut,
    ReplayOut,
    RotateIn,
    RotateOut,
    SourceList,
    SourceOut,
    WebhookAccepted,
)
from launchbridge.secrets import (
    UnknownSourceError,
    list_sources,
    rotate_source,
    source_exists,
    source_secrets,
)
from launchbridge.signing import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    verify_signature_any,
)
from launchbridge.stats import collect_stats

router = APIRouter()
log = get_logger("launchbridge.api")
MAX_BODY_BYTES = 1_000_000
RECORDED_HEADERS = ("content-type", "user-agent", "x-event-id", "x-request-id")


def get_registry(request: Request) -> DestinationRegistry:
    return request.app.state.registry


@router.get("/healthz", response_model=Health, tags=["ops"])
def healthz() -> Health:
    return Health(status="ok", version=__version__)


@router.get("/readyz", response_model=Readiness, tags=["ops"])
def readyz(response: Response, session: Session = Depends(get_session)) -> Readiness:
    try:
        ok = session.execute(text("SELECT 1")).scalar() == 1
    except Exception:
        ok = False
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Readiness(status="unavailable", database="down")
    return Readiness(status="ok", database="ok")


@router.get("/metrics", tags=["ops"], include_in_schema=False)
def prometheus_metrics(session: Session = Depends(get_session)) -> Response:
    counts = dict(
        session.execute(select(Delivery.status, func.count()).group_by(Delivery.status)).all()
    )
    for delivery_status in DeliveryStatus:
        metrics.DELIVERIES_BY_STATUS.labels(status=delivery_status.value).set(
            counts.get(delivery_status.value, 0)
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post(
    "/webhooks/{source}",
    response_model=WebhookAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["webhooks"],
    responses={200: {"description": "Duplicate event, not re-dispatched"}},
)
async def receive_webhook(
    source: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    registry: DestinationRegistry = Depends(get_registry),
) -> WebhookAccepted:
    now = utcnow()
    secrets = source_secrets(session, settings, source, now)
    if secrets is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown source {source!r}")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="body too large")

    signature = request.headers.get(SIGNATURE_HEADER)
    try:
        signed_at, key = verify_signature_any(
            secrets,
            request.headers.get(TIMESTAMP_HEADER),
            signature,
            body,
            settings.signature_tolerance_seconds,
            now=now.timestamp(),
        )
    except SignatureError as exc:
        record_rejection(session, source, exc.reason, now)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail={"error": exc.reason, "message": exc.detail}
        ) from exc
    metrics.SIGNATURES_VERIFIED.labels(source=source, key=key).inc()

    headers = {name: value for name, value in request.headers.items() if name in RECORDED_HEADERS}
    if EVENT_ID_HEADER.lower() not in headers and request.headers.get(EVENT_ID_HEADER):
        headers[EVENT_ID_HEADER.lower()] = request.headers[EVENT_ID_HEADER]

    try:
        result = ingest_event(
            session,
            source=source,
            body=body,
            headers=headers,
            signature=signature or "",
            signed_at=signed_at,
            now=now,
            registry=registry,
        )
    except SignatureReplayedError as exc:
        record_rejection(session, source, "replayed_signature", now)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error": "replayed_signature", "message": "signature already accepted"},
        ) from exc

    if result.deduplicated:
        response.status_code = status.HTTP_200_OK
    return WebhookAccepted(
        event_id=result.event_id,
        deduplicated=result.deduplicated,
        delivery_ids=result.delivery_ids,
    )


@router.post("/dry-run/{source}", response_model=DryRunOut, tags=["webhooks"])
async def dry_run(
    source: str,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    registry: DestinationRegistry = Depends(get_registry),
    _actor: str = Depends(require_api_key),
) -> DryRunOut:
    """Show where a sample event would be routed and what each destination would receive.

    Takes the raw event body like `/webhooks/{source}` but needs the admin key instead of a
    signature, and records nothing.
    """
    if not source_exists(session, settings, source):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown source {source!r}")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="body too large")
    payload = parse_payload(body)
    event_key = derive_event_key(payload, body, request.headers.get(EVENT_ID_HEADER))
    context = {
        "event_id": "00000000-0000-0000-0000-000000000000",
        "source": source,
        "event_key": event_key,
        "received_at": utcnow().isoformat(),
    }
    outbound = payload if payload is not None else body.decode("utf-8", errors="replace")
    results = []
    for destination in registry.destinations:
        decision = destination.decide(source, payload, registry.event_type_field)
        results.append(
            DryRunDestination(
                destination=destination.name,
                routed=decision.routed,
                reason=decision.reason,
                url=destination.url if decision.routed else None,
                payload=destination.render_payload(outbound, context) if decision.routed else None,
            )
        )
    return DryRunOut(
        source=source,
        event_key=event_key,
        event_type=event_type_of(payload, registry.event_type_field),
        routed_to=[r.destination for r in results if r.routed],
        destinations=results,
    )


@router.get("/sources", response_model=SourceList, tags=["sources"])
def get_sources(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _actor: str = Depends(require_api_key),
) -> SourceList:
    """Known sources and their rotation state. Secrets are never returned here."""
    items = [SourceOut(**row) for row in list_sources(session, settings, utcnow())]
    return SourceList(items=items, count=len(items))


@router.post("/sources/{source}/rotate", response_model=RotateOut, tags=["sources"])
def rotate_secret(
    source: str,
    body: RotateIn | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    actor: str = Depends(require_api_key),
) -> RotateOut:
    """Issue a new inbound secret; the old one keeps verifying until the overlap ends.

    The new secret is returned once, here. Pass `secret` to choose it and `overlap_seconds`
    to override `LAUNCHBRIDGE_SECRET_OVERLAP_SECONDS`.
    """
    body = body or RotateIn()
    overlap = (
        settings.secret_overlap_seconds if body.overlap_seconds is None else body.overlap_seconds
    )
    now = utcnow()
    try:
        row = rotate_source(
            session, settings, source, now=now, new_secret=body.secret, overlap_seconds=overlap
        )
    except UnknownSourceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown source {source!r}") from exc
    log.info("secret_rotated", source=source, actor=actor, overlap_seconds=overlap)
    return RotateOut(
        source=source,
        secret=row.current_secret,
        rotated_at=row.rotated_at,
        previous_expires_at=row.previous_expires_at,
    )


@router.get("/destinations", response_model=DestinationList, tags=["destinations"])
def list_destinations(
    session: Session = Depends(get_session),
    registry: DestinationRegistry = Depends(get_registry),
    _actor: str = Depends(require_api_key),
) -> DestinationList:
    """Configured destinations with their persisted breaker state and queue depth."""
    states = {row.destination: row for row in session.scalars(select(DestinationState))}
    queued = dict(
        session.execute(
            select(Delivery.destination, func.count())
            .where(Delivery.status == DeliveryStatus.PENDING)
            .group_by(Delivery.destination)
        ).all()
    )
    items = [
        DestinationOut(
            name=d.name,
            url=d.url,
            sources=d.sources,
            event_types=d.event_types,
            predicates=len(d.when),
            transform=not d.transform.is_identity(),
            max_attempts=d.retry.max_attempts,
            rate_limit=d.rate_limit.model_dump() if d.rate_limit else None,
            circuit_breaker=d.circuit_breaker.model_dump() if d.circuit_breaker else None,
            breaker=BreakerOut.model_validate(states[d.name]) if d.name in states else None,
            queued=int(queued.get(d.name, 0)),
        )
        for d in registry.destinations
    ]
    return DestinationList(items=items, count=len(items))


@router.get("/deliveries", response_model=DeliveryList, tags=["deliveries"])
def list_deliveries(
    status_filter: str | None = Query(default=None, alias="status"),
    source: str | None = None,
    destination: str | None = None,
    event_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _actor: str = Depends(require_api_key),
) -> DeliveryList:
    stmt = select(Delivery).join(Event, Event.id == Delivery.event_id)
    if status_filter:
        if status_filter not in DeliveryStatus.__members__.values():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="unknown status")
        stmt = stmt.where(Delivery.status == status_filter)
    if source:
        stmt = stmt.where(Event.source == source)
    if destination:
        stmt = stmt.where(Delivery.destination == destination)
    if event_id:
        stmt = stmt.where(Delivery.event_id == event_id)
    if since is not None:
        stmt = stmt.where(Delivery.created_at >= since)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(Delivery.created_at.desc()).limit(limit).offset(offset))
    return DeliveryList(items=[DeliveryOut.model_validate(d) for d in rows], count=total)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryDetail, tags=["deliveries"])
def get_delivery(
    delivery_id: uuid.UUID,
    session: Session = Depends(get_session),
    _actor: str = Depends(require_api_key),
) -> DeliveryDetail:
    delivery = session.scalar(
        select(Delivery)
        .options(selectinload(Delivery.attempt_log), selectinload(Delivery.event))
        .where(Delivery.id == delivery_id)
    )
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delivery not found")
    out = DeliveryOut.model_validate(delivery).model_dump()
    return DeliveryDetail(**out, source=delivery.event.source, attempt_log=delivery.attempt_log)


@router.post(
    "/deliveries/{delivery_id}/replay",
    response_model=ReplayOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["replay"],
)
def replay_one(
    delivery_id: uuid.UUID,
    reason: str | None = None,
    session: Session = Depends(get_session),
    actor: str = Depends(require_api_key),
) -> ReplayOut:
    delivery = session.get(Delivery, delivery_id, with_for_update=True)
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delivery not found")
    try:
        replacement = replay_delivery(
            session, delivery, actor=actor, mode="single", now=utcnow(), reason=reason
        )
    except ReplayError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    session.commit()
    return ReplayOut(original_delivery_id=delivery.id, replay_delivery_id=replacement.id)


@router.post(
    "/replay", response_model=BulkReplayOut, status_code=status.HTTP_202_ACCEPTED, tags=["replay"]
)
def replay_bulk(
    source: str | None = None,
    since: datetime | None = None,
    destination: str | None = None,
    reason: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_session),
    actor: str = Depends(require_api_key),
) -> BulkReplayOut:
    if not source and since is None and not destination:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="provide at least one of source, since, destination",
        )
    targets = failed_deliveries(
        session, source=source, since=since, destination=destination, limit=limit
    )
    ids = replay_many(session, targets, actor=actor, now=utcnow(), reason=reason)
    return BulkReplayOut(replayed=len(ids), delivery_ids=ids)


@router.get("/replays", response_model=ReplayAuditList, tags=["replay"])
def list_replays(
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    _actor: str = Depends(require_api_key),
) -> ReplayAuditList:
    rows = session.scalars(select(Replay).order_by(Replay.requested_at.desc()).limit(limit)).all()
    total = session.scalar(select(func.count()).select_from(Replay)) or 0
    return ReplayAuditList(items=[ReplayAuditOut.model_validate(r) for r in rows], count=total)


@router.get("/events", response_model=EventList, tags=["events"])
def list_events(
    source: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _actor: str = Depends(require_api_key),
) -> EventList:
    stmt = select(Event)
    if source:
        stmt = stmt.where(Event.source == source)
    if status_filter:
        stmt = stmt.where(Event.status == status_filter)
    if since is not None:
        stmt = stmt.where(Event.received_at >= since)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(Event.received_at.desc()).limit(limit).offset(offset))
    return EventList(items=[EventOut.model_validate(e) for e in rows], count=total)


@router.get("/events/{event_id}", response_model=EventOut, tags=["events"])
def get_event(
    event_id: uuid.UUID,
    session: Session = Depends(get_session),
    _actor: str = Depends(require_api_key),
) -> EventOut:
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="event not found")
    return EventOut.model_validate(event)


@router.get("/stats", tags=["ops"])
def stats(
    source: str | None = None,
    since: datetime | None = None,
    session: Session = Depends(get_session),
    _actor: str = Depends(require_api_key),
) -> dict:
    return collect_stats(session, source=source, since=since)
