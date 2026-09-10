import time
from datetime import UTC, datetime, timedelta

from tests.conftest import new_payload, signed_post


def test_overview_breaks_counts_down_by_source_and_destination(client, worker, receiver, admin):
    receiver.post("/control/rules", json={"tag": "broken", "status": 400})
    payload = new_payload()
    signed_post(client, "orders", payload)
    signed_post(client, "orders", payload, timestamp=int(time.time()) - 5)  # duplicate
    signed_post(client, "orders", payload, timestamp=1)  # stale: rejected
    signed_post(client, "crm-source", new_payload(tag="broken"))
    worker.drain(datetime.now(UTC) + timedelta(hours=1))

    overview = client.get("/ops/overview", headers=admin).json()
    assert overview["totals"]["received"] == 3
    assert overview["totals"]["rejected"] == 1
    assert overview["totals"]["queue_depth"] == 0
    assert overview["totals"]["breakers_open"] == 0
    assert overview["totals"]["delivered"] == 2 and overview["totals"]["failed"] == 1

    orders = overview["sources"]["orders"]
    assert orders["accepted"] == 1 and orders["deduplicated"] == 1 and orders["rejected"] == 1
    assert orders["deliveries"]["delivered"] == 2, "crm and billing"
    assert overview["sources"]["crm-source"]["deliveries"]["failed"] == 1

    crm = overview["destinations"]["crm"]
    assert crm["delivered"] == 1 and crm["failed"] == 1 and crm["breaker"] == "closed"
    assert crm["queue_depth"] == 0 and crm["latency_ms"]["p50"] is not None
    assert overview["destinations"]["billing"]["delivered"] == 1

    failed = overview["failed"]
    assert failed["count"] == 1
    assert failed["recent"][0]["destination"] == "crm"
    assert failed["recent"][0]["source"] == "crm-source"
    assert "HTTP 400" in failed["recent"][0]["last_error"]


def test_overview_shows_the_queue_before_the_worker_runs(client, admin):
    signed_post(client, "orders", new_payload())
    overview = client.get("/ops/overview", headers=admin).json()
    assert overview["totals"]["queue_depth"] == 2
    assert overview["destinations"]["billing"]["queue_depth"] == 1
    assert overview["failed"]["count"] == 0 and overview["last_replay"] is None
    assert overview["smoke"] is None


def test_overview_reports_the_last_replay(client, worker, receiver, admin):
    receiver.post("/control/rules", json={"tag": "broken", "status": 400})
    response = signed_post(client, "crm-source", new_payload(tag="broken"))
    worker.drain(datetime.now(UTC) + timedelta(hours=1))
    failed_id = response.json()["delivery_ids"][0]

    receiver.delete("/control/rules")
    replayed = client.post(
        f"/deliveries/{failed_id}/replay", headers=admin, params={"reason": "receiver fixed"}
    )
    worker.drain(datetime.now(UTC) + timedelta(hours=1))

    last = client.get("/ops/overview", headers=admin).json()["last_replay"]
    assert last["actor"] == "ops" and last["mode"] == "single"
    assert last["reason"] == "receiver fixed"
    assert last["source"] == "crm-source" and last["destination"] == "crm"
    assert last["original_delivery_id"] == failed_id
    assert last["new_delivery_id"] == replayed.json()["replay_delivery_id"]
    assert last["outcome"] == "delivered"


def test_overview_carries_the_last_smoke_result(client, admin):
    assert client.post("/ops/smoke", json={"passed": 15, "failed": 0}).status_code == 401
    green = client.post(
        "/ops/smoke",
        headers=admin,
        json={"passed": 15, "failed": 0, "version": "5.0.0", "duration_ms": 4200},
    )
    assert green.status_code == 201 and green.json()["status"] == "green"

    smoke = client.get("/ops/overview", headers=admin).json()["smoke"]
    assert smoke["status"] == "green" and smoke["checks"] == 15
    assert smoke["version"] == "5.0.0" and smoke["duration_ms"] == 4200

    client.post("/ops/smoke", headers=admin, json={"passed": 11, "failed": 3, "skipped": 1})
    smoke = client.get("/ops/overview", headers=admin).json()["smoke"]
    assert smoke["status"] == "red" and (smoke["passed"], smoke["failed"]) == (11, 3)
    assert smoke["checks"] == 15
    assert client.post(
        "/ops/smoke", headers=admin, json={"passed": -1, "failed": 0}
    ).status_code == (422)


def test_overview_since_narrows_the_window(client, worker, admin):
    signed_post(client, "orders", new_payload())
    worker.drain()
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    later = client.get("/ops/overview", params={"since": cutoff.isoformat()}, headers=admin).json()
    assert later["since"] == cutoff.isoformat()
    assert later["totals"]["received"] == 0 and later["totals"]["delivered"] == 0
    assert later["sources"] == {}
    assert later["destinations"]["crm"]["delivered"] == 0
    assert client.get("/ops/overview").status_code == 401
