"""Demo: smoke suite plus a 300-event burst with duplicates and injected failures.

Runs against a live stack (see `make demo`). Every number printed is read back from the
service's /stats endpoint or the smoke results; nothing is estimated.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx

from launchbridge.signing import sign_headers
from smoke.smoke import Smoke, summarize

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
RECEIVER_URL = os.environ.get("RECEIVER_URL", "http://localhost:8081")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "dev-admin-key")
DEMO_SOURCE = os.environ.get("DEMO_SOURCE", "demo")
DEMO_SECRET = os.environ.get("DEMO_SECRET", "demo-dev-secret")
SMOKE_SOURCE = os.environ.get("SMOKE_SOURCE", "smoke")
SMOKE_SECRET = os.environ.get("SMOKE_SECRET", "smoke-dev-secret")

TOTAL_EVENTS = 300
DUPLICATES = 50
HARD_FAILURES = 20
FLAKY = 30
FLAKY_FAILURES_EACH = 2
BAD_SIGNATURES = 3
SETTLE_TIMEOUT = float(os.environ.get("DEMO_TIMEOUT", 180))


def post(
    api: httpx.Client, payload: dict, *, secret: str = DEMO_SECRET, timestamp: int | None = None
):
    body = json.dumps(payload).encode()
    headers = sign_headers(secret, body, timestamp)
    headers["Content-Type"] = "application/json"
    return api.post(f"/webhooks/{DEMO_SOURCE}", content=body, headers=headers)


def wait_until_settled(api: httpx.Client, admin: dict, since: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    stats: dict = {}
    while time.monotonic() < deadline:
        stats = api.get(
            "/stats", params={"source": DEMO_SOURCE, "since": since}, headers=admin
        ).json()
        open_count = stats["deliveries"]["pending"] + stats["deliveries"]["in_progress"]
        if open_count == 0:
            return stats
        time.sleep(0.5)
    raise SystemExit(f"deliveries did not settle within {timeout}s: {stats['deliveries']}")


def main() -> int:
    api = httpx.Client(base_url=BASE_URL, timeout=30)
    receiver = httpx.Client(base_url=RECEIVER_URL, timeout=30)
    admin = {"X-API-Key": ADMIN_API_KEY}
    run_id = uuid.uuid4().hex[:8]

    print("== smoke suite ==")
    smoke_results = Smoke(
        api, receiver, source=SMOKE_SOURCE, secret=SMOKE_SECRET, api_key=ADMIN_API_KEY
    ).run()
    smoke_passed, smoke_failed, smoke_skipped = summarize(smoke_results)

    print(
        f"\n== burst: {TOTAL_EVENTS} events, {DUPLICATES} duplicates, "
        f"{HARD_FAILURES} hard failures, {FLAKY} flaky x{FLAKY_FAILURES_EACH} =="
    )
    hard_tag = f"hard-{run_id}"
    flaky_tag = f"flaky-{run_id}"
    receiver.delete("/control/rules")
    receiver.post("/control/rules", json={"tag": hard_tag, "status": 400})
    receiver.post(
        "/control/rules", json={"tag": flaky_tag, "status": 503, "times": FLAKY_FAILURES_EACH}
    )

    since = datetime.now(UTC).isoformat()
    unique = TOTAL_EVENTS - DUPLICATES
    payloads = []
    for index in range(unique):
        payload = {"id": f"demo-{run_id}-{index:04d}", "sequence": index, "kind": "burst"}
        if index < HARD_FAILURES:
            payload["tag"] = hard_tag
        elif index < HARD_FAILURES + FLAKY:
            payload["tag"] = flaky_tag
        payloads.append(payload)
    plain = [p for p in payloads if "tag" not in p]
    duplicates = [plain[i % len(plain)] for i in range(DUPLICATES)]

    # The first event is sent by hand so its exact request can be replayed later.
    lead_body = json.dumps(payloads[0]).encode()
    lead_headers = sign_headers(DEMO_SECRET, lead_body)
    lead_headers["Content-Type"] = "application/json"
    started = time.perf_counter()
    first_pass = [
        api.post(f"/webhooks/{DEMO_SOURCE}", content=lead_body, headers=lead_headers).status_code
    ]
    with ThreadPoolExecutor(max_workers=16) as pool:
        first_pass += list(pool.map(lambda p: post(api, p).status_code, payloads[1:]))
    time.sleep(1.05)  # fresh timestamps so duplicates are dedup hits, not signature replays
    with ThreadPoolExecutor(max_workers=16) as pool:
        second_pass = list(pool.map(lambda p: post(api, p).json()["deduplicated"], duplicates))
    ingest_seconds = time.perf_counter() - started

    stale = int(time.time()) - 3600
    rejections = [
        post(api, {"id": f"bad-{run_id}-1"}, secret="wrong-secret").status_code,
        post(api, {"id": f"bad-{run_id}-2"}, timestamp=stale).status_code,
    ]
    rejections.append(
        api.post(f"/webhooks/{DEMO_SOURCE}", content=lead_body, headers=lead_headers).status_code
    )

    print(
        f"posted {len(first_pass)} unique ({first_pass.count(202)} accepted) and "
        f"{len(second_pass)} duplicates ({sum(second_pass)} deduplicated) "
        f"in {ingest_seconds:.1f}s; bad-signature responses {rejections}"
    )
    print("waiting for deliveries to settle ...")
    before = wait_until_settled(api, admin, since, SETTLE_TIMEOUT)
    failed_first_pass = before["deliveries"]["failed"]
    delivered_first_pass = before["deliveries"]["delivered"]

    print("repairing destination and replaying failed deliveries ...")
    receiver.delete("/control/rules")
    replay = api.post(
        "/replay", params={"source": DEMO_SOURCE, "since": since}, headers=admin
    ).json()
    after = wait_until_settled(api, admin, since, SETTLE_TIMEOUT)

    events = after["events"]
    deliveries = after["deliveries"]
    checks = {
        "deduplicated == duplicates": events["deduplicated"] == DUPLICATES,
        "failed before replay == hard failures": failed_first_pass == HARD_FAILURES,
        "replayed == hard failures": replay["replayed"] == HARD_FAILURES,
        "all replays delivered": after["replays"]["delivered"] == replay["replayed"],
        "nothing left failed": deliveries["failed"] == 0,
        "signature rejections == bad requests": after["signature_rejections"] == BAD_SIGNATURES,
        "smoke suite green": smoke_failed == 0,
    }
    latency = after["latency_ms"]
    lines = [
        "== LaunchBridge demo summary ==",
        f"events received:        {events['received']}",
        f"  unique accepted:      {events['accepted']}",
        f"  deduplicated:         {events['deduplicated']}   (duplicates sent: {DUPLICATES})",
        f"deliveries delivered:   {deliveries['delivered']}   "
        f"({delivered_first_pass} first pass + {after['replays']['delivered']} after replay)",
        f"deliveries retried:     {after['retries']}   (attempts beyond the first)",
        f"deliveries failed:      {failed_first_pass}   (hard failures injected: {HARD_FAILURES})",
        f"replayed after fix:     {replay['replayed']}   "
        f"-> delivered {after['replays']['delivered']}, still failed {deliveries['failed']}",
        f"signature rejections:   {after['signature_rejections']}   "
        f"(sent: wrong secret, stale timestamp, replayed signature)",
        f"dispatch latency:       p50 {latency['p50']} ms   p95 {latency['p95']} ms",
        f"smoke checks passed:    {smoke_passed}/{len(smoke_results)}"
        + (f"   ({smoke_skipped} skipped)" if smoke_skipped else ""),
    ]
    lines += [f"check {'ok ' if ok else 'BAD'}  {name}" for name, ok in checks.items()]
    summary = "\n".join(lines)
    print("\n" + summary)
    with open("demo-summary.txt", "w") as handle:
        handle.write(summary + "\n")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
