import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from launchbridge.models import Delivery, Event, ProcessedEvent, SignatureRejection
from launchbridge.signing import sign_headers
from tests.conftest import SOURCE_SECRETS, new_payload, signed_post


def test_accepted_event_fans_out_to_matching_destinations(client, session_factory):
    response = signed_post(client, "orders", new_payload())
    assert response.status_code == 202
    body = response.json()
    assert body["deduplicated"] is False
    assert len(body["delivery_ids"]) == 2
    with session_factory() as session:
        event = session.get(Event, uuid.UUID(body["event_id"]))
        assert event.status == "accepted"
        assert event.payload["type"] == "order.created"
        destinations = {
            d.destination
            for d in session.scalars(select(Delivery).where(Delivery.event_id == event.id))
        }
        assert destinations == {"crm", "billing"}
        for delivery in session.scalars(select(Delivery)):
            assert delivery.status == "pending"
            assert delivery.idempotency_key == f"{event.id}:{delivery.destination}"
            assert delivery.max_attempts in (4, 3)


def test_source_without_billing_route_only_hits_crm(client):
    response = signed_post(client, "crm-source", new_payload())
    assert response.status_code == 202
    assert len(response.json()["delivery_ids"]) == 1


def test_duplicate_event_returns_200_and_is_not_redispatched(client, session_factory):
    payload = new_payload()
    first = signed_post(client, "orders", payload)
    time.sleep(1.1)  # a fresh timestamp gives a different signature, as a retrying sender would
    second = signed_post(client, "orders", payload)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {
        "event_id": second.json()["event_id"],
        "deduplicated": True,
        "delivery_ids": [],
    }
    assert second.json()["event_id"] != first.json()["event_id"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Delivery)) == 2
        statuses = sorted(session.scalars(select(Event.status)))
        assert statuses == ["accepted", "deduplicated"]
        assert session.scalar(select(func.count()).select_from(ProcessedEvent)) == 1


def test_duplicate_with_different_timestamp_is_dedup_not_replay(client):
    payload = new_payload()
    now = int(time.time())
    assert signed_post(client, "orders", payload, timestamp=now - 10).status_code == 202
    assert signed_post(client, "orders", payload, timestamp=now - 5).status_code == 200


def test_replayed_signature_is_rejected(client, session_factory):
    payload = new_payload()
    body = json.dumps(payload).encode()
    headers = sign_headers(SOURCE_SECRETS["orders"], body)
    assert client.post("/webhooks/orders", content=body, headers=headers).status_code == 202
    replay = client.post("/webhooks/orders", content=body, headers=headers)
    assert replay.status_code == 409
    assert replay.json()["detail"]["error"] == "replayed_signature"
    with session_factory() as session:
        rejection = session.scalars(select(SignatureRejection)).one()
        assert rejection.reason == "replayed_signature"
        assert session.scalar(select(func.count()).select_from(Event)) == 1


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"secret": "wrong"}, "invalid_signature"),
        ({"timestamp": int(time.time()) - 900}, "stale_timestamp"),
    ],
)
def test_bad_signatures_are_rejected_and_recorded(client, session_factory, kwargs, reason):
    response = signed_post(client, "orders", new_payload(), **kwargs)
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == reason
    with session_factory() as session:
        assert session.scalar(select(SignatureRejection.reason)) == reason
        assert session.scalar(select(func.count()).select_from(Event)) == 0


def test_missing_headers_are_rejected(client):
    response = client.post("/webhooks/orders", content=b"{}")
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "missing_timestamp"


def test_unknown_source_is_404(client):
    assert signed_post(client, "nobody", new_payload(), secret="x").status_code == 404


def test_event_key_prefers_header_then_id_then_hash(client, session_factory):
    payload = new_payload()
    signed_post(client, "orders", payload, extra_headers={"X-Event-Id": "hdr-1"})
    signed_post(client, "orders", {"id": "pl-1"})
    signed_post(client, "orders", {"no": "id"})
    signed_post(client, "orders", {"no": "id"}, timestamp=int(time.time()) - 1)
    with session_factory() as session:
        keys = list(session.scalars(select(ProcessedEvent.event_key)))
    assert "id:hdr-1" in keys
    assert "id:pl-1" in keys
    assert len([k for k in keys if k.startswith("hash:")]) == 1
    assert len(keys) == 3


def test_unique_constraint_is_enforced_by_the_database(session_factory):
    now = datetime.now(UTC)
    with session_factory() as session:
        event = Event(
            source="orders",
            event_key="id:x",
            content_hash="h",
            signature="sha256=a",
            signed_at=1,
            raw_body="{}",
            headers={},
            status="accepted",
            received_at=now,
        )
        session.add(event)
        session.flush()
        session.add(
            ProcessedEvent(
                source="orders",
                event_key="id:x",
                signature="sha256=a",
                event_id=event.id,
                processed_at=now,
            )
        )
        session.commit()
        session.add(
            ProcessedEvent(
                source="orders",
                event_key="id:x",
                signature="sha256=b",
                event_id=event.id,
                processed_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_ledger_ttl_cleanup_expires_old_keys(client, session_factory, worker):
    payload = new_payload()
    assert signed_post(client, "orders", payload).status_code == 202
    assert worker.cleanup_processed_events(datetime.now(UTC)) == 0
    assert worker.cleanup_processed_events(datetime.now(UTC) + timedelta(hours=73)) == 1
    time.sleep(1.1)
    assert signed_post(client, "orders", payload).status_code == 202


def test_oversized_body_is_rejected(client):
    body = b"x" * 1_000_001
    headers = sign_headers(SOURCE_SECRETS["orders"], body)
    assert client.post("/webhooks/orders", content=body, headers=headers).status_code == 413
