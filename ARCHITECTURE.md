# Architecture

LaunchBridge accepts signed webhooks from named sources, records them once, and delivers
them to configured destinations with bounded retries. Failed deliveries can be replayed
without the destination seeing a new identity. Two processes share one PostgreSQL database:
the API (FastAPI) and the worker.

```
 source ---signed POST---> API ---> events / processed_events / deliveries (PostgreSQL)
                                                              |
                            worker <--- claim due deliveries (FOR UPDATE SKIP LOCKED)
                              |
                              +---signed POST, X-Idempotency-Key---> destination
```

## Inbound signing

Every request to `POST /webhooks/{source}` must carry `X-Timestamp` (unix seconds) and
`X-Signature: sha256=<hex>`. The signature is HMAC-SHA256 over `"<timestamp>." + body`
with the per-source secret from `LAUNCHBRIDGE_WEBHOOK_SECRETS`. Verification
(`launchbridge/signing.py`):

1. Timestamp present and integer, otherwise `401 missing_timestamp` / `invalid_timestamp`.
2. `|now - timestamp| <= LAUNCHBRIDGE_SIGNATURE_TOLERANCE_SECONDS` (default 300),
   otherwise `401 stale_timestamp`.
3. Constant-time comparison of the expected digest, otherwise `401 invalid_signature`.
4. The signature itself must not have been accepted before, otherwise `409 replayed_signature`.

Binding the timestamp into the digest means a captured request cannot be re-sent after the
window closes, and the signature uniqueness check (a unique constraint on
`processed_events.signature`) catches re-sends inside the window. A legitimate retry from the
source uses a fresh timestamp, so it produces a new signature and lands in the dedup path
instead. Every rejection is written to `signature_rejections` and counted in Prometheus.

## Deduplication

The event key is, in order of preference, the `X-Event-Id` header, the payload's `id` field,
or `hash:<sha256 of body>`. Each accepted request is stored in `events` (raw body, parsed
payload, recorded headers). The dedup ledger is `processed_events` with
`UNIQUE (source, event_key)`; the API inserts with `ON CONFLICT DO NOTHING ... RETURNING id`,
so two concurrent copies of the same event race safely in the database rather than in Python.

- First arrival: ledger row inserted, one `deliveries` row per matching destination, `202`
  with `deduplicated: false`.
- Later arrivals: the event row is kept with `status = deduplicated` for audit, no deliveries
  are created, `200` with `deduplicated: true` and an empty `delivery_ids`.

The worker deletes ledger rows older than `LAUNCHBRIDGE_PROCESSED_EVENTS_TTL_HOURS`
(default 72) during its maintenance pass, so the unique index stays bounded. An event that
reappears after the TTL is treated as new; set the TTL to cover the longest retry horizon of
any source.

## Destinations and outbound signing

`destinations.yaml` lists destinations with a URL, an outbound secret, the sources they
accept (`"*"` or a list) and a retry policy. `${VAR:-default}` references are expanded from
the environment so the same file works in compose and on ECS, where the secrets come from
Secrets Manager.

Outbound requests carry a canonical JSON envelope (`event_id`, `source`, `event_key`,
`received_at`, `destination`, `payload`) serialised with sorted keys, plus:

- `X-Timestamp` and `X-Signature`, the same scheme as inbound, keyed by the destination secret.
- `X-Idempotency-Key`: `<event_id>:<destination>`. It is identical across retries and replays,
  so a destination that stores keys can discard repeats.
- `X-Event-Id`.

## Delivery and retries

`deliveries` rows move through `pending -> in_progress -> delivered | failed`, and
`failed -> replayed` when a replay is issued. The worker loop (`launchbridge/worker.py`):

1. Claims up to `batch_size` due rows with a single
   `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING id`, so several
   worker replicas can run against one database without double delivery.
2. Posts each delivery from a thread pool with the destination's per-attempt timeout.
3. Classifies the result (`launchbridge/retry.py`): 2xx is success; 5xx, 408, 425, 429 and
   transport errors are transient; any other 4xx is permanent.
4. Records a `delivery_attempts` row and updates the delivery:
   - success: `delivered`, `latency_ms` measured from delivery creation;
   - transient and `attempts < max_attempts`: back to `pending` with
     `next_attempt_at = now + backoff(attempt)`;
   - permanent, or transient with attempts exhausted: `failed` with `last_error` and
     `last_status_code` preserved.

Backoff is `min(base * multiplier^(attempt-1), max_delay)` with symmetric jitter, never above
`max_delay`. Rows stuck `in_progress` for more than five minutes (a worker that died mid
batch) are returned to `pending` by the maintenance pass.

## Replay

Replay never mutates the failed row's history. `POST /deliveries/{id}/replay` and
`POST /replay?source=&since=&destination=` create a new `deliveries` row with `series + 1`,
`attempts = 0`, `replay_of` pointing at the original and the same idempotency key. The original
becomes `replayed`, and a `replays` row records actor (API key label), mode (single or bulk),
optional reason and time. The new row is picked up by the worker like any other pending
delivery, with the full retry budget.

## Observability

- `GET /healthz`: process liveness. `GET /readyz`: runs `SELECT 1`, returns 503 when the
  database is unreachable; this is the ALB target-group health check.
- `GET /metrics` (API) and port 9100 (worker) expose Prometheus counters for received,
  deduplicated, rejected, delivered, retried, failed and replayed, plus latency and attempt
  duration histograms. The API also reports a `launchbridge_deliveries{status}` gauge read
  from the database at scrape time.
- `GET /stats?source=&since=` returns the same counts aggregated from the database, including
  p50 and p95 delivery latency. The demo prints these numbers.
- Logs are JSON via structlog.

## Deployment

`deploy/terraform` creates a VPC with public, private and database subnets, an ALB, an ECS
Fargate cluster with `api`, `worker` and (optionally) `receiver` services, an RDS PostgreSQL 16
instance, an ECR repository and Secrets Manager entries for the database URL, inbound secrets,
admin keys and destination secrets. Task definitions read secrets through the execution role;
nothing sensitive is baked into the image. A one-off `migrate` task definition runs
`alembic upgrade head` per release. The image is built once, tagged with the git sha and
pushed to ECR; `image_tag` selects it.

The receiver fake is deployed behind service discovery (`receiver.launchbridge.local`) and,
for the trial, reachable through the ALB with an `X-Target: receiver` header so the smoke
suite can drive failure injection. Set `deploy_receiver_fake = false` for a real integration.

## Smoke strategy

`smoke/smoke.py` runs the same checks in every environment: health and readiness, a signed
event delivered end to end with a verified outbound signature, deduplication, the three
rejection paths, admin authentication, bounded retries ending in `failed`, single and bulk
replay after the destination is repaired, and the metrics endpoint. Each check prints PASS,
FAIL or SKIP; the process exits non-zero on any FAIL. Checks that need the receiver control
API are skipped when `RECEIVER_URL` is unset, so the suite still validates a deployment whose
destinations are real systems. The suite is also executed in-process by the test suite
(`tests/test_smoke.py`) with a worker thread and the receiver fake, so a regression is caught
before an image is built.
