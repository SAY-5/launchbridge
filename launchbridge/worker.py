"""Delivery worker: claims due deliveries, posts signed requests, applies the retry policy."""

from __future__ import annotations

import json
import random
import signal
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from launchbridge import metrics
from launchbridge.breaker import BreakerState
from launchbridge.config import Settings, get_settings
from launchbridge.db import get_session_factory, utcnow
from launchbridge.destinations import Destination, DestinationRegistry
from launchbridge.gating import Gate
from launchbridge.logging import configure_logging, get_logger
from launchbridge.models import (
    Delivery,
    DeliveryAttempt,
    DeliveryStatus,
    DestinationState,
    Event,
    ProcessedEvent,
    SignatureNonce,
)
from launchbridge.retry import Outcome, classify
from launchbridge.signing import EVENT_ID_HEADER, IDEMPOTENCY_HEADER, sign_headers

log = get_logger("launchbridge.worker")

CLAIM_SQL = text(
    """
    UPDATE deliveries SET status = 'in_progress', updated_at = :now
    WHERE id IN (
        SELECT id FROM deliveries
        WHERE status = 'pending' AND next_attempt_at <= :now
        ORDER BY next_attempt_at
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
    """
)

STALE_IN_PROGRESS_SECONDS = 300


def envelope_context(event: Event) -> dict:
    return {
        "event_id": str(event.id),
        "source": event.source,
        "event_key": event.event_key,
        "received_at": event.received_at.isoformat(),
    }


