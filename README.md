# LaunchBridge

Integration service built on FastAPI and PostgreSQL: signed inbound webhooks, database-backed
deduplication, routing rules and per-destination payload transforms, outbound delivery with
bounded retries behind per-destination rate limits and circuit breakers, replay of failed
events, secret rotation with an overlap window, self-service source onboarding, an operations
overview and delivery search, Docker builds, and Terraform for AWS with a smoke suite that
runs against any base URL.

```
 source ---HMAC signed POST /webhooks/{source}---> API
                                                    |  verify signature (current or previous secret)
                                                    |  timestamp window + nonce store
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

Output of `make demo` against the compose stack, pasted as it printed. The header names the
build and the machine; every number under it is read back from `/stats` and `/ops/overview`
after the run, and the `check` lines are assertions the script makes on those numbers.

```
== LaunchBridge demo summary ==
run started (UTC):      2026-09-15T19:13:09+00:00
build:                  version 5.0.0, GIT_SHA a924dcd
driver:                 macOS-26.0.1-arm64-arm-64bit, 10 cpus
target:                 http://localhost:8080
events received:        300
  unique accepted:      250
  deduplicated:         50   (duplicates sent: 50)
deliveries delivered:   250   (230 first pass + 20 after replay)
deliveries retried:     60   (attempts beyond the first)
deliveries failed:      20   (hard failures injected: 20)
replayed after fix:     20   -> delivered 20, still failed 0
signature rejections:   3   (sent: wrong secret, stale timestamp, replayed signature)
dispatch latency:       p50 5184.0 ms   p95 7328.6 ms
ingest rate:            108 events/s   (300 posts in 2.79s of request time, 16 client threads)
smoke checks passed:    15/15
ops overview:           queue depth 0   failed 0   breakers open 0   sources 2
last replay:            bulk by dev -> delivered
smoke status:           green (15/15 checks, 7346 ms)
check ok   deduplicated == duplicates
check ok   failed before replay == hard failures
check ok   replayed == hard failures
check ok   all replays delivered
check ok   nothing left failed
check ok   signature rejections == bad requests
check ok   smoke suite green
check ok   overview agrees with stats
```

The burst sends 250 unique events (20 tagged so the receiver answers 400, 30 tagged so it
answers 503 twice before succeeding) and 50 re-sends of already accepted events with fresh
signatures. Dispatch latency is measured from the moment a delivery row is created, so it
counts the time a delivery waits in the queue while one worker drains the whole burst: that
queue wait, not the HTTP call, is most of the p50 above. The smoke run against the same
stack reports a single delivery arriving in 178 ms. The p95 additionally covers the 30 flaky
deliveries waiting out two backoff steps. The ingest rate counts the posts only, with the
deliberate pause between the two passes left out. This machine was running other work at the
time, so every duration here moves with how busy it is. The last three summary lines are
read back from `/ops/overview`, and the last check compares it against `/stats`.

## What `make smoke` prints

```
smoke: http://localhost:8080 (receiver: http://localhost:8081)
[PASS] health endpoint  (version 5.0.0)
[PASS] readiness endpoint (database)  (database ok)
[PASS] signed event accepted  (event 8a8b0fb3-eff8-44fc-abd6-70c8f5f2a84d with 1 deliveries)
[PASS] event delivered to destination  (crm in 178 ms)
[PASS] receiver verified outbound signature  (signature valid, seen once)
[PASS] duplicate event deduplicated  (deduplicated: true, no deliveries)
[PASS] wrong secret rejected  (invalid_signature)
[PASS] stale timestamp rejected  (stale_timestamp)
[PASS] replayed signature rejected  (replayed_signature)
[PASS] admin endpoints require API key  (401 without key, 200 with key)
[PASS] bounded retries end in failed  (failed after 4/4 attempts)
[PASS] replay after fix delivers  (same idempotency key, receiver count 5)
[PASS] bulk replay by source and since  (replayed 1, all delivered)
[PASS] secret rotation keeps the old secret in the overlap  (old and new accepted in overlap, rotated back)
[PASS] metrics endpoint  (prometheus series present)
smoke: 15 passed, 0 failed, 0 skipped in 7151 ms (reported as green, build a924dcd)
```

The same suite with `SMOKE_ARGS=--read-only`, which leaves the target's secrets and replay
history alone:

```
smoke: 12 passed, 0 failed, 3 skipped in 5735 ms (reported as green, build a924dcd)
```

`make smoke` targets the local compose stack. For anywhere else:

```
make smoke-remote BASE_URL=https://your-host SMOKE_SOURCE=smoke SMOKE_SECRET=... ADMIN_API_KEY=...
```

`smoke-remote` leaves `RECEIVER_URL` empty unless it is given, so the four checks that need
failure injection are reported as SKIP and the exit code reflects the rest. In the Terraform
trial the receiver fake answers on the same ALB as the API behind an `X-Target: receiver`
rule (`deploy/terraform/alb.tf`), so both URLs are the load balancer and the routing header
is passed through to every receiver request:

```
make smoke-remote BASE_URL=https://alb-host RECEIVER_URL=https://alb-host \
     RECEIVER_HEADERS=X-Target=receiver SMOKE_SECRET=... ADMIN_API_KEY=...
