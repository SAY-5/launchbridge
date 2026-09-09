import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request, Response

from launchbridge import api


@pytest.mark.parametrize("route", ["webhook", "dry-run"])
@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")]])
def test_oversized_stream_is_rejected_before_reading_the_rest(monkeypatch, route, headers):
    monkeypatch.setattr(api, "source_secrets", lambda *args: ["synthetic-test-secret"])
    monkeypatch.setattr(api, "source_exists", lambda *args: True)
    received = 0

    async def receive():
        nonlocal received
        received += 1
        if received > 2:
            raise AssertionError("continued consuming an already oversized request")
        return {"type": "http.request", "body": b"x" * 600_000, "more_body": True}

    request = Request({"type": "http", "headers": headers}, receive=receive)
    kwargs = {
        "source": "orders",
        "request": request,
        "session": None,
        "settings": SimpleNamespace(),
        "registry": None,
    }
    if route == "webhook":
        call = api.receive_webhook(response=Response(), **kwargs)
    else:
        call = api.dry_run(**kwargs)
    with pytest.raises(HTTPException) as error:
        asyncio.run(call)
    assert error.value.status_code == 413
    assert received == 2


@pytest.mark.parametrize("body", [b"", '{"id":"café"}'.encode(), b"x" * api.MAX_BODY_BYTES])
def test_body_bytes_are_preserved_through_the_limit(body):
    chunks = iter([body[:10], body[10:]])
    received = 0

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": next(chunks), "more_body": received < 2}

    request = Request({"type": "http", "headers": []}, receive=receive)
    assert asyncio.run(api._read_event_body(request)) == body
