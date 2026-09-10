from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import new_payload, signed_post


@pytest.fixture
def deliveries(client, worker, receiver, admin):
    """Two clean orders events, one order that fails at both destinations, one replayed."""
    receiver.post("/control/rules", json={"tag": "broken", "status": 400})
    signed_post(client, "orders", new_payload(id="ok-1"))
    signed_post(client, "crm-source", new_payload(id="ok-2"))
    broken = signed_post(client, "orders", new_payload(id="bad-1", tag="broken"))
    worker.drain(datetime.now(UTC) + timedelta(hours=1))

    receiver.delete("/control/rules")
    failed = broken.json()["delivery_ids"][0]
    client.post(f"/deliveries/{failed}/replay", headers=admin, params={"reason": "fixed"})
    worker.drain(datetime.now(UTC) + timedelta(hours=1))
    return failed


def search(client, admin, **params):
    return client.get("/deliveries", params=params, headers=admin).json()


def test_search_by_status_source_and_destination(client, deliveries, admin):
    assert search(client, admin)["count"] == 6, "5 originals plus one replay"
    assert search(client, admin, status="delivered")["count"] == 4
    assert search(client, admin, status="failed,replayed")["count"] == 2
    assert client.get("/deliveries", params={"status": "nope"}, headers=admin).status_code == 422

    assert search(client, admin, source="crm-source")["count"] == 1
    assert search(client, admin, destination="billing")["count"] == 2
    both = search(client, admin, source="orders", destination="crm")
    assert both["count"] == 3 and {d["source"] for d in both["items"]} == {"orders"}


def test_search_by_event_key_idempotency_key_and_status_code(client, deliveries, admin):
    by_key = search(client, admin, event_key="id:bad-1")
    assert by_key["count"] == 3, "crm original, crm replay and the failed billing delivery"

    idempotency_key = by_key["items"][0]["idempotency_key"]
    assert search(client, admin, idempotency_key=idempotency_key)["count"] == 2

    assert search(client, admin, status_code=400)["count"] == 2
    assert search(client, admin, status_code=200)["count"] == 4
    assert client.get("/deliveries", params={"status_code": 99}, headers=admin).status_code == 422


def test_search_separates_replays_and_matches_error_text(client, deliveries, admin):
    replays = search(client, admin, replayed=True)
    assert replays["count"] == 1
    assert replays["items"][0]["replay_of"] == deliveries
    assert search(client, admin, replayed=False)["count"] == 5

    errors = search(client, admin, q="HTTP 400")
    assert {d["destination"] for d in errors["items"]} == {"crm", "billing"}
    assert search(client, admin, q="id:ok-1")["count"] == 2, "matches the event key too"
    assert search(client, admin, q="nothing here")["count"] == 0


def test_search_windows_orders_and_pages(client, deliveries, admin):
    now = datetime.now(UTC)
    assert search(client, admin, since=(now - timedelta(minutes=5)).isoformat())["count"] == 6
    assert search(client, admin, until=(now - timedelta(minutes=5)).isoformat())["count"] == 0
    assert search(client, admin, since=(now + timedelta(minutes=5)).isoformat())["count"] == 0

    newest = search(client, admin, order="desc")["items"]
    oldest = search(client, admin, order="asc")["items"]
    assert [d["id"] for d in newest] == [d["id"] for d in reversed(oldest)]

    page = search(client, admin, order="asc", limit=2, offset=2)
    assert page["count"] == 6, "count is the whole result, not the page"
    assert [d["id"] for d in page["items"]] == [d["id"] for d in oldest[2:4]]
    assert client.get("/deliveries", params={"order": "sideways"}, headers=admin).status_code == (
        422
    )
