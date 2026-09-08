# LaunchBridge

Integration service built on FastAPI and PostgreSQL: signed inbound webhooks, database-backed
deduplication, routing rules and per-destination payload transforms, outbound delivery with
bounded retries behind per-destination rate limits and circuit breakers, replay of failed
events, Docker builds, and Terraform for AWS with a smoke suite that runs against any base URL.

```
 source ---HMAC signed POST /webhooks/{source}---> API
                                                    |  verify signature + timestamp window
                                                    |  reject replayed signatures
                                                    |  dedup on (source, event key) in PostgreSQL
                                                    |  route by source, event type, predicates
                                                    v
                                     events / processed_events / deliveries
                                                    |
                              worker <--- claim due deliveries (FOR UPDATE SKIP LOCKED)
                                |  transform payload per destination (pick/drop/rename/set)
                                |  token bucket + circuit breaker gate (defer, never fail)
                                |  sign envelope, X-Idempotency-Key, per-attempt timeout
                                |  exponential backoff + jitter, bounded attempts
                                +---> destination   (delivered | failed -> replay -> delivered)
```

## Quick start

Requirements: Docker, `uv`, Python 3.12, Terraform 1.5 (only for `make tf-*`).

```
make install     # dependencies
make test        # pytest: unit, PostgreSQL (Testcontainers) and in-process smoke
make up          # build image tagged by git sha, start api + worker + postgres + receiver fake
make smoke       # end-to-end checks against the stack (BASE_URL=... for any deployment)
make demo        # smoke suite + 300-event burst, prints the summary below
make down        # stop the stack
```

The compose stack listens on `:8080` (API, docs at `/docs`), `:8081` (receiver fake) and
`:9100` (worker metrics). Override with `API_PORT=18080 make up` if 8080 is taken.

## What `make demo` prints

Output of a run against the compose stack on this machine. Every number is read back from
`/stats` after the run; the `check` lines are assertions the script makes on those numbers.

```
== LaunchBridge demo summary ==
events received:        300
  unique accepted:      250
  deduplicated:         50   (duplicates sent: 50)
deliveries delivered:   250   (230 first pass + 20 after replay)
deliveries retried:     60   (attempts beyond the first)
deliveries failed:      20   (hard failures injected: 20)
replayed after fix:     20   -> delivered 20, still failed 0
signature rejections:   3   (sent: wrong secret, stale timestamp, replayed signature)
dispatch latency:       p50 95.0 ms   p95 1793.1 ms
smoke checks passed:    14/14
check ok   deduplicated == duplicates
check ok   failed before replay == hard failures
check ok   replayed == hard failures
check ok   all replays delivered
check ok   nothing left failed
check ok   signature rejections == bad requests
check ok   smoke suite green
```

The burst sends 250 unique events (20 tagged so the receiver answers 400, 30 tagged so it
answers 503 twice before succeeding) and 50 re-sends of already accepted events with fresh
signatures. The p95 latency reflects the 30 flaky deliveries waiting out two backoff steps.

## What `make smoke` prints

```
[PASS] health endpoint  (version 3.0.0)
[PASS] readiness endpoint (database)  (database ok)
[PASS] signed event accepted  (event 95d4c6ad-a19e-44b1-8431-80259f99030d with 1 deliveries)
[PASS] event delivered to destination  (crm in 111 ms)
[PASS] receiver verified outbound signature  (signature valid, seen once)
[PASS] duplicate event deduplicated  (deduplicated: true, no deliveries)
[PASS] wrong secret rejected  (invalid_signature)
[PASS] stale timestamp rejected  (stale_timestamp)
[PASS] replayed signature rejected  (replayed_signature)
[PASS] admin endpoints require API key  (401 without key, 200 with key)
[PASS] bounded retries end in failed  (failed after 4/4 attempts)
[PASS] replay after fix delivers  (same idempotency key, receiver count 5)
[PASS] bulk replay by source and since  (replayed 1, all delivered)
[PASS] metrics endpoint  (prometheus series present)
smoke: 14 passed, 0 failed, 0 skipped
```

