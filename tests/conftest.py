import json
import os
import random
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from fakes.receiver import create_app as create_receiver
from launchbridge.app import create_app
from launchbridge.config import Settings, get_settings
from launchbridge.db import get_session, make_engine, make_session_factory
from launchbridge.destinations import DestinationRegistry
from launchbridge.signing import sign_headers
from launchbridge.worker import Worker

ROOT = Path(__file__).resolve().parents[1]
TABLES = "events, processed_events, deliveries, delivery_attempts, replays, signature_rejections"

SOURCE_SECRETS = {"orders": "orders-secret", "crm-source": "crm-source-secret"}
ADMIN_KEYS = {"ops": "test-admin-key"}
RECEIVER_SECRETS = {"crm": "crm-secret", "billing": "billing-secret"}

REGISTRY_YAML = """
destinations:
  - name: crm
    url: http://receiver/hooks/crm
    secret: crm-secret
    sources: ["*"]
    retry: {max_attempts: 4, base_delay_seconds: 0.5, max_delay_seconds: 8, jitter: 0.2}
  - name: billing
    url: http://receiver/hooks/billing
    secret: billing-secret
    sources: ["orders"]
    retry: {max_attempts: 3, base_delay_seconds: 1, max_delay_seconds: 30}
"""


def _normalise(url: str) -> str:
    return Settings(database_url=url).database_url


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        yield _normalise(configured)
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as container:
        yield _normalise(container.get_connection_url())


@pytest.fixture(scope="session")
def engine(database_url):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "launchbridge" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    return make_session_factory(engine)


@pytest.fixture
def settings(database_url) -> Settings:
    return Settings(
        database_url=database_url,
        webhook_secrets=SOURCE_SECRETS,
        admin_api_keys=ADMIN_KEYS,
        signature_tolerance_seconds=300,
    )


@pytest.fixture
def registry() -> DestinationRegistry:
    return DestinationRegistry.from_yaml(REGISTRY_YAML)


@pytest.fixture
def receiver() -> Iterator[TestClient]:
    with TestClient(create_receiver(RECEIVER_SECRETS)) as client:
        yield client


@pytest.fixture
def http_client(receiver) -> httpx.Client:
    """An httpx client whose requests are routed to the in-process receiver fake."""

    def handler(request: httpx.Request) -> httpx.Response:
        result = receiver.request(
            request.method,
            request.url.path,
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(result.status_code, content=result.content, headers=result.headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def app(settings, registry, session_factory):
    application = create_app(settings, registry)

    def session_override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_session] = session_override
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def worker(session_factory, registry, http_client) -> Worker:
    return Worker(session_factory, registry, http_client, concurrency=1, rng=random.Random(7))


@pytest.fixture
def admin() -> dict[str, str]:
    return {"X-API-Key": ADMIN_KEYS["ops"]}


def signed_post(
    client: TestClient,
    source: str,
    payload: dict,
    *,
    secret: str | None = None,
    timestamp: int | None = None,
    extra_headers: dict | None = None,
):
    body = json.dumps(payload).encode()
    headers = sign_headers(secret or SOURCE_SECRETS[source], body, timestamp or int(time.time()))
    headers["Content-Type"] = "application/json"
    headers.update(extra_headers or {})
    return client.post(f"/webhooks/{source}", content=body, headers=headers)


def new_payload(**fields) -> dict:
    payload = {"id": str(uuid.uuid4()), "type": "order.created", "amount": 42}
    payload.update(fields)
    return payload
