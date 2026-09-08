import threading

import httpx
import pytest

from launchbridge.worker import Worker
from smoke.smoke import Smoke, summarize
from tests.conftest import ADMIN_KEYS, SOURCE_SECRETS


def _bridge(test_client) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        result = test_client.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(result.status_code, content=result.content, headers=result.headers)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://smoke")


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
    assert passed == 14
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
    assert (passed, failed, skipped) == (10, 0, 4)
