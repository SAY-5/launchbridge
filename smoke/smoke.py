"""End-to-end smoke checks against any LaunchBridge base URL.

    python -m smoke.smoke --base-url http://localhost:8080 --receiver-url http://localhost:8081

Every check prints PASS, FAIL or SKIP. Checks that need the receiver fake's control API
(failure injection, inbox inspection) are skipped when no receiver URL is given. Where the
receiver is reached through a router that needs a header to pick it, pass
--receiver-header NAME=VALUE (repeatable, or RECEIVER_HEADERS as a comma-separated list):
the Terraform trial puts the receiver behind the same ALB as the API on an
`X-Target: receiver` rule, so both URLs are the load balancer.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from launchbridge.signing import sign_headers


@dataclass
class Check:
    name: str
    passed: bool | None
    detail: str = ""

    @property
    def label(self) -> str:
        return {True: "PASS", False: "FAIL", None: "SKIP"}[self.passed]


class SmokeFailure(AssertionError):
    pass


def parse_receiver_headers(values: list[str] | None) -> dict[str, str]:
    """Parse `NAME=VALUE` pairs sent with every receiver request.

    Accepts repeated arguments and comma-separated lists, so the argument and the
    RECEIVER_HEADERS environment variable take the same syntax.
    """
    headers: dict[str, str] = {}
    for value in values or []:
        for pair in value.split(","):
            item = pair.strip()
            if not item:
                continue
            name, separator, header_value = item.partition("=")
            if not separator or not name.strip():
                raise ValueError(f"receiver header {item!r} is not NAME=VALUE")
            headers[name.strip()] = header_value.strip()
    return headers


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


class Smoke:
    def __init__(
        self,
        api: httpx.Client,
        receiver: httpx.Client | None,
        *,
        source: str,
        secret: str,
        api_key: str,
        timeout: float = 60.0,
        poll_interval: float = 0.25,
        out: Callable[[str], None] = print,
    ) -> None:
        self.api = api
        self.receiver = receiver
        self.source = source
        self.secret = secret
        self.admin = {"X-API-Key": api_key}
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.out = out
        self.run_id = uuid.uuid4().hex[:8]
        self.results: list[Check] = []

    # helpers

    def post_event(self, payload: dict, *, secret: str | None = None, timestamp: int | None = None):
        body = json.dumps(payload).encode()
        headers = sign_headers(secret or self.secret, body, timestamp)
        headers["Content-Type"] = "application/json"
        return self.api.post(f"/webhooks/{self.source}", content=body, headers=headers)

    def payload(self, **fields: object) -> dict:
        data: dict = {"id": f"smoke-{self.run_id}-{uuid.uuid4().hex[:8]}", "kind": "smoke"}
        data.update(fields)
        return data

    def wait_for(self, delivery_id: str, statuses: set[str]) -> dict:
        deadline = time.monotonic() + self.timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.api.get(f"/deliveries/{delivery_id}", headers=self.admin).json()
            if last.get("status") in statuses:
                return last
            time.sleep(self.poll_interval)
        raise SmokeFailure(
            f"delivery {delivery_id} still {last.get('status')} after {self.timeout}s"
        )

    def add_rule(self, tag: str, status: int, times: int | None = None) -> None:
        assert self.receiver is not None
        body = {"tag": tag, "status": status}
        if times is not None:
            body["times"] = times
        expect(self.receiver.post("/control/rules", json=body).status_code == 201, "rule not added")

    def clear_rules(self) -> None:
        if self.receiver is not None:
            self.receiver.delete("/control/rules")

    def inbox(self, key: str) -> dict:
        assert self.receiver is not None
        response = self.receiver.get(f"/inbox/{key}")
        expect(response.status_code == 200, f"receiver never saw {key}")
        return response.json()

    def check(self, name: str, fn: Callable[[], str | None], needs_receiver: bool = False) -> None:
        if needs_receiver and self.receiver is None:
            result = Check(name, None, "no receiver URL configured")
        else:
            try:
                result = Check(name, True, fn() or "")
            except (SmokeFailure, httpx.HTTPError, KeyError, ValueError) as exc:
                result = Check(name, False, f"{type(exc).__name__}: {exc}")
        self.results.append(result)
        suffix = f"  ({result.detail})" if result.detail else ""
        self.out(f"[{result.label}] {name}{suffix}")

    # checks

    def run(self) -> list[Check]:
        state: dict = {}

        def healthz() -> str:
            response = self.api.get("/healthz")
            expect(response.status_code == 200, f"healthz returned {response.status_code}")
            return f"version {response.json()['version']}"

        def readyz() -> str:
            response = self.api.get("/readyz")
            expect(response.status_code == 200, f"readyz returned {response.status_code}")
            return f"database {response.json()['database']}"

        def accepted() -> str:
            state["payload"] = self.payload()
            response = self.post_event(state["payload"])
            expect(response.status_code == 202, f"expected 202, got {response.status_code}")
            body = response.json()
            expect(body["deduplicated"] is False, "fresh event flagged as duplicate")
            expect(len(body["delivery_ids"]) >= 1, "no deliveries enqueued")
            state["event_id"] = body["event_id"]
            state["delivery_id"] = body["delivery_ids"][0]
            return f"event {body['event_id']} with {len(body['delivery_ids'])} deliveries"

        def delivered() -> str:
            delivery = self.wait_for(state["delivery_id"], {"delivered", "failed"})
            expect(delivery["status"] == "delivered", f"delivery ended {delivery['status']}")
            state["key"] = delivery["idempotency_key"]
            return f"{delivery['destination']} in {delivery['latency_ms']} ms"

        def receiver_saw_it() -> str:
            entry = self.inbox(state["key"])
            expect(entry["signature_valid"] is True, "receiver rejected the outbound signature")
            expect(entry["count"] == 1, f"receiver saw the key {entry['count']} times")
            return "signature valid, seen once"

        def deduplicated() -> str:
            time.sleep(1.05)
            response = self.post_event(state["payload"])
            expect(response.status_code == 200, f"expected 200, got {response.status_code}")
            body = response.json()
            expect(body["deduplicated"] is True, "duplicate not flagged")
            expect(body["delivery_ids"] == [], "duplicate enqueued deliveries")
            return "deduplicated: true, no deliveries"

        def wrong_secret() -> str:
            response = self.post_event(self.payload(), secret="not-the-secret")
            expect(response.status_code == 401, f"expected 401, got {response.status_code}")
            return response.json()["detail"]["error"]

        def stale_timestamp() -> str:
            response = self.post_event(self.payload(), timestamp=int(time.time()) - 3600)
            expect(response.status_code == 401, f"expected 401, got {response.status_code}")
            return response.json()["detail"]["error"]

        def replayed_signature() -> str:
            body = json.dumps(self.payload()).encode()
            headers = sign_headers(self.secret, body)
            first = self.api.post(f"/webhooks/{self.source}", content=body, headers=headers)
            expect(first.status_code == 202, f"first send returned {first.status_code}")
            second = self.api.post(f"/webhooks/{self.source}", content=body, headers=headers)
            expect(second.status_code == 409, f"expected 409, got {second.status_code}")
            return second.json()["detail"]["error"]

        def admin_auth() -> str:
            expect(self.api.get("/deliveries").status_code == 401, "missing key accepted")
            expect(
                self.api.get("/deliveries", headers={"X-API-Key": "wrong"}).status_code == 401,
                "wrong key accepted",
            )
            expect(
                self.api.get("/deliveries", headers=self.admin).status_code == 200,
                "valid key rejected",
            )
            return "401 without key, 200 with key"

        def bounded_retries() -> str:
            tag = f"smoke-down-{self.run_id}"
            self.add_rule(tag, 503)
            response = self.post_event(self.payload(tag=tag))
            expect(response.status_code == 202, "failing event not accepted")
            state["failed_id"] = response.json()["delivery_ids"][0]
            delivery = self.wait_for(state["failed_id"], {"delivered", "failed"})
            expect(delivery["status"] == "failed", f"delivery ended {delivery['status']}")
            expect(
                delivery["attempts"] == delivery["max_attempts"],
                f"{delivery['attempts']} attempts, expected {delivery['max_attempts']}",
            )
            expect("exhausted" in (delivery["last_error"] or ""), "last_error lacks exhaustion")
            state["failed_key"] = delivery["idempotency_key"]
            state["max_attempts"] = delivery["max_attempts"]
            expect(self.inbox(state["failed_key"])["count"] == delivery["max_attempts"], "count")
            return f"failed after {delivery['attempts']}/{delivery['max_attempts']} attempts"

        def replay_after_fix() -> str:
            self.clear_rules()
            response = self.api.post(
                f"/deliveries/{state['failed_id']}/replay",
                headers=self.admin,
                params={"reason": "smoke: destination repaired"},
            )
            expect(response.status_code == 202, f"replay returned {response.status_code}")
            new_id = response.json()["replay_delivery_id"]
            delivery = self.wait_for(new_id, {"delivered", "failed"})
            expect(delivery["status"] == "delivered", f"replay ended {delivery['status']}")
            expect(delivery["series"] == 2, "replay is not series 2")
            original = self.api.get(f"/deliveries/{state['failed_id']}", headers=self.admin).json()
            expect(original["status"] == "replayed", "original not marked replayed")
            entry = self.inbox(state["failed_key"])
            expect(
                entry["count"] == state["max_attempts"] + 1,
                f"receiver saw the key {entry['count']} times",
            )
            return f"same idempotency key, receiver count {entry['count']}"

        def bulk_replay() -> str:
            tag = f"smoke-broken-{self.run_id}"
            since = datetime.now(UTC).isoformat()
            self.add_rule(tag, 400)
            response = self.post_event(self.payload(tag=tag))
            failed_id = response.json()["delivery_ids"][0]
            self.wait_for(failed_id, {"failed"})
            self.clear_rules()
            bulk = self.api.post(
                "/replay", headers=self.admin, params={"source": self.source, "since": since}
            )
            expect(bulk.status_code == 202, f"bulk replay returned {bulk.status_code}")
            ids = bulk.json()["delivery_ids"]
            expect(bulk.json()["replayed"] >= 1, "bulk replay found nothing")
            outcomes = [self.wait_for(i, {"delivered", "failed"})["status"] for i in ids]
            expect(all(o == "delivered" for o in outcomes), f"outcomes {outcomes}")
            return f"replayed {len(ids)}, all delivered"

        def secret_rotation() -> str:
            temporary = f"smoke-rotation-{self.run_id}-{uuid.uuid4().hex}"
            rotated = self.api.post(
                f"/sources/{self.source}/rotate",
                headers=self.admin,
                json={"secret": temporary, "overlap_seconds": 120},
            )
            expect(rotated.status_code == 200, f"rotate returned {rotated.status_code}")
            expect(rotated.json()["secret"] == temporary, "rotate did not use the given secret")
            old = self.post_event(self.payload())
            expect(old.status_code == 202, f"old secret in overlap returned {old.status_code}")
            new = self.post_event(self.payload(), secret=temporary)
            expect(new.status_code == 202, f"new secret returned {new.status_code}")
            restored = self.api.post(
                f"/sources/{self.source}/rotate",
                headers=self.admin,
                json={"secret": self.secret, "overlap_seconds": 120},
            )
            expect(restored.status_code == 200, "rotating back failed")
            back = self.post_event(self.payload())
            expect(back.status_code == 202, f"restored secret returned {back.status_code}")
            listing = self.api.get("/sources", headers=self.admin).json()
            entry = next(s for s in listing["items"] if s["source"] == self.source)
            expect(entry["secret_from"] == "database", "source not marked as rotated")
            return "old and new accepted in overlap, rotated back"

        def metrics() -> str:
            text = self.api.get("/metrics").text
            for name in ("launchbridge_events_received_total", "launchbridge_deliveries"):
                expect(name in text, f"{name} missing from /metrics")
            return "prometheus series present"

        self.check("health endpoint", healthz)
        self.check("readiness endpoint (database)", readyz)
        self.check("signed event accepted", accepted)
        self.check("event delivered to destination", delivered)
        self.check("receiver verified outbound signature", receiver_saw_it, needs_receiver=True)
        self.check("duplicate event deduplicated", deduplicated)
        self.check("wrong secret rejected", wrong_secret)
        self.check("stale timestamp rejected", stale_timestamp)
        self.check("replayed signature rejected", replayed_signature)
        self.check("admin endpoints require API key", admin_auth)
        self.check("bounded retries end in failed", bounded_retries, needs_receiver=True)
        self.check("replay after fix delivers", replay_after_fix, needs_receiver=True)
        self.check("bulk replay by source and since", bulk_replay, needs_receiver=True)
        self.check("secret rotation keeps the old secret in the overlap", secret_rotation)
        self.check("metrics endpoint", metrics)
        self.clear_rules()
        return self.results


def summarize(results: list[Check]) -> tuple[int, int, int]:
    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    skipped = sum(1 for r in results if r.passed is None)
    return passed, failed, skipped


def report(
    api: httpx.Client,
    api_key: str,
    results: list[Check],
    *,
    base_url: str | None = None,
    duration_ms: int | None = None,
) -> str:
    """Post the result to `/ops/smoke` so the ops overview knows when the suite last ran.

    Best effort: a deployment that rejects the admin key still gets a full smoke report on
    stdout and the exit code the checks earned.
    """
    passed, failed, skipped = summarize(results)
    version = None
    with contextlib.suppress(httpx.HTTPError, ValueError):
        version = api.get("/healthz").json().get("version")
    body = {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "version": version,
        "base_url": base_url,
        "duration_ms": duration_ms,
    }
    try:
        response = api.post("/ops/smoke", json=body, headers={"X-API-Key": api_key})
    except httpx.HTTPError as exc:
        return f"not reported ({type(exc).__name__})"
    if response.status_code != 201:
        return f"not reported (HTTP {response.status_code})"
    return f"reported as {response.json()['status']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8080"))
    parser.add_argument("--receiver-url", default=os.environ.get("RECEIVER_URL") or None)
    parser.add_argument(
        "--receiver-header",
        action="append",
        metavar="NAME=VALUE",
        help="header sent with every receiver request; repeatable (env RECEIVER_HEADERS)",
    )
    parser.add_argument("--source", default=os.environ.get("SMOKE_SOURCE", "smoke"))
    parser.add_argument("--secret", default=os.environ.get("SMOKE_SECRET", "smoke-dev-secret"))
    parser.add_argument("--api-key", default=os.environ.get("ADMIN_API_KEY", "dev-admin-key"))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("SMOKE_TIMEOUT", 60)))
    args = parser.parse_args(argv)

    configured = os.environ.get("RECEIVER_HEADERS")
    header_values = args.receiver_header or ([configured] if configured else [])
    receiver_headers = parse_receiver_headers(header_values)

    api = httpx.Client(base_url=args.base_url, timeout=30)
    receiver = (
        httpx.Client(base_url=args.receiver_url, timeout=30, headers=receiver_headers)
        if args.receiver_url
        else None
    )
    routed = f" via {', '.join(receiver_headers)}" if receiver_headers else ""
    print(f"smoke: {args.base_url} (receiver: {args.receiver_url or 'none'}{routed})")
    started = time.perf_counter()
    results = Smoke(
        api,
        receiver,
        source=args.source,
        secret=args.secret,
        api_key=args.api_key,
        timeout=args.timeout,
    ).run()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    passed, failed, skipped = summarize(results)
    status = report(api, args.api_key, results, base_url=args.base_url, duration_ms=elapsed_ms)
    print(
        f"smoke: {passed} passed, {failed} failed, {skipped} skipped in {elapsed_ms} ms ({status})"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