```

The totals are posted to `/ops/smoke`, so `GET /ops/overview` afterwards says when that
deployment was last checked, which build answered and whether it came back green.

The suite writes to whatever it checks: it posts events to the smoke source (creating
events, deliveries and attempts), adds and clears failure-injection rules on the receiver
fake, replays the deliveries it failed on purpose, rotates the smoke source's secret to a
temporary value and back with a 120 second overlap, and records its own totals. Give it a
dedicated `smoke` source rather than one that carries real traffic, or add
`SMOKE_ARGS=--read-only` to skip the rotation and replay checks, which are then reported as
SKIP.

## Browser console

`web/` holds a browser port of the delivery path, in TypeScript and React: signing, the
nonce store, the dedup ledger, the retry policy, the worker, replay, the smoke suite and the
demo burst. Signatures are real HMAC-SHA256 through Web Crypto. The database, the HTTP
transport and the clock are in-memory stand-ins, so the page issues no network requests and
its durations come from a virtual clock advanced by a seeded PRNG rather than from a
measurement. Routing rules, payload transforms, rate limits, circuit breakers, secret
rotation and `/ops/overview` are not ported, and the page says so where it shows numbers.

```
cd web
npm ci
npm run dev        # vite dev server on :5173
npm run build      # typecheck, then the production bundle
npm run selfcheck  # the port's assertions in node; exits non-zero when one fails
```

`npm run selfcheck` is the port's own suite: it drives the accept, reject, dedup, replay and
retry paths, checks the backoff bounds, runs the 14 ported smoke checks and the demo burst,
and prints one line per assertion. In the browser it runs on the dev server or with
`?selfcheck` in the URL, and the footer shows the tally. `make web-ci` runs the same three
commands the `web` CI job does. See [web/README.md](web/README.md).

## API

Inbound (HMAC, no API key):

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/webhooks/{source}` | Headers `X-Timestamp`, `X-Signature: sha256=<hmac>`; optional `X-Event-Id`. `202` new, `200` with `deduplicated: true` for repeats, `401` bad or stale signature, `409` replayed signature (nonce seen before). The current and, during rotation, the previous secret both verify. |

