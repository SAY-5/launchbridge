from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from launchbridge.models import Delivery, Replay
from tests.conftest import new_payload, signed_post


def _fail_one(client, worker, receiver, tag="broken"):
    receiver.post("/control/rules", json={"tag": tag, "status": 400})
    response = signed_post(client, "crm-source", new_payload(tag=tag))
    worker.drain(datetime.now(UTC) + timedelta(hours=1))
    return response.json()["delivery_ids"][0]


def test_single_replay_creates_new_series_with_same_idempotency_key(
    client, worker, receiver, session_factory, admin
):
    failed_id = _fail_one(client, worker, receiver)
    listing = client.get("/deliveries", params={"status": "failed"}, headers=admin).json()
    assert listing["count"] == 1 and listing["items"][0]["id"] == failed_id

    receiver.delete("/control/rules")
    replayed = client.post(
        f"/deliveries/{failed_id}/replay", headers=admin, params={"reason": "fixed"}
    )
    assert replayed.status_code == 202
    new_id = replayed.json()["replay_delivery_id"]

    with session_factory() as session:
        original = session.get(Delivery, failed_id)
        replacement = session.get(Delivery, new_id)
        audit = session.scalars(select(Replay)).one()
    assert original.status == "replayed" and original.replayed_at is not None
    assert replacement.status == "pending"
    assert replacement.series == 2 and replacement.attempts == 0
    assert replacement.idempotency_key == original.idempotency_key
    assert str(replacement.replay_of) == failed_id
    assert audit.actor == "ops" and audit.mode == "single" and audit.reason == "fixed"

    worker.drain(datetime.now(UTC) + timedelta(hours=1))
    detail = client.get(f"/deliveries/{new_id}", headers=admin).json()
    assert detail["status"] == "delivered"
    assert detail["source"] == "crm-source"
    assert [a["outcome"] for a in detail["attempt_log"]] == ["success"]
    inbox = receiver.get(f"/inbox/{original.idempotency_key}").json()
    assert inbox["count"] == 2, "the destination saw the same key twice"

    assert (
        client.get("/deliveries", params={"status": "failed"}, headers=admin).json()["count"] == 0
    )
    replays = client.get("/replays", headers=admin).json()
    assert replays["count"] == 1 and replays["items"][0]["new_delivery_id"] == new_id


def test_only_failed_deliveries_can_be_replayed(client, worker, receiver, admin):
    response = signed_post(client, "crm-source", new_payload())
    delivery_id = response.json()["delivery_ids"][0]
    assert client.post(f"/deliveries/{delivery_id}/replay", headers=admin).status_code == 409
    worker.drain()
    assert client.post(f"/deliveries/{delivery_id}/replay", headers=admin).status_code == 409
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/deliveries/{missing}/replay", headers=admin).status_code == 404


def test_bulk_replay_by_source_and_since(client, worker, receiver, session_factory, admin):
    since = datetime.now(UTC) - timedelta(seconds=5)
    receiver.post("/control/rules", json={"tag": "broken", "status": 400})
    for _ in range(3):
        signed_post(client, "crm-source", new_payload(tag="broken"))
    signed_post(client, "orders", new_payload(tag="broken"))
    worker.drain(datetime.now(UTC) + timedelta(hours=1))
    assert (
        client.get("/deliveries", params={"status": "failed"}, headers=admin).json()["count"] == 5
    )

    receiver.delete("/control/rules")
    response = client.post(
        "/replay", params={"source": "crm-source", "since": since.isoformat()}, headers=admin
    )
    assert response.status_code == 202
    assert response.json()["replayed"] == 3
    worker.drain(datetime.now(UTC) + timedelta(hours=1))

    remaining = client.get("/deliveries", params={"status": "failed"}, headers=admin).json()
    assert remaining["count"] == 2
    assert {d["destination"] for d in remaining["items"]} == {"crm", "billing"}
    with session_factory() as session:
        assert session.scalar(select(Replay.mode).limit(1)) == "bulk"
        replayed = session.scalars(select(Delivery).where(Delivery.replay_of.is_not(None))).all()
    assert len(replayed) == 3 and all(d.status == "delivered" for d in replayed)

    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert client.post("/replay", params={"since": future}, headers=admin).json()["replayed"] == 0


def test_bulk_replay_requires_a_filter(client, admin):
    assert client.post("/replay", headers=admin).status_code == 422
