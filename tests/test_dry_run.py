import json

import httpx
import pytest
from sqlalchemy import func, select

from launchbridge.destinations import DestinationRegistry
from launchbridge.models import Delivery, Event
from launchbridge.worker import Worker
from tests.conftest import new_payload, signed_post

RULES_YAML = """
event_type_field: type
destinations:
  - name: crm
    url: http://receiver/hooks/crm
    secret: crm-secret
    sources: ["*"]
    transform:
      drop: [internal]
      rename: {amount: total}
      set:
        channel: "{source}"
        label: "{source}:{payload.type}"
        ref: "{event_key}"
  - name: billing
    url: http://receiver/hooks/billing
    secret: billing-secret
    sources: ["orders"]
    event_types: ["order.*"]
    when:
      - {field: amount, op: gte, value: 100}
      - {field: currency, op: in, value: [USD, EUR]}
    transform:
      pick: [id, amount, currency]
  - name: audit
    url: http://receiver/hooks/audit
    secret: audit-secret
    sources: ["orders"]
    event_types: ["order.refunded"]
"""


@pytest.fixture
def registry() -> DestinationRegistry:
    return DestinationRegistry.from_yaml(RULES_YAML)


def _destinations(session_factory, event_id) -> set[str]:
    with session_factory() as session:
        return set(
            session.scalars(select(Delivery.destination).where(Delivery.event_id == event_id))
        )


def test_rules_route_by_source_event_type_and_predicates(client, session_factory):
    big = signed_post(client, "orders", new_payload(amount=150, currency="USD")).json()
    small = signed_post(client, "orders", new_payload(amount=50, currency="USD")).json()
    gbp = signed_post(client, "orders", new_payload(amount=500, currency="GBP")).json()
    refund = signed_post(
        client, "orders", new_payload(type="order.refunded", amount=150, currency="EUR")
    ).json()
    other = signed_post(client, "crm-source", new_payload(amount=999, currency="USD")).json()
    assert _destinations(session_factory, big["event_id"]) == {"crm", "billing"}
    assert _destinations(session_factory, small["event_id"]) == {"crm"}
    assert _destinations(session_factory, gbp["event_id"]) == {"crm"}
    assert _destinations(session_factory, refund["event_id"]) == {"crm", "billing", "audit"}
    assert _destinations(session_factory, other["event_id"]) == {"crm"}


def _capturing_worker(session_factory, registry) -> tuple[Worker, dict[str, dict]]:
    seen: dict[str, dict] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        envelope = json.loads(request.content)
        seen[envelope["destination"]] = envelope
        return httpx.Response(200, json={"ok": True})

    worker = Worker(
        session_factory,
        registry,
        httpx.Client(transport=httpx.MockTransport(capture)),
        concurrency=1,
    )
    return worker, seen


def test_transforms_shape_the_outbound_payload(client, session_factory, registry):
    worker, seen = _capturing_worker(session_factory, registry)
    payload = new_payload(amount=150, currency="USD", internal="do-not-forward")
    response = signed_post(client, "orders", payload)
    worker.drain()
    assert set(seen) == {"crm", "billing"}
    assert seen["crm"]["payload"] == {
        "id": payload["id"],
        "type": "order.created",
        "total": 150,
        "currency": "USD",
        "channel": "orders",
        "label": "orders:order.created",
        "ref": f"id:{payload['id']}",
    }
    assert seen["billing"]["payload"] == {"id": payload["id"], "amount": 150, "currency": "USD"}
    assert seen["crm"]["event_id"] == response.json()["event_id"]
    with session_factory() as session:
        event = session.get(Event, response.json()["event_id"])
    assert event.payload["internal"] == "do-not-forward", "the stored event keeps the raw payload"


def test_dry_run_matches_real_dispatch(client, session_factory, registry, admin):
    worker, seen = _capturing_worker(session_factory, registry)
    payload = new_payload(amount=150, currency="EUR", internal="x")
    body = json.dumps(payload).encode()

    preview = client.post("/dry-run/orders", content=body, headers=admin)
    assert preview.status_code == 200
    result = preview.json()
    assert result["source"] == "orders"
    assert result["event_key"] == f"id:{payload['id']}"
    assert result["event_type"] == "order.created"
    assert result["routed_to"] == ["crm", "billing"]
    by_name = {d["destination"]: d for d in result["destinations"]}
    assert by_name["audit"]["routed"] is False
    assert by_name["audit"]["reason"] == "event type 'order.created' not in ['order.refunded']"
    assert by_name["audit"]["payload"] is None
    assert by_name["billing"]["url"] == "http://receiver/hooks/billing"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0, (
            "dry-run records nothing"
        )

    signed_post(client, "orders", payload)
    worker.drain()
    assert set(seen) == set(result["routed_to"])
    for name in result["routed_to"]:
        assert seen[name]["payload"] == by_name[name]["payload"]


def test_dry_run_needs_admin_key_and_known_source(client, admin):
    body = json.dumps(new_payload()).encode()
    assert client.post("/dry-run/orders", content=body).status_code == 401
    assert client.post("/dry-run/nobody", content=body, headers=admin).status_code == 404
    unparsable = client.post("/dry-run/orders", content=b"not json", headers=admin)
    assert unparsable.status_code == 200
    assert unparsable.json()["event_key"].startswith("hash:")
    assert unparsable.json()["routed_to"] == ["crm"]
