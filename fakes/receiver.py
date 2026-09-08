"""Destination fake: verifies outbound signatures, records an inbox, injects failures.

Failure rules match the `tag` field of the delivered payload:

    POST /control/rules {"tag": "flaky", "status": 503, "times": 2}   # 503 twice per key, then 200
    POST /control/rules {"tag": "broken", "status": 400}              # 400 forever
    DELETE /control/rules                                              # clear all rules

`times` counts per idempotency key, so retries of the same delivery eventually succeed.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from launchbridge.config import _parse_mapping
from launchbridge.signing import (
    EVENT_ID_HEADER,
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    verify_signature,
)


class Rule(BaseModel):
    tag: str
    status: int = Field(ge=400, le=599)
    times: int | None = Field(default=None, ge=1)


class Inbox:
    def __init__(self, secrets: dict[str, str], tolerance: int = 300) -> None:
        self.secrets = secrets
        self.tolerance = tolerance
        self.lock = threading.Lock()
        self.rules: list[Rule] = []
        self.rule_hits: dict[tuple[int, str], int] = {}
        self.entries: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        with self.lock:
            self.entries.clear()
            self.rule_hits.clear()

    def clear_rules(self) -> None:
        with self.lock:
            self.rules.clear()
            self.rule_hits.clear()

    def add_rule(self, rule: Rule) -> None:
        with self.lock:
            self.rules.append(rule)

    def injected_status(self, tag: str | None, key: str) -> int | None:
        if tag is None:
            return None
        with self.lock:
            for index, rule in enumerate(self.rules):
                if rule.tag != tag:
                    continue
                hits = self.rule_hits.get((index, key), 0)
                if rule.times is not None and hits >= rule.times:
                    continue
                self.rule_hits[(index, key)] = hits + 1
                return rule.status
        return None

    def record(self, key: str, **fields: Any) -> dict[str, Any]:
        with self.lock:
            entry = self.entries.setdefault(
                key, {"idempotency_key": key, "count": 0, "first_seen": time.time()}
            )
            entry["count"] += 1
            entry["last_seen"] = time.time()
            entry.update(fields)
            return dict(entry)


def create_app(secrets: dict[str, str] | None = None, tolerance: int = 300) -> FastAPI:
    if secrets is None:
        secrets = _parse_mapping(
            os.environ.get("RECEIVER_SECRETS", "crm=crm-dev-secret,billing=billing-dev-secret")
        )
    inbox = Inbox(secrets, tolerance)
    app = FastAPI(title="LaunchBridge receiver fake")
    app.state.inbox = inbox

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/hooks/{name}")
    async def hook(name: str, request: Request, response: Response) -> dict:
        body = await request.body()
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if not key:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="missing idempotency key")
        secret = secrets.get(name)
        signature_valid: bool | None = None
        if secret is not None:
            try:
                verify_signature(
                    secret,
                    request.headers.get(TIMESTAMP_HEADER),
                    request.headers.get(SIGNATURE_HEADER),
                    body,
                    tolerance,
                )
                signature_valid = True
            except SignatureError as exc:
                inbox.record(key, hook=name, signature_valid=False, reason=exc.reason)
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=exc.reason) from exc

        payload: Any = None
        tag: str | None = None
        try:
            import json

            envelope = json.loads(body)
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            if isinstance(payload, dict) and isinstance(payload.get("tag"), str):
                tag = payload["tag"]
        except ValueError:
            pass

        injected = inbox.injected_status(tag, key)
        entry = inbox.record(
            key,
            hook=name,
            signature_valid=signature_valid,
            event_id=request.headers.get(EVENT_ID_HEADER),
            tag=tag,
            last_status=injected or 200,
        )
        if injected is not None:
            response.status_code = injected
            return {"ok": False, "injected": injected, "count": entry["count"]}
        return {"ok": True, "count": entry["count"]}

    @app.get("/inbox")
    def list_inbox() -> dict:
        with inbox.lock:
            items = sorted(inbox.entries.values(), key=lambda e: e["first_seen"])
        return {"count": len(items), "keys": items}

    @app.get("/inbox/{key}")
    def get_entry(key: str) -> dict:
        with inbox.lock:
            entry = inbox.entries.get(key)
        if entry is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not received")
        return entry

    @app.delete("/inbox", status_code=status.HTTP_204_NO_CONTENT)
    def reset_inbox() -> Response:
        inbox.reset()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/control/rules")
    def list_rules() -> dict:
        with inbox.lock:
            return {"rules": [r.model_dump() for r in inbox.rules]}

    @app.post("/control/rules", status_code=status.HTTP_201_CREATED)
    def add_rule(rule: Rule) -> dict:
        inbox.add_rule(rule)
        return rule.model_dump()

    @app.delete("/control/rules", status_code=status.HTTP_204_NO_CONTENT)
    def clear_rules() -> Response:
        inbox.clear_rules()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


app = create_app()
