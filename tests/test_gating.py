from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from launchbridge.destinations import DestinationRegistry
from launchbridge.models import Delivery, DestinationState
from launchbridge.worker import Worker
from tests.conftest import new_payload, signed_post

CRM = """
destinations:
  - name: crm
    url: http://receiver/hooks/crm
    secret: crm-secret
    retry: {max_attempts: 4, base_delay_seconds: 0.5, max_delay_seconds: 8, jitter: 0}
"""
RATE_LIMITED = CRM + "    rate_limit: {rate: 1, burst: 2}\n"
BREAKER = (
    CRM + "    circuit_breaker: {failure_threshold: 2, recovery_seconds: 10, half_open_max: 1}\n"
)
GATED = RATE_LIMITED + "    circuit_breaker: {failure_threshold: 2, recovery_seconds: 10}\n"


@pytest.fixture
def registry() -> DestinationRegistry:
    return DestinationRegistry.from_yaml(GATED)


def _worker(session_factory, http_client, yaml: str) -> Worker:
    """batch_size=1 so deliveries are processed in next_attempt_at order."""
    registry = DestinationRegistry.from_yaml(yaml)
    return Worker(session_factory, registry, http_client, concurrency=1, batch_size=1)


def _rows(session_factory) -> list[Delivery]:
    with session_factory() as session:
        return list(session.scalars(select(Delivery).order_by(Delivery.created_at)))


def _state(session_factory) -> DestinationState | None:
    with session_factory() as session:
        return session.get(DestinationState, "crm")


def _close_to(actual: datetime, expected: datetime) -> bool:
    return abs((actual - expected).total_seconds()) < 0.001


def _requests_seen(receiver) -> int:
    return sum(entry["count"] for entry in receiver.get("/inbox").json()["keys"])


def test_rate_limit_defers_the_overflow_without_spending_attempts(
    client, session_factory, http_client, receiver
):
    worker = _worker(session_factory, http_client, RATE_LIMITED)
    for _ in range(5):
        signed_post(client, "crm-source", new_payload())
    start = datetime.now(UTC)

    assert worker.drain(start) == 5, "all five were claimed"
    rows = _rows(session_factory)
    assert [r.status for r in rows] == ["delivered", "delivered", "pending", "pending", "pending"]
    deferred = rows[2:]
    assert all(r.attempts == 0 and r.last_status_code is None for r in deferred)
    assert all(_close_to(r.next_attempt_at, start + timedelta(seconds=1)) for r in deferred)
    assert _requests_seen(receiver) == 2

    assert worker.run_once(start) == 0, "deferred rows are not due yet"
    assert worker.drain(start + timedelta(seconds=1)) == 3, "one token, two deferred again"
    assert [r.status for r in _rows(session_factory)].count("delivered") == 3
    assert worker.drain(start + timedelta(seconds=3)) == 2
    assert all(r.status == "delivered" and r.attempts == 1 for r in _rows(session_factory))
    assert _state(session_factory) is None, "no breaker configured, nothing persisted"


def test_open_circuit_queues_deliveries_and_drains_after_recovery(
    client, session_factory, http_client, receiver
):
    worker = _worker(session_factory, http_client, BREAKER)
    receiver.post("/control/rules", json={"tag": "down", "status": 503})
    for _ in range(2):
        signed_post(client, "crm-source", new_payload(tag="down"))
    for _ in range(3):
        signed_post(client, "crm-source", new_payload())
    t0 = datetime.now(UTC)

    assert worker.run_once(t0) == 1
    assert worker.run_once(t0) == 1, "second transient failure trips the breaker"
    state = _state(session_factory)
    assert state.breaker_state == "open" and state.consecutive_failures == 2
    assert _close_to(state.opened_at, t0)

    t_open = t0 + timedelta(seconds=1)
    assert worker.drain(t_open) == 5, "retries and fresh deliveries are all deferred"
    rows = _rows(session_factory)
    assert all(r.status == "pending" for r in rows), "queued, not failed"
    assert [r.attempts for r in rows] == [1, 1, 0, 0, 0]
    assert all(_close_to(r.next_attempt_at, t0 + timedelta(seconds=10)) for r in rows)
    assert _requests_seen(receiver) == 2, "nothing was sent while open"
    assert worker.run_once(t0 + timedelta(seconds=5)) == 0

    receiver.delete("/control/rules")
    t1 = t0 + timedelta(seconds=10)
    assert worker.run_once(t1) == 1, "one half-open probe"
    assert _state(session_factory).breaker_state == "closed"
    assert worker.drain(t1) == 4
    rows = _rows(session_factory)
    assert all(r.status == "delivered" for r in rows)
    assert sorted(r.attempts for r in rows) == [1, 1, 1, 2, 2]
    assert _state(session_factory).consecutive_failures == 0


def test_failed_probe_reopens_and_state_is_reloaded_by_a_new_worker(
    client, session_factory, http_client, receiver
):
    worker = _worker(session_factory, http_client, BREAKER)
    receiver.post("/control/rules", json={"tag": "down", "status": 503})
    signed_post(client, "crm-source", new_payload(tag="down"))
    signed_post(client, "crm-source", new_payload(tag="down"))
    t0 = datetime.now(UTC)
    worker.run_once(t0)
    worker.run_once(t0)
    assert _state(session_factory).breaker_state == "open"

    t1 = t0 + timedelta(seconds=10)
    assert worker.run_once(t1) == 1, "the probe goes out and fails"
    state = _state(session_factory)
    assert state.breaker_state == "open" and _close_to(state.opened_at, t1)

    fresh = _worker(session_factory, http_client, BREAKER)
    gate = fresh.gate_for(fresh.registry.get("crm"))
    assert gate.breaker.state == "open"
    assert gate.breaker.opened_at == pytest.approx(t1.timestamp(), abs=0.001)
    assert fresh.drain(t1 + timedelta(seconds=5)) == 2, "both due, both deferred"
    assert all(r.status == "pending" for r in _rows(session_factory))
    assert _requests_seen(receiver) == 3


def test_destinations_endpoint_reports_breaker_and_queue(
    client, session_factory, http_client, receiver, admin
):
    worker = _worker(session_factory, http_client, BREAKER)
    receiver.post("/control/rules", json={"tag": "down", "status": 503})
    signed_post(client, "crm-source", new_payload(tag="down"))
    signed_post(client, "crm-source", new_payload(tag="down"))
    signed_post(client, "crm-source", new_payload())
    t0 = datetime.now(UTC)
    worker.drain(t0)

    listing = client.get("/destinations", headers=admin).json()
    assert listing["count"] == 1
    (crm,) = listing["items"]
    assert crm["rate_limit"] == {"rate": 1.0, "burst": 2}
    assert crm["circuit_breaker"]["failure_threshold"] == 2
    assert crm["breaker"]["breaker_state"] == "open"
    assert crm["breaker"]["consecutive_failures"] == 2
    assert crm["queued"] == 3
    text = client.get("/metrics").text
    assert 'launchbridge_circuit_state{destination="crm"} 2.0' in text
    assert 'launchbridge_deliveries_deferred_total{destination="crm",reason="circuit_open"}' in text
    assert client.get("/destinations").status_code == 401