Admin (header `X-API-Key`):

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/dry-run/{source}` | Body is a sample event. Returns the routing decision (with reason) and the transformed payload per destination; records nothing. |
| GET | `/sources` | Sources with rotation state (`secret_from`, `rotated_at`, `previous_expires_at`); never returns secrets. |
| POST | `/sources` | Body `{"source", "secret"?}`. Onboards a source, returns its secret once with the webhook path and ready-to-run signing snippets. `409` if the name is taken. |
| DELETE | `/sources/{source}` | Removes an onboarded source; its webhook path starts answering `404`. Sources that come from the environment return `409`. |
| POST | `/sources/{source}/rotate` | Body `{"secret"?, "overlap_seconds"?}`. Issues a new secret (returned once) and keeps the old one verifying until the overlap ends. |
| GET | `/destinations` | Configured destinations with routing summary, rate limit, breaker config, persisted breaker state and queued (pending) count. |
| GET | `/deliveries?...` | Delivery search; see the filters below. |
| GET | `/deliveries/{id}` | Delivery with its attempt log. |
| POST | `/deliveries/{id}/replay?reason=` | New attempt series for a failed delivery. |
| POST | `/replay?source=&since=&destination=&reason=` | Bulk replay of failed deliveries. |
| GET | `/replays` | Replay audit trail. |
| GET | `/events`, `/events/{id}` | Raw recorded events. |
| GET | `/stats?source=&since=` | Counts and p50/p95 latency from the database. |
| GET | `/ops/overview?since=` | One call for an operations view: counters per source, per-destination queue depth, breaker state and latency, the failed deliveries with their errors, the last replay and the last smoke result. |
| POST | `/ops/smoke` | Body `{"passed", "failed", "skipped"?, "version"?, "base_url"?, "duration_ms"?}`. Records a smoke run; the suite posts this itself when it finishes. |

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
| `LAUNCHBRIDGE_SIGNATURE_TOLERANCE_SECONDS` | 300 | Timestamp window; nonces are kept for twice this. |
| `LAUNCHBRIDGE_SECRET_OVERLAP_SECONDS` | 86400 | How long the previous secret stays valid after a rotation. |
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

### Secret rotation

```
curl -X POST -H "X-API-Key: $KEY" $BASE/sources/orders/rotate \
     -d '{"overlap_seconds": 3600}' -H 'Content-Type: application/json'
```

The response carries the new secret once. Requests signed with the old secret keep working
for `overlap_seconds`, then return `401 invalid_signature`. Outbound keys rotate through
`destinations.yaml`: set `secret` to the new key and `previous_secret` to the old one, and
deliveries carry both `X-Signature` and `X-Signature-Previous` (plus `X-Key-Id` when
`key_id` is set) until you drop `previous_secret`.

### Onboarding a source

```
curl -X POST -H "X-API-Key: $KEY" $BASE/sources \
     -d '{"source": "shop"}' -H 'Content-Type: application/json'
