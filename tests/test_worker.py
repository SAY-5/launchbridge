import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from launchbridge.models import Delivery, DeliveryAttempt
from launchbridge.signing import compute_signature
from launchbridge.worker import Worker
from tests.conftest import new_payload, signed_post


def _deliveries(session_factory, destination="crm"):
    with session_factory() as session:
        return list(
            session.scalars(
                select(Delivery)
                .where(Delivery.destination == destination)
                .order_by(Delivery.created_at)
            )
        )


def _attempts(session_factory, delivery_id):
    with session_factory() as session:
        return list(
            session.scalars(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt_number)
            )
        )


def test_successful_delivery_records_latency_and_receiver_sees_valid_signature(
    client, worker, receiver, session_factory
):
    signed_post(client, "crm-source", new_payload())
    assert worker.run_once() == 1
    (delivery,) = _deliveries(session_factory)
    assert delivery.status == "delivered"
    assert delivery.attempts == 1
    assert delivery.latency_ms is not None and delivery.latency_ms >= 0
    entry = receiver.get(f"/inbox/{delivery.idempotency_key}").json()
    assert entry["signature_valid"] is True
    assert entry["count"] == 1
    assert entry["event_id"] == str(delivery.event_id)


def test_transient_failures_retry_with_backoff_then_succeed(
    client, worker, receiver, session_factory
):
    receiver.post("/control/rules", json={"tag": "flaky", "status": 503, "times": 2})
    signed_post(client, "crm-source", new_payload(tag="flaky"))
    start = datetime.now(UTC)

    assert worker.run_once(start) == 1
    (delivery,) = _deliveries(session_factory)
    assert delivery.status == "pending"
    assert delivery.attempts == 1
    assert delivery.last_status_code == 503
    delay = (delivery.next_attempt_at - start).total_seconds()
    assert 0.4 <= delay <= 0.6

    assert worker.run_once(start) == 0, "not due yet"
    assert worker.run_once(start + timedelta(seconds=1)) == 1
    (delivery,) = _deliveries(session_factory)
    assert delivery.status == "pending" and delivery.attempts == 2
    delay = (delivery.next_attempt_at - (start + timedelta(seconds=1))).total_seconds()
    assert 0.8 <= delay <= 1.2

    assert worker.drain(start + timedelta(seconds=10)) == 1
    (delivery,) = _deliveries(session_factory)
    assert delivery.status == "delivered"
    assert delivery.attempts == 3
    assert delivery.last_error is None
    outcomes = [a.outcome for a in _attempts(session_factory, delivery.id)]
    assert outcomes == ["transient", "transient", "success"]
    assert receiver.get(f"/inbox/{delivery.idempotency_key}").json()["count"] == 3


def test_permanent_failure_is_terminal_after_one_attempt(client, worker, receiver, session_factory):
    receiver.post("/control/rules", json={"tag": "broken", "status": 400})
    signed_post(client, "crm-source", new_payload(tag="broken"))
    worker.drain(datetime.now(UTC) + timedelta(hours=1))
    (delivery,) = _deliveries(session_factory)
    assert delivery.status == "failed"
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is None
    assert delivery.last_error.startswith("HTTP 400")


def test_transient_failures_are_bounded_by_max_attempts(client, worker, receiver, session_factory):
    receiver.post("/control/rules", json={"tag": "down", "status": 503})
    signed_post(client, "crm-source", new_payload(tag="down"))
    start = datetime.now(UTC)
    total = sum(worker.run_once(start + timedelta(minutes=step)) for step in range(10))
    (delivery,) = _deliveries(session_factory)
    assert total == 4, "no attempts beyond max_attempts even with the clock advancing"
    assert delivery.status == "failed"
    assert delivery.attempts == delivery.max_attempts == 4
    assert "retries exhausted after 4 attempts" in delivery.last_error
    assert len(_attempts(session_factory, delivery.id)) == 4
    assert receiver.get(f"/inbox/{delivery.idempotency_key}").json()["count"] == 4


