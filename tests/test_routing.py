import pytest

from launchbridge.destinations import DestinationRegistry
from launchbridge.routing import Predicate, event_type_of, lookup
from launchbridge.transform import Transform, render

PAYLOAD = {
    "id": "o-1",
    "type": "order.created",
    "amount": 150,
    "currency": "USD",
    "customer": {"email": "a@example.com", "tags": ["vip", "eu"]},
}


def test_lookup_walks_dicts_and_lists():
    assert lookup(PAYLOAD, "customer.email") == "a@example.com"
    assert lookup(PAYLOAD, "customer.tags.1") == "eu"
    assert lookup(PAYLOAD, "customer.tags.9") is lookup(PAYLOAD, "nope")
    assert lookup("not a dict", "id") is lookup(PAYLOAD, "nope")
    assert event_type_of(PAYLOAD) == "order.created"
    assert event_type_of({"type": 3}) is None
    assert event_type_of({"kind": "x"}, "kind") == "x"


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        ({"field": "amount", "op": "gte", "value": 100}, True),
        ({"field": "amount", "op": "lt", "value": 100}, False),
        ({"field": "currency", "op": "in", "value": ["USD", "EUR"]}, True),
        ({"field": "currency", "op": "not_in", "value": ["GBP"]}, True),
        ({"field": "customer.email", "op": "matches", "value": "@example\\.com$"}, True),
        ({"field": "customer.email", "op": "eq", "value": "b@example.com"}, False),
        ({"field": "missing", "op": "exists", "value": False}, True),
        ({"field": "customer", "op": "exists", "value": True}, True),
        ({"field": "missing", "op": "eq", "value": 1}, False),
        ({"field": "missing", "op": "ne", "value": 1}, True),
        ({"field": "currency", "op": "gt", "value": 5}, False),
    ],
)
def test_predicates(predicate, expected):
    assert Predicate(**predicate).holds(PAYLOAD) is expected


@pytest.mark.parametrize(
    "predicate",
    [
        {"field": "a", "op": "in", "value": "USD"},
        {"field": "a", "op": "exists", "value": "yes"},
        {"field": "a", "op": "matches", "value": "("},
        {"field": "a", "op": "between", "value": 1},
        {"field": "", "op": "eq", "value": 1},
    ],
)
def test_invalid_predicates_are_rejected(predicate):
    with pytest.raises(ValueError):
        Predicate(**predicate)


def test_destination_decisions_explain_why():
    registry = DestinationRegistry.from_yaml(
        """
destinations:
  - {name: all, url: "http://r/all", secret: s}
  - {name: orders-only, url: "http://r/o", secret: s, sources: ["orders"]}
  - {name: refunds, url: "http://r/r", secret: s, event_types: ["order.refund*"]}
  - name: big
    url: "http://r/big"
    secret: s
    when:
      - {field: amount, op: gte, value: 1000}
"""
    )
    decisions = {d.destination: d for d in registry.decisions("shop", PAYLOAD)}
    assert decisions["all"].routed and decisions["all"].reason == "matched"
    assert decisions["orders-only"].reason == "source 'shop' not in ['orders']"
    assert decisions["refunds"].reason == "event type 'order.created' not in ['order.refund*']"
    assert decisions["big"].reason == "predicate failed: amount gte 1000"
    assert [d.name for d in registry.route("shop", PAYLOAD)] == ["all"]
    refund = {**PAYLOAD, "type": "order.refunded", "amount": 5000}
    assert [d.name for d in registry.route("orders", refund)] == [
        "all",
        "orders-only",
        "refunds",
        "big",
    ]
    assert registry.decisions("orders", {"amount": 1})[2].reason == "payload has no 'type' field"


def test_render_keeps_types_for_whole_placeholders():
    context = {"source": "orders", "event_key": "id:o-1", "payload": PAYLOAD}
    assert render("{payload.amount}", context) == 150
    assert render("{payload.customer.tags}", context) == ["vip", "eu"]
    assert render("{source}/{payload.type}#{payload.amount}", context) == "orders/order.created#150"
    assert render("{payload.missing}", context) is None
    assert render("x-{payload.missing}-y", context) == "x--y"
    assert render({"n": ["{payload.id}", 7, True]}, context) == {"n": ["o-1", 7, True]}


def test_transform_pick_drop_rename_set_in_order():
    transform = Transform(
        pick=["id", "amount", "currency", "secret"],
        drop=["secret"],
        rename={"amount": "total"},
        set={
            "channel": "{source}",
            "label": "{source}:{payload.type}",
            "raw_amount": "{payload.amount}",
        },
    )
    out = transform.apply({**PAYLOAD, "secret": "x"}, {"source": "orders", "event_key": "k"})
    assert out == {
        "id": "o-1",
        "total": 150,
        "currency": "USD",
        "channel": "orders",
        "label": "orders:order.created",
        "raw_amount": 150,
    }
    assert Transform().is_identity()
    assert Transform().apply(PAYLOAD, {}) == PAYLOAD
    assert transform.apply(["not", "an", "object"], {}) == ["not", "an", "object"]
    assert transform.apply("raw text", {}) == "raw text"
