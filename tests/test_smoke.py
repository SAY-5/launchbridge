import threading

import httpx
import pytest

from launchbridge.worker import Worker
from smoke.smoke import Smoke, parse_receiver_headers, summarize
from tests.conftest import ADMIN_KEYS, SOURCE_SECRETS


def _bridge(test_client, headers: dict[str, str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        result = test_client.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(result.status_code, content=result.content, headers=result.headers)

    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://smoke", headers=headers or {}
    )


def _routed_bridge(test_client, rule: tuple[str, str]) -> httpx.Client:
    """A bridge that stands in for the trial ALB: one listener, one header rule.

    Requests without the rule header reach the API target group, which knows nothing about
    the receiver control API, so they come back 404 the way the load balancer would answer.
    """
    name, value = rule

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get(name) != value:
            return httpx.Response(404, json={"detail": "Not Found"})
        result = test_client.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(result.status_code, content=result.content, headers=result.headers)

    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://alb", headers=dict([rule])
    )


def _unrouted_bridge(test_client, rule: tuple[str, str]) -> httpx.Client:
    """The same ALB stand-in without the routing header on the client."""
    name, value = rule

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get(name) != value:
            return httpx.Response(404, json={"detail": "Not Found"})
        result = test_client.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(result.status_code, content=result.content, headers=result.headers)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://alb")


@pytest.mark.timeout(180)
def test_smoke_suite_passes_in_process(client, receiver, session_factory, registry, http_client):
    worker = Worker(session_factory, registry, http_client, concurrency=2)
    thread = threading.Thread(
        target=worker.run_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    lines: list[str] = []
    try:
        results = Smoke(
            _bridge(client),
            _bridge(receiver),
            source="crm-source",
            secret=SOURCE_SECRETS["crm-source"],
            api_key=ADMIN_KEYS["ops"],
            timeout=60,
            out=lines.append,
        ).run()
    finally:
        worker.stop()
        thread.join(timeout=5)
    passed, failed, skipped = summarize(results)
    assert failed == 0, "\n".join(lines)
    assert skipped == 0
    assert passed == 15
    assert all(line.startswith("[PASS]") for line in lines)


def test_smoke_skips_receiver_checks_without_receiver(
    client, session_factory, registry, http_client
):
    worker = Worker(session_factory, registry, http_client, concurrency=1)
    thread = threading.Thread(
        target=worker.run_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        results = Smoke(
            _bridge(client),
            None,
            source="crm-source",
            secret=SOURCE_SECRETS["crm-source"],
            api_key=ADMIN_KEYS["ops"],
            timeout=30,
            out=lambda _: None,
        ).run()
    finally:
        worker.stop()
        thread.join(timeout=5)
    passed, failed, skipped = summarize(results)
    assert (passed, failed, skipped) == (11, 0, 4)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["X-Target=receiver"], {"X-Target": "receiver"}),
        (["X-Target=receiver,X-Stage=trial"], {"X-Target": "receiver", "X-Stage": "trial"}),
        (["X-Target = receiver ", ""], {"X-Target": "receiver"}),
        ([], {}),
        (None, {}),
    ],
)
def test_receiver_headers_parse_from_arguments_and_the_environment_syntax(values, expected):
    assert parse_receiver_headers(values) == expected


@pytest.mark.parametrize("value", ["X-Target", "=receiver"])
def test_receiver_headers_reject_values_that_are_not_name_equals_value(value):
    with pytest.raises(ValueError, match="NAME=VALUE"):
        parse_receiver_headers([value])


@pytest.mark.timeout(180)
def test_failure_injection_reaches_a_receiver_that_needs_a_routing_header(
    client, receiver, session_factory, registry, http_client
):
    """The receiver fake behind the ALB rule is only reachable with its header.

    Without the header on the receiver client the four failure-injection checks cannot add
    a rule or read the inbox, so the suite comes back red instead of green.
    """
    worker = Worker(session_factory, registry, http_client, concurrency=2)
    thread = threading.Thread(
        target=worker.run_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    rule = ("X-Target", "receiver")
    lines: list[str] = []
    try:
        results = Smoke(
            _bridge(client),
            _routed_bridge(receiver, rule),
            source="crm-source",
            secret=SOURCE_SECRETS["crm-source"],
            api_key=ADMIN_KEYS["ops"],
            timeout=60,
            out=lines.append,
        ).run()
    finally:
        worker.stop()
        thread.join(timeout=5)
    unrouted = _unrouted_bridge(receiver, rule)
    assert unrouted.get("/inbox/anything").status_code == 404, "the rule is what routes"
    passed, failed, skipped = summarize(results)
    assert failed == 0, "\n".join(lines)
    assert (passed, skipped) == (15, 0)
