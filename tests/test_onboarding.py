import json

from launchbridge.models import SourceSecret
from tests.conftest import new_payload, signed_post


def _onboard(client, admin, source="shop", **body):
    return client.post("/sources", headers=admin, json={"source": source, **body})


def test_onboarded_source_can_sign_events_immediately(client, admin, session_factory):
    created = _onboard(client, admin)
    assert created.status_code == 201
    body = created.json()
    assert body["source"] == "shop" and body["webhook_path"] == "/webhooks/shop"
    assert len(body["secret"]) >= 32
    assert body["secret"] in body["signing"]["python"]
    assert "/webhooks/shop" in body["signing"]["shell"]

    accepted = signed_post(client, "shop", new_payload(), secret=body["secret"])
    assert accepted.status_code == 202
    assert signed_post(client, "shop", new_payload(), secret="another-secret").status_code == 401

    with session_factory() as session:
        row = session.get(SourceSecret, "shop")
    assert row.previous_secret is None
    listing = client.get("/sources", headers=admin).json()
    shop = next(s for s in listing["items"] if s["source"] == "shop")
    assert shop["secret_from"] == "database" and shop["previous_active"] is False
    assert body["secret"] not in json.dumps(listing)


def test_onboarding_accepts_a_chosen_secret_and_rejects_clashes(client, admin):
    assert _onboard(client, admin, secret="a-secret-long-enough").json()["secret"] == (
        "a-secret-long-enough"
    )
    assert _onboard(client, admin).status_code == 409
    assert _onboard(client, admin, source="orders").status_code == 409, "environment source"
    assert _onboard(client, admin, source="Shop Two").status_code == 422
    assert _onboard(client, admin, source="other", secret="short").status_code == 422
    assert client.post("/sources", json={"source": "shop"}).status_code == 401


def test_removing_a_source_stops_its_webhooks(client, admin):
    secret = _onboard(client, admin).json()["secret"]
    assert signed_post(client, "shop", new_payload(), secret=secret).status_code == 202

    assert client.delete("/sources/shop", headers=admin).status_code == 204
    assert signed_post(client, "shop", new_payload(), secret=secret).status_code == 404
    assert [s["source"] for s in client.get("/sources", headers=admin).json()["items"]] == [
        "crm-source",
        "orders",
    ]

    assert client.delete("/sources/shop", headers=admin).status_code == 404
    assert client.delete("/sources/orders", headers=admin).status_code == 409, "from the env"
    assert client.delete("/sources/shop").status_code == 401


def test_onboarded_source_is_routed_and_delivered(client, worker, receiver, admin):
    secret = _onboard(client, admin, source="shop").json()["secret"]
    response = signed_post(client, "shop", new_payload(), secret=secret)
    worker.drain()
    delivery_id = response.json()["delivery_ids"][0]
    detail = client.get(f"/deliveries/{delivery_id}", headers=admin).json()
    assert detail["status"] == "delivered" and detail["source"] == "shop"
    assert detail["destination"] == "crm", "the wildcard destination takes any source"
    with_source = client.get("/deliveries", params={"source": "shop"}, headers=admin).json()
    assert with_source["count"] == 1
