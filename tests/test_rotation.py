import json
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from launchbridge.destinations import DestinationRegistry
from launchbridge.models import SignatureNonce, SourceSecret
from launchbridge.signing import sign_headers
from launchbridge.worker import Worker
from tests.conftest import SOURCE_SECRETS, new_payload, signed_post


def _rotate(client, admin, source="orders", **body):
    return client.post(f"/sources/{source}/rotate", headers=admin, json=body or None)


def _verified(client, key: str) -> float:
    """Process-wide counter, so tests compare deltas rather than absolute values."""
    series = f'launchbridge_signatures_verified_total{{key="{key}",source="orders"}} '
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(series):
            return float(line.split()[-1])
    return 0.0


def test_both_secrets_verify_during_the_overlap(client, admin, session_factory):
    old = SOURCE_SECRETS["orders"]
    rotated = _rotate(client, admin, overlap_seconds=3600)
    assert rotated.status_code == 200
    new = rotated.json()["secret"]
    assert new != old and len(new) >= 32
    assert rotated.json()["previous_expires_at"] > rotated.json()["rotated_at"]

    before = _verified(client, "current"), _verified(client, "previous")
    assert signed_post(client, "orders", new_payload(), secret=old).status_code == 202
    assert signed_post(client, "orders", new_payload(), secret=new).status_code == 202
    assert signed_post(client, "orders", new_payload(), secret="neither").status_code == 401
    with session_factory() as session:
        row = session.get(SourceSecret, "orders")
    assert row.current_secret == new and row.previous_secret == old
    after = _verified(client, "current"), _verified(client, "previous")
    assert after == (before[0] + 1, before[1] + 1)


def test_previous_secret_is_rejected_after_the_overlap(client, admin):
    old = SOURCE_SECRETS["orders"]
    new = _rotate(client, admin, overlap_seconds=1).json()["secret"]
    assert signed_post(client, "orders", new_payload(), secret=old).status_code == 202
    time.sleep(1.1)
    rejected = signed_post(client, "orders", new_payload(), secret=old)
    assert rejected.status_code == 401
    assert rejected.json()["detail"]["error"] == "invalid_signature"
    assert signed_post(client, "orders", new_payload(), secret=new).status_code == 202
    listing = client.get("/sources", headers=admin).json()
    orders = next(s for s in listing["items"] if s["source"] == "orders")
    assert orders["secret_from"] == "database" and orders["previous_active"] is False


def test_rotating_twice_keeps_only_two_secrets_live(client, admin):
    first = _rotate(client, admin, secret="first-rotation-secret-value").json()["secret"]
    second = _rotate(client, admin, secret="second-rotation-secret-value").json()["secret"]
    assert (first, second) == ("first-rotation-secret-value", "second-rotation-secret-value")
    assert signed_post(client, "orders", new_payload(), secret=second).status_code == 202
    assert signed_post(client, "orders", new_payload(), secret=first).status_code == 202
    original = signed_post(client, "orders", new_payload(), secret=SOURCE_SECRETS["orders"])
    assert original.status_code == 401


def test_rotation_endpoint_validation(client, admin):
    assert _rotate(client, admin, source="nobody").status_code == 404
    assert client.post("/sources/orders/rotate").status_code == 401
    assert _rotate(client, admin, secret="short").status_code == 422
    assert _rotate(client, admin, overlap_seconds=-1).status_code == 422
    listing = client.get("/sources", headers=admin).json()
    assert {s["source"]: s["secret_from"] for s in listing["items"]} == {
        "crm-source": "environment",
        "orders": "environment",
    }
    assert SOURCE_SECRETS["orders"] not in json.dumps(listing), "secrets are never listed"


def test_replayed_request_of_a_deduplicated_event_is_rejected(client, session_factory):
    payload = new_payload()
    assert signed_post(client, "orders", payload).status_code == 202
    body = json.dumps(payload).encode()
    headers = sign_headers(SOURCE_SECRETS["orders"], body, int(time.time()) - 5)
    duplicate = client.post("/webhooks/orders", content=body, headers=headers)
    assert duplicate.status_code == 200 and duplicate.json()["deduplicated"] is True
    replay = client.post("/webhooks/orders", content=body, headers=headers)
    assert replay.status_code == 409
    assert replay.json()["detail"]["error"] == "replayed_signature"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SignatureNonce)) == 2


def test_nonce_cleanup_respects_ttl(client, session_factory, registry, http_client):
    worker = Worker(session_factory, registry, http_client, nonce_ttl=timedelta(seconds=600))
    signed_post(client, "orders", new_payload())
    now = datetime.now(UTC)
    assert worker.cleanup_nonces(now + timedelta(seconds=599)) == 0
    assert worker.cleanup_nonces(now + timedelta(seconds=601)) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SignatureNonce)) == 0


def test_outbound_rotation_signs_with_both_keys(client, session_factory, http_client, receiver):
    registry = DestinationRegistry.from_yaml(
        """
destinations:
  - name: crm
    url: http://receiver/hooks/crm
    secret: crm-secret-v2
    previous_secret: crm-secret
    key_id: v2
  - name: billing
    url: http://receiver/hooks/billing
    secret: billing-secret
    key_id: v1
"""
    )
    worker = Worker(session_factory, registry, http_client, concurrency=1)
    response = signed_post(client, "orders", new_payload())
    worker.drain()
    event_id = response.json()["event_id"]
    crm = receiver.get(f"/inbox/{event_id}:crm").json()
    assert crm["signature_valid"] is True
    assert crm["signature_header"] == "X-Signature-Previous", "receiver still holds the old key"
    assert crm["key_id"] == "v2"
    billing = receiver.get(f"/inbox/{event_id}:billing").json()
    assert billing["signature_valid"] is True
    assert billing["signature_header"] == "X-Signature" and billing["key_id"] == "v1"