def build_envelope(event: Event, delivery: Delivery, destination: Destination) -> bytes:
    """Canonical outbound body; identical across retries and replays of one event.

    The payload is passed through the destination's transform, which is deterministic for a
    given configuration, so retries and replays keep producing the same bytes.
    """
    context = envelope_context(event)
    payload = event.payload if event.payload is not None else event.raw_body
    envelope = {
        **context,
        "destination": delivery.destination,
        "payload": destination.render_payload(payload, context),
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


def outbound_headers(
    destination: Destination, delivery: Delivery, body: bytes, now: datetime
) -> dict:
    headers = sign_headers(
        destination.secret,
        body,
        timestamp=int(now.timestamp()),
        previous_secret=destination.previous_secret,
        key_id=destination.key_id,
    )
    headers[IDEMPOTENCY_HEADER] = delivery.idempotency_key
    headers[EVENT_ID_HEADER] = str(delivery.event_id)
    headers["Content-Type"] = "application/json"
    headers["User-Agent"] = "launchbridge-worker"
    return headers


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: DestinationRegistry,
        http_client: httpx.Client | None = None,
        *,
        now_fn: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        batch_size: int = 50,
        concurrency: int = 8,
        processed_events_ttl: timedelta = timedelta(hours=72),
        nonce_ttl: timedelta = timedelta(seconds=600),
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.http = http_client or httpx.Client()
        self.now_fn = now_fn
        self.rng = rng or random.Random()
        self.batch_size = batch_size
        self.concurrency = concurrency
        self.processed_events_ttl = processed_events_ttl
        self.nonce_ttl = nonce_ttl
        self._stop = False
        self._gates: dict[str, Gate] = {}
        self._gates_lock = threading.Lock()

    def gate_for(self, destination: Destination) -> Gate:
        """One gate per destination; the breaker is seeded from destination_states."""
        with self._gates_lock:
            gate = self._gates.get(destination.name)
            if gate is None:
                gate = Gate(
                    bucket=destination.rate_limit.bucket() if destination.rate_limit else None,
                    breaker=(
                        destination.circuit_breaker.breaker()
                        if destination.circuit_breaker
                        else None
                    ),
                )
                if gate.breaker is not None:
                    self._seed_breaker(destination.name, gate)
                self._gates[destination.name] = gate
                metrics.CIRCUIT_STATE.labels(destination=destination.name).set(
                    metrics.CIRCUIT_STATE_VALUES[gate.state.value]
                )
            return gate

    def _seed_breaker(self, name: str, gate: Gate) -> None:
        assert gate.breaker is not None
        with self.session_factory() as session:
            row = session.get(DestinationState, name)
        if row is None:
            return
        gate.breaker.state = BreakerState(row.breaker_state)
        gate.breaker.consecutive_failures = row.consecutive_failures
        gate.breaker.opened_at = row.opened_at.timestamp() if row.opened_at else None

    def _persist_breaker(self, session: Session, name: str, gate: Gate, now: datetime) -> None:
        assert gate.breaker is not None
        breaker = gate.breaker
        opened_at = datetime.fromtimestamp(breaker.opened_at, tz=UTC) if breaker.opened_at else None
        values = {
            "breaker_state": breaker.state.value,
            "consecutive_failures": breaker.consecutive_failures,
            "opened_at": opened_at,
            "updated_at": now,
        }
        session.execute(
            pg_insert(DestinationState)
            .values(destination=name, **values)
            .on_conflict_do_update(index_elements=["destination"], set_=values)
        )
        metrics.CIRCUIT_STATE.labels(destination=name).set(
            metrics.CIRCUIT_STATE_VALUES[breaker.state.value]
        )

    def _defer(
        self, session: Session, delivery: Delivery, reason: str, wait: float, now: datetime
    ) -> str:
        """Put a claimed delivery back in the queue without spending an attempt."""
        delivery.status = DeliveryStatus.PENDING
        delivery.next_attempt_at = now + timedelta(seconds=wait)
        delivery.updated_at = now
        session.commit()
        metrics.DELIVERIES_DEFERRED.labels(destination=delivery.destination, reason=reason).inc()
        log.info(
            "deferred",
            delivery_id=str(delivery.id),
            destination=delivery.destination,
            reason=reason,
            retry_in_seconds=round(wait, 3),
        )
        return "deferred"

    def claim(self, now: datetime) -> list[uuid.UUID]:
        with self.session_factory() as session:
            rows = (
                session.execute(CLAIM_SQL, {"now": now, "batch": self.batch_size}).scalars().all()
            )
            session.commit()
            return list(rows)

    def process(self, delivery_id: uuid.UUID, now: datetime | None = None) -> str:
        now = now or self.now_fn()
        with self.session_factory() as session:
            delivery = session.get(Delivery, delivery_id)
            if delivery is None:
                return "missing"
            event = session.get(Event, delivery.event_id)
            destination = self.registry.get(delivery.destination)
            if event is None or destination is None:
                delivery.status = DeliveryStatus.FAILED
                delivery.last_error = "unknown destination" if event else "event missing"
                delivery.updated_at = now
                session.commit()
                metrics.DELIVERIES_FAILED.labels(destination=delivery.destination).inc()
                return delivery.status

            gate = self.gate_for(destination)
            reason, wait = gate.check(now.timestamp())
            if reason is not None:
                if gate.breaker is not None:
                    self._persist_breaker(session, destination.name, gate, now)
                return self._defer(session, delivery, reason, wait, now)

            attempt_number = delivery.attempts + 1
            body = build_envelope(event, delivery, destination)
            headers = outbound_headers(destination, delivery, body, utcnow())
            started = time.perf_counter()
            status_code: int | None = None
            error: str | None = None
            try:
                response = self.http.post(
                    destination.url,
                    content=body,
                    headers=headers,
                    timeout=destination.retry.timeout_seconds,
                )
                status_code = response.status_code
                if not 200 <= status_code < 300:
                    error = f"HTTP {status_code}: {response.text[:200]}"
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
            duration_ms = int((time.perf_counter() - started) * 1000)
            metrics.ATTEMPT_DURATION.labels(destination=destination.name).observe(
                duration_ms / 1000
            )

            outcome = classify(status_code)
            transition = gate.record(outcome, now.timestamp())
            if transition is not None:
                metrics.CIRCUIT_TRANSITIONS.labels(
                    destination=destination.name, state=transition.value
                ).inc()
                log.warning("circuit", destination=destination.name, state=transition.value)
            if gate.breaker is not None:
                self._persist_breaker(session, destination.name, gate, now)
            session.add(
                DeliveryAttempt(
                    delivery_id=delivery.id,
                    attempt_number=attempt_number,
                    started_at=now,
                    duration_ms=duration_ms,
                    status_code=status_code,
                    outcome=outcome.value,
                    error=error,
                    succeeded=outcome is Outcome.SUCCESS,
                )
            )
            delivery.attempts = attempt_number
            delivery.last_status_code = status_code
            delivery.last_error = error
            delivery.updated_at = now
            policy = destination.policy

            if outcome is Outcome.SUCCESS:
                delivery.status = DeliveryStatus.DELIVERED
                delivery.delivered_at = now
                delivery.next_attempt_at = None
                latency = (now - delivery.created_at).total_seconds()
                delivery.latency_ms = max(int(latency * 1000), 0)
                metrics.DELIVERIES_DELIVERED.labels(destination=destination.name).inc()
                metrics.DELIVERY_LATENCY.labels(destination=destination.name).observe(
                    max(latency, 0)
                )
            elif outcome is Outcome.TRANSIENT and attempt_number < delivery.max_attempts:
                delay = policy.backoff(attempt_number, self.rng)
                delivery.status = DeliveryStatus.PENDING
                delivery.next_attempt_at = now + timedelta(seconds=delay)
                metrics.DELIVERIES_RETRIED.labels(destination=destination.name).inc()
            else:
                delivery.status = DeliveryStatus.FAILED
                delivery.next_attempt_at = None
                if outcome is Outcome.TRANSIENT:
                    delivery.last_error = (
                        f"{error}; retries exhausted after {attempt_number} attempts"
                    )
                metrics.DELIVERIES_FAILED.labels(destination=destination.name).inc()
            session.commit()
            log.info(
                "attempt",
                delivery_id=str(delivery.id),
                destination=destination.name,
                attempt=attempt_number,
                status_code=status_code,
                outcome=outcome.value,
                state=delivery.status,
            )
            return delivery.status

    def run_once(self, now: datetime | None = None) -> int:
        """Claim and process one batch; returns the number of deliveries processed."""
        now = now or self.now_fn()
        ids = self.claim(now)
        if not ids:
            return 0
        if self.concurrency <= 1 or len(ids) == 1:
            for delivery_id in ids:
                self.process(delivery_id, now)
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                list(pool.map(lambda i: self.process(i, now), ids))
        return len(ids)

    def drain(self, now: datetime | None = None, max_batches: int = 100) -> int:
        """Process batches until nothing is due at `now`."""
        total = 0
        for _ in range(max_batches):
            processed = self.run_once(now)
            if processed == 0:
                break
            total += processed
        return total

    def release_stale(self, now: datetime) -> int:
        """Return deliveries stuck in_progress (a crashed worker) to the queue."""
        cutoff = now - timedelta(seconds=STALE_IN_PROGRESS_SECONDS)
        with self.session_factory() as session:
            result = session.execute(
                update(Delivery)
                .where(Delivery.status == DeliveryStatus.IN_PROGRESS, Delivery.updated_at < cutoff)
                .values(status=DeliveryStatus.PENDING, next_attempt_at=now, updated_at=now)
            )
            session.commit()
            return result.rowcount or 0

    def cleanup_processed_events(self, now: datetime) -> int:
        cutoff = now - self.processed_events_ttl
        with self.session_factory() as session:
            result = session.execute(
                delete(ProcessedEvent).where(ProcessedEvent.processed_at < cutoff)
            )
            session.commit()
            return result.rowcount or 0

    def cleanup_nonces(self, now: datetime) -> int:
        """Drop nonces older than the TTL; their timestamps are stale by then anyway."""
        cutoff = now - self.nonce_ttl
        with self.session_factory() as session:
            result = session.execute(delete(SignatureNonce).where(SignatureNonce.seen_at < cutoff))
            session.commit()
            return result.rowcount or 0

    def stop(self) -> None:
        self._stop = True

    def run_forever(self, poll_interval: float = 0.5, maintenance_interval: float = 60.0) -> None:
        last_maintenance = 0.0
        while not self._stop:
            now = self.now_fn()
            if time.monotonic() - last_maintenance > maintenance_interval:
                released = self.release_stale(now)
                removed = self.cleanup_processed_events(now)
                nonces = self.cleanup_nonces(now)
                if released or removed or nonces:
                    log.info(
                        "maintenance",
                        released=released,
                        ledger_rows_removed=removed,
                        nonces_removed=nonces,
                    )
                last_maintenance = time.monotonic()
            try:
                processed = self.run_once(now)
            except Exception:
                log.exception("batch_failed")
                processed = 0
            if processed == 0:
                time.sleep(poll_interval)


def pending_count(session: Session) -> int:
    return (
        session.scalar(select(Delivery.id).where(Delivery.status == DeliveryStatus.PENDING).count())
        or 0
    )


def main(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    registry = DestinationRegistry.load(settings.destinations_file)
    if settings.worker_metrics_port:
        from prometheus_client import start_http_server

        start_http_server(settings.worker_metrics_port)
    worker = Worker(
        get_session_factory(),
        registry,
        batch_size=settings.worker_batch_size,
        concurrency=settings.worker_concurrency,
        processed_events_ttl=timedelta(hours=settings.processed_events_ttl_hours),
        nonce_ttl=timedelta(seconds=settings.signature_tolerance_seconds * 2),
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop())
    log.info("worker_started", destinations=[d.name for d in registry.destinations])
    worker.run_forever(poll_interval=settings.worker_poll_interval_seconds)
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