`make smoke BASE_URL=https://your-host RECEIVER_URL=... SMOKE_SECRET=... ADMIN_API_KEY=...`
runs the same checks against any deployment. Without `RECEIVER_URL` the four checks that
need failure injection are reported as SKIP and the exit code still reflects the rest.

## API

Inbound (HMAC, no API key):

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/webhooks/{source}` | Headers `X-Timestamp`, `X-Signature: sha256=<hmac>`; optional `X-Event-Id`. `202` new, `200` with `deduplicated: true` for repeats, `401` bad or stale signature, `409` replayed signature. |

Admin (header `X-API-Key`):

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/dry-run/{source}` | Body is a sample event. Returns the routing decision (with reason) and the transformed payload per destination; records nothing. |
| GET | `/destinations` | Configured destinations with routing summary, rate limit, breaker config, persisted breaker state and queued (pending) count. |
| GET | `/deliveries?status=&source=&destination=&event_id=&since=` | Paginated delivery list. |
| GET | `/deliveries/{id}` | Delivery with its attempt log. |
| POST | `/deliveries/{id}/replay?reason=` | New attempt series for a failed delivery. |
| POST | `/replay?source=&since=&destination=&reason=` | Bulk replay of failed deliveries. |
| GET | `/replays` | Replay audit trail. |
| GET | `/events`, `/events/{id}` | Raw recorded events. |
| GET | `/stats?source=&since=` | Counts and p50/p95 latency from the database. |

Operations (public): `GET /healthz`, `GET /readyz` (database probe), `GET /metrics`
(Prometheus), `GET /docs` (OpenAPI).

Signing an inbound request:

```python
import hashlib, hmac, json, time

body = json.dumps({"id": "order-1", "amount": 42}).encode()
ts = str(int(time.time()))
message = f"{ts}.".encode() + body
sig = "sha256=" + hmac.new(b"orders-dev-secret", message, hashlib.sha256).hexdigest()
# POST /webhooks/orders with X-Timestamp: ts, X-Signature: sig
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LAUNCHBRIDGE_DATABASE_URL` | local postgres | SQLAlchemy URL (`postgresql://` is upgraded to `postgresql+psycopg://`). |
| `LAUNCHBRIDGE_WEBHOOK_SECRETS` | empty | `source=secret,...` or JSON object. |
| `LAUNCHBRIDGE_ADMIN_API_KEYS` | empty | `label=key,...` or JSON; the label is recorded as the replay actor. |
| `LAUNCHBRIDGE_SIGNATURE_TOLERANCE_SECONDS` | 300 | Timestamp window. |
| `LAUNCHBRIDGE_DESTINATIONS_FILE` | `destinations.yaml` | Destinations and retry policies. |
| `LAUNCHBRIDGE_PROCESSED_EVENTS_TTL_HOURS` | 72 | Dedup ledger retention. |
| `LAUNCHBRIDGE_WORKER_CONCURRENCY` | 8 | Parallel deliveries per worker batch. |
| `LAUNCHBRIDGE_WORKER_METRICS_PORT` | 0 (off) | Worker Prometheus port. |

`destinations.yaml` entries take `name`, `url`, `secret`, routing fields and a `retry` block
(`max_attempts`, `base_delay_seconds`, `max_delay_seconds`, `multiplier`, `jitter`,
`timeout_seconds`). See [ARCHITECTURE.md](ARCHITECTURE.md) for the signing, dedup, routing,
retry and replay design.

### Routing rules and transforms

```yaml
event_type_field: type            # payload field holding the event type
destinations:
  - name: billing
    url: https://billing.example/hooks
    secret: ${BILLING_SECRET}
    sources: ["orders"]           # or ["*"]
    event_types: ["order.*"]      # globs against the event type
    when:                         # every predicate must hold
      - {field: amount, op: gte, value: 100}
      - {field: customer.country, op: in, value: [DE, FR]}
    transform:                    # pick, drop, rename, then set
      drop: [internal_notes]
      rename: {amount: total}
      set:
        channel: "{source}"
        label: "{source}:{payload.type}"
        reference: "{event_key}"
```

### Rate limits and circuit breakers