```

The response carries the generated secret once, the path to post to and a Python and a shell
snippet that sign a request with that secret. Pass `secret` to bring your own (16 characters
or more). Names are lowercase alphanumerics, dashes and underscores. `DELETE /sources/shop`
takes the source back out; further posts to `/webhooks/shop` return `404`. Sources configured
through `LAUNCHBRIDGE_WEBHOOK_SECRETS` are the bootstrap and cannot be deleted over the API.

### Delivery search

`GET /deliveries` takes any combination of `status` (comma-separated, so
`status=failed,replayed`), `source`, `destination`, `event_id`, `event_key`,
`idempotency_key`, `status_code`, `replayed` (true for replays only, false for originals),
`q` (substring of the last error or the event key), `since`, `until`, `order` (`asc` or
`desc`), `limit` and `offset`. Filters combine with AND, `count` is the size of the whole
result rather than the page, and rows are ordered by creation time with the delivery id
breaking ties so paging stays stable.

```
curl -H "X-API-Key: $KEY" "$BASE/deliveries?status=failed&destination=crm&q=timeout&limit=20"
```

### Operations overview

`GET /ops/overview` answers the questions an on-call rotation asks first, in one round trip:
which sources are sending and how much of it was deduplicated or rejected, how deep each
destination queue is, which breakers are open, what is sitting in `failed` and why, when the
last replay ran and how it ended, and whether the last smoke run was green.

```
curl -H "X-API-Key: $KEY" "$BASE/ops/overview?since=2026-01-01T00:00:00Z" | jq .totals
```

`since` narrows the event, delivery and replay figures; `smoke` is always the newest reported
run. `make smoke` posts its own result to `/ops/smoke` at the end, so the overview shows when
the suite last ran, against which base URL and how long it took.

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

What the trial defaults leave out, and would need changing before this carried traffic:

- The ALB listener is plain HTTP on port 80: no ACM certificate, no redirect to HTTPS.
- `RECEIVER_SECRETS` reaches the receiver task as plain task environment, not as a Secrets
  Manager reference like the database URL, inbound secrets and admin keys.
- RDS is single AZ with `deletion_protection = false` and `skip_final_snapshot = true`.
- The receiver fake is reachable from outside the VPC through the `X-Target: receiver`
  listener rule. Set `deploy_receiver_fake = false` for a real integration.
- The worker serves its metrics on port 9100 inside the task, but nothing scrapes it: there
  is no service discovery entry and no Prometheus in this stack.

## CI

`.github/workflows/ci.yml` defines the checks: ruff, pytest against a PostgreSQL service
container, an image build tagged with the commit sha followed by a container start and a
`/healthz` probe, `terraform fmt -check` plus `validate`, and the browser console's
typecheck, bundle and self-check. Dependencies install from the lockfile
(`uv sync --locked`, `npm ci`).

GitHub Actions has recorded no run for this repository, so nothing here rests on a green
badge. What has run is local. `make ci` passes at commit `27fb461` on 2026-09-15: ruff
clean, 162 tests, the image build tagged `launchbridge:27fb461`, terraform reporting the
configuration valid, and the console's typecheck, bundle and 25 self-check assertions. The
smoke and demo transcripts above come from the compose stack at commit `a924dcd` the same
day, which is the commit their `GIT_SHA` line names.

## Releases

Each version is an annotated git tag and the notes for it are the changelog entry below; no
GitHub Release objects are attached to the tags.

| Version | Tag | Headline | Tests |
| --- | --- | --- | --- |
| 5.0.0 | [v5.0.0](https://github.com/SAY-5/launchbridge/releases/tag/v5.0.0) | Source onboarding and removal, `/ops/overview`, delivery search | 141 |
| 4.0.0 | [v4.0.0](https://github.com/SAY-5/launchbridge/releases/tag/v4.0.0) | Per-source secret rotation, nonce store, outbound key rotation | 128 |
| 3.0.0 | [v3.0.0](https://github.com/SAY-5/launchbridge/releases/tag/v3.0.0) | Rate limits, circuit breakers, persisted breaker state | 119 |
| 2.0.0 | [v2.0.0](https://github.com/SAY-5/launchbridge/releases/tag/v2.0.0) | Routing rules, payload transforms, dry run | 104 |
| 1.0.0 | [v1.0.0](https://github.com/SAY-5/launchbridge/releases/tag/v1.0.0) | Signed webhooks, dedup, delivery worker, replay, smoke suite | 80 |

## Changelog

### 5.0.0

- Self-service source onboarding: `POST /sources` creates a source, returns its secret once
  with the webhook path and signing snippets, and the source can post immediately.
  `DELETE /sources/{source}` takes it back out; environment sources stay read-only.
- `GET /ops/overview` answers the on-call questions in one call: counters per source,
  per-destination queue depth, breaker state and latency, the failed deliveries with their
  errors, the last replay and its outcome, and the last smoke result.
- Delivery search on `/deliveries`: status lists, event key, idempotency key, status code,
  replays only, error substring, `since`/`until` window, ascending or descending order and
  stable paging.
- Smoke runs report themselves to `POST /ops/smoke` (`smoke_runs`, Alembic `0004`), so a
  deployment can be asked when its suite last ran and whether it was green. 141 tests.

### 4.0.0

- Secret rotation per source: `POST /sources/{source}/rotate` issues a new secret and keeps
  the previous one verifying until an overlap window closes; `GET /sources` shows rotation
  state. Environment secrets remain the bootstrap.
- Nonce store (`signature_nonces`, Alembic `0003`) rejects a replayed signature even when the
  first arrival was deduplicated; the worker expires nonces after twice the timestamp window.
- Outbound key rotation: `previous_secret` and `key_id` per destination add
  `X-Signature-Previous` and `X-Key-Id` to deliveries; the receiver fake accepts either.
- Smoke suite gains a rotation check (15 checks). 128 tests.

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
                signing.py, secrets.py, destinations.py, models.py, alembic/
fakes/          receiver fake with inbox and failure injection
smoke/          smoke suite (python -m smoke.smoke --base-url ...)
scripts/        demo burst
deploy/terraform/
tests/          pytest suite
web/            browser console: a TypeScript port of the delivery path (see below)
```
