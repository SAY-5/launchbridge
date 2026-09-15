import pytest

from launchbridge.app import create_app
from launchbridge.auth import resolve_api_key
from launchbridge.config import Settings, get_settings
from tests.conftest import new_payload, signed_post


@pytest.mark.parametrize(
    "path",
    [
        "/deliveries",
        "/replays",
        "/events",
        "/stats",
        "/deliveries/00000000-0000-0000-0000-000000000000",
    ],
)
def test_admin_endpoints_require_api_key(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "nope"}).status_code == 401


def test_admin_endpoints_accept_valid_key(client, admin):
    assert client.get("/deliveries", headers=admin).status_code == 200
    assert client.get("/events", headers=admin).status_code == 200


def test_unconfigured_keys_return_503(database_url, registry):
    settings = Settings(database_url=database_url, webhook_secrets={"orders": "s"})
    app = create_app(settings, registry)
    app.dependency_overrides[get_settings] = lambda: settings
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/deliveries", headers={"X-API-Key": "x"}).status_code == 503


def test_resolve_api_key_returns_label():
    keys = {"ops": "k1", "ci": "k2"}
    assert resolve_api_key("k2", keys) == "ci"
    assert resolve_api_key("k3", keys) is None
    assert resolve_api_key(None, keys) is None


def test_health_ready_and_docs_are_public(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json() == {"status": "ok", "database": "ok"}
    assert client.get("/openapi.json").status_code == 200


def test_metrics_exposes_launchbridge_series(client, worker, admin):
    signed_post(client, "crm-source", new_payload())
    worker.drain()
    text = client.get("/metrics").text
    for name in (
        "launchbridge_events_received_total",
        "launchbridge_deliveries_delivered_total",
        "launchbridge_delivery_latency_seconds",
        'launchbridge_deliveries{status="delivered"} 1.0',
    ):
        assert name in text


def test_stats_and_event_listing(client, worker, receiver, admin):
    receiver.post("/control/rules", json={"tag": "broken", "status": 400})
    payload = new_payload()
    signed_post(client, "orders", payload)
    signed_post(client, "orders", payload, timestamp=1)  # stale: rejected
    signed_post(client, "crm-source", new_payload(tag="broken"))
    worker.drain()
    stats = client.get("/stats", headers=admin).json()
    assert stats["events"] == {"accepted": 2, "deduplicated": 0, "received": 2}
    assert stats["deliveries"]["delivered"] == 2
    assert stats["deliveries"]["failed"] == 1
    assert stats["signature_rejections"] == 1
    assert stats["latency_ms"]["p50"] is not None
    scoped = client.get("/stats", params={"source": "orders"}, headers=admin).json()
    assert scoped["deliveries"]["failed"] == 0 and scoped["signature_rejections"] == 1
    events = client.get("/events", params={"source": "orders"}, headers=admin).json()
    assert events["count"] == 1
    event = client.get(f"/events/{events['items'][0]['id']}", headers=admin).json()
    assert event["event_key"] == f"id:{payload['id']}"


def test_healthz_reports_the_build_it_is_running(client, monkeypatch):
    """GIT_SHA is baked into the image, so /healthz can name the build under test."""
    monkeypatch.setenv("GIT_SHA", "abc1234")
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["git_sha"] == "abc1234"
    monkeypatch.delenv("GIT_SHA")
    assert client.get("/healthz").json()["git_sha"] is None