```yaml
    rate_limit: {rate: 20, burst: 50}                    # per second, per worker process
    circuit_breaker: {failure_threshold: 5, recovery_seconds: 30, half_open_max: 1}
```

Both are optional. A delivery that hits an empty bucket or an open circuit is put back in the
queue with a future `next_attempt_at`; no attempt is spent and nothing fails. The breaker
opens after `failure_threshold` consecutive transient failures (5xx, 429, timeouts), lets
`half_open_max` probes through after `recovery_seconds`, and closes on a successful probe.
State is persisted in `destination_states` and shown by `GET /destinations` and the
`launchbridge_circuit_state` gauge.

Predicate operators: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `exists`,
`matches`. Templates reference `{source}`, `{event_id}`, `{event_key}`, `{received_at}` and
`{payload.<path>}`; a value that is exactly one placeholder keeps the source type.
`POST /dry-run/orders` with a sample body shows, per destination, whether it would be routed,
why not, and the payload it would receive.

## Deployment on AWS

`deploy/terraform` provisions a VPC, ALB, ECS Fargate services (`api`, `worker`, optional
`receiver` fake), RDS PostgreSQL 16, an ECR repository and Secrets Manager entries; task
definitions read secrets at start, and a `migrate` task definition runs Alembic per release.

```
make build && docker tag launchbridge:<sha> <account>.dkr.ecr.<region>.amazonaws.com/launchbridge:<sha>
docker push ...
cp deploy/terraform/terraform.tfvars.example deploy/terraform/terraform.tfvars   # fill in values
make tf-validate
make tf-plan            # needs AWS credentials
terraform -chdir=deploy/terraform apply
aws ecs run-task --cluster launchbridge-trial --task-definition launchbridge-migrate ...
make smoke BASE_URL=$(terraform -chdir=deploy/terraform output -raw base_url) ...
```

No AWS account was available while building this project. The Terraform is `fmt` and
`validate` clean and mirrors the compose topology, but `plan` and `apply` require credentials
and have not been run; the smoke and demo results above come from the compose stack. The
smoke suite is written to be the acceptance check for the ECS deployment once it exists.

## CI

`.github/workflows/ci.yml` runs ruff, pytest against a PostgreSQL service container, a Docker
build tagged with the commit sha followed by a container start and `/healthz` probe, and
`terraform fmt -check` plus `validate`. `make ci` runs the same steps locally.

## Changelog

### 3.0.0

- Token-bucket rate limit per destination (`rate_limit`) and a circuit breaker
  (`circuit_breaker`) with closed, open and half-open states; deliveries held back by either
  are deferred in the queue, never failed, and drain once the destination recovers.
- Breaker state persisted in `destination_states` (Alembic `0002`) so restarts and the API
  see the same circuit; `launchbridge_circuit_state`, `launchbridge_circuit_transitions_total`
  and `launchbridge_deliveries_deferred_total` metrics.
- `GET /destinations` lists configuration, breaker state and queue depth per destination.
- 119 tests.

### 2.0.0

- Routing rules per destination: `sources`, `event_types` globs and `when` predicates on
  payload fields, evaluated at ingest with a recorded reason per decision.
- Payload transforms per destination (`pick`, `drop`, `rename`, templated `set`) applied to
  the outbound envelope; stored events stay raw.
- `POST /dry-run/{source}` previews routing and rendered payloads for a sample event without
  recording anything.
- 104 tests.

### 1.0.0

- Signed inbound webhooks, PostgreSQL dedup ledger, delivery worker with bounded retries,
  replay, smoke suite, demo burst, compose stack and Terraform for ECS Fargate with RDS.
- 80 tests.

## Layout

```
launchbridge/   API (app.py, api.py), ingest.py, routing.py, transform.py, replay.py,
                stats.py, worker.py, retry.py, ratelimit.py, breaker.py, gating.py,
                signing.py, destinations.py, models.py, alembic/
fakes/          receiver fake with inbox and failure injection
smoke/          smoke suite (python -m smoke.smoke --base-url ...)
scripts/        demo burst
deploy/terraform/
tests/          pytest suite
```