def test_transport_errors_are_transient(client, session_factory, registry):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    worker = Worker(
        session_factory, registry, httpx.Client(transport=httpx.MockTransport(boom)), concurrency=1
    )
    signed_post(client, "crm-source", new_payload())
    worker.run_once()
    (delivery,) = _deliveries(session_factory)
    assert delivery.status == "pending"
    assert delivery.last_status_code is None
    assert delivery.last_error.startswith("ConnectError")


def test_outbound_requests_are_signed_and_carry_idempotency_key(client, session_factory, registry):
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    worker = Worker(
        session_factory,
        registry,
        httpx.Client(transport=httpx.MockTransport(capture)),
        concurrency=1,
    )
    response = signed_post(client, "crm-source", new_payload())
    worker.run_once()
    (request,) = seen
    (delivery,) = _deliveries(session_factory)
    assert request.headers["X-Idempotency-Key"] == delivery.idempotency_key
    assert request.headers["X-Event-Id"] == response.json()["event_id"]
    expected = compute_signature("crm-secret", request.headers["X-Timestamp"], request.content)
    assert request.headers["X-Signature"] == expected
    envelope = json.loads(request.content)
    assert envelope["source"] == "crm-source"
    assert envelope["payload"]["type"] == "order.created"


def test_unknown_destination_fails_fast(client, worker, session_factory):
    signed_post(client, "orders", new_payload())
    worker.registry.destinations = [d for d in worker.registry.destinations if d.name == "crm"]
    worker.drain(datetime.now(UTC))
    (billing,) = _deliveries(session_factory, "billing")
    assert billing.status == "failed"
    assert billing.last_error == "unknown destination"


def test_claim_marks_in_progress_and_stale_rows_are_released(client, worker, session_factory):
    signed_post(client, "crm-source", new_payload())
    now = datetime.now(UTC)
    ids = worker.claim(now)
    assert len(ids) == 1
    assert worker.claim(now) == []
    (delivery,) = _deliveries(session_factory)
    assert delivery.status == "in_progress"
    assert worker.release_stale(now + timedelta(seconds=301)) == 1
    (delivery,) = _deliveries(session_factory)
    assert delivery.status == "pending"


def test_concurrent_batch_processing(client, session_factory, registry, http_client):
    worker = Worker(session_factory, registry, http_client, concurrency=4, batch_size=10)
    for _ in range(6):
        signed_post(client, "crm-source", new_payload())
    assert worker.run_once() == 6
    assert all(d.status == "delivered" for d in _deliveries(session_factory))


def test_two_workers_draining_one_queue_deliver_every_row_once(
    client, session_factory, registry, http_client, receiver
):
    """Several worker replicas can share one database without double delivery.

    The claim is `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING id`,
    so two workers take disjoint batches. Each delivery therefore ends with exactly one
    attempt and the destination sees each idempotency key once.
    """
    for _ in range(8):
        signed_post(client, "crm-source", new_payload())
    workers = [
        Worker(session_factory, registry, http_client, concurrency=4, batch_size=3)
        for _ in range(2)
    ]
    barrier = threading.Barrier(len(workers))
    lock = threading.Lock()
    processed: list[int] = []
    failures: list[Exception] = []

    def drain(worker: Worker) -> None:
        try:
            barrier.wait(timeout=20)
            count = worker.drain(datetime.now(UTC) + timedelta(hours=1))
            with lock:
                processed.append(count)
        except Exception as exc:
            failures.append(exc)
            barrier.abort()

    threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not failures, failures
    assert sum(processed) == 8, "no delivery was claimed twice"

    deliveries = _deliveries(session_factory)
    assert len(deliveries) == 8
    assert all(d.status == "delivered" for d in deliveries)
    assert all(d.attempts == 1 for d in deliveries)
    for delivery in deliveries:
        assert receiver.get(f"/inbox/{delivery.idempotency_key}").json()["count"] == 1
