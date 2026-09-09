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
window closes. Inside the window the nonce store closes the gap: every accepted signature
is inserted into `signature_nonces` (`INSERT ... ON CONFLICT DO NOTHING RETURNING`) before
anything else is recorded, and a signature that was seen before is rejected with `409`
whether its first arrival was accepted or deduplicated. The worker deletes nonces older than
twice the tolerance window during maintenance; by then the timestamp check alone rejects
them. A legitimate retry from the source uses a fresh timestamp, so it produces a new
signature and lands in the dedup path instead. Every rejection is written to
`signature_rejections` and counted in Prometheus.

### Secret rotation

`LAUNCHBRIDGE_WEBHOOK_SECRETS` bootstraps sources. `POST /sources/{source}/rotate` moves a
source into `source_secrets`: the current secret becomes `previous_secret` with
`previous_expires_at = now + overlap`, and a new secret (generated, or supplied in the body)
becomes current. Verification (`verify_signature_any`) tries every live candidate so both
keys work during the overlap, records which one matched in
`launchbridge_signatures_verified_total{key}`, and drops the previous key the moment its
expiry passes. Rotating again inside the window replaces the previous secret, so at most two
are ever live. The new secret is returned once by the rotate call; `GET /sources` shows
rotation state without secrets.

Outbound rotation is configuration driven: set `secret` to the new key and
`previous_secret` to the old one in `destinations.yaml`. The worker then sends `X-Signature`
(new) and `X-Signature-Previous` (old) with an optional `X-Key-Id`, so a receiver that has not
switched yet keeps verifying. Remove `previous_secret` once every receiver has the new key.

## Deduplication

The event key is, in order of preference, the `X-Event-Id` header, the payload's `id` field,
or `hash:<sha256 of body>`. Explicit IDs use `id:<value>`; if this would exceed the
255-character storage limit, the key is `id-hash:<sha256 of the full id:<value> string>`.
Distinct long IDs therefore retain their identity. For ledger entries from older releases
that truncated IDs, ingestion compares the full ID in the original event before treating
a request as a duplicate. Each accepted request is stored in `events` (raw body, parsed
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

## Routing and transforms

Which destinations an accepted event fans out to is decided at ingest time
(`launchbridge/routing.py`). A destination matches when all three hold:

1. `sources` contains the source or `"*"`.
2. `event_types` is empty, or the payload's event type (the field named by the registry's
   `event_type_field`, `type` by default) matches one of the globs (`order.*`).
3. Every `when` predicate holds. Predicates are `{field, op, value}` with dotted field paths
   (`customer.address.country`) and operators `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`,
   `not_in`, `exists` and `matches` (regular expression). A missing field only satisfies
   `ne`, `not_in` and `exists: false`.

Each decision carries a reason (`matched`, `source 'shop' not in ['orders']`,
`predicate failed: amount gte 100`), which is what `POST /dry-run/{source}` reports. The
stored event is always the raw request; routing never mutates it.

Transforms (`launchbridge/transform.py`) shape the `payload` inside the outbound envelope
per destination and run in a fixed order: `pick` keeps only the listed top-level keys,
`drop` removes keys, `rename` moves values to new keys, and `set` adds fields rendered from
templates. Templates see `{source}`, `{event_id}`, `{event_key}`, `{received_at}` and
`{payload.<dotted.path>}` (always the original payload, not the partially transformed one).
A value that is exactly one placeholder keeps the referenced JSON type; placeholders inside
longer text render as strings, with missing paths rendering empty. Non-object payloads pass
through untouched. Transforms are pure functions of configuration and the stored event, so
retries and replays keep producing byte-identical envelopes and the idempotency key stays
meaningful.

The dry-run endpoint takes the same body a source would send, runs the same `decide` and
`render_payload` code paths the API and worker use, and records nothing: the test suite
asserts that its output equals what the receiver actually gets.

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

## Rate limits and circuit breakers

Every destination may carry a `rate_limit` (token bucket: `rate` per second, `burst` stored)
and a `circuit_breaker` (`failure_threshold` consecutive transient failures,
`recovery_seconds`, `half_open_max` probes). The worker keeps one gate per destination
(`launchbridge/gating.py`) and consults it after claiming a delivery, before the HTTP call:

- Bucket empty: the delivery goes back to `pending` with `next_attempt_at` set to when the
  next token arrives. No attempt is recorded and `attempts` is unchanged.
- Circuit open: back to `pending` with `next_attempt_at = opened_at + recovery_seconds`.
- Otherwise a token is taken and the request goes out.

Deferred deliveries therefore queue in the database instead of failing or burning retry
budget; the queue is visible as the pending count per destination on `GET /destinations` and
as `launchbridge_deliveries_deferred_total{reason}`. Because deferral only touches
`next_attempt_at`, the claim query's ordering keeps them behind whatever is due sooner.

The breaker (`launchbridge/breaker.py`) counts consecutive transient outcomes (5xx, 408,
425, 429, transport errors, timeouts). Permanent 4xx responses are the payload's fault, not
the destination's, and do not count. After `failure_threshold` the circuit opens; after
`recovery_seconds` the next claimed delivery is a half-open probe (up to `half_open_max` in
flight). A successful probe closes the circuit and the queue drains; a failed probe reopens it
with a fresh recovery window. Transitions are logged, counted in
`launchbridge_circuit_transitions_total` and exposed as `launchbridge_circuit_state` (0
closed, 1 half open, 2 open).

State is written to `destination_states` on every outcome so that a restarted worker resumes
with the circuit in the state it left, and the API can report it without talking to the
worker. Several worker replicas share that table but count failures independently; the rate
limit is likewise per worker process, so size `rate` by replica count.

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
