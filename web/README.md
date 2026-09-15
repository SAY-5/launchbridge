# LaunchBridge console

A browser port of the LaunchBridge delivery path, in TypeScript and React. It exists so the
parts of the service that are hard to describe in prose can be driven by hand: sign a
request and break it on purpose, watch the dedup ledger short-circuit a repeat, follow a
delivery through its retries and backoffs, replay what failed, and run the ported smoke
suite and demo burst.

## Commands

```
npm ci
npm run dev        # vite dev server on :5173
npm run build      # typecheck (tsc -b), then the production bundle
npm run selfcheck  # the port's assertions in node; exits non-zero when one fails
npm run preview    # serve the built bundle
```

`make web-ci` from the repository root runs `npm ci`, `npm run build` and
`npm run selfcheck`, which is what the `web` job in `.github/workflows/ci.yml` does.

## What is ported, and what is not

Ported from the Python service: HMAC signing and verification (`signing.ts`), the signature
nonce store and dedup ledger (`store.ts`, `ingest.ts`), the retry policy with jittered
backoff (`retry.ts`), the delivery worker and its claim loop (`worker.ts`), replay
(`replay.ts`), the receiver fake with failure injection (`receiver.ts`), the stats
aggregation (`stats.ts`), the smoke suite (`smoke.ts`, 14 of the 15 checks) and the demo
burst (`burst.ts`, 7 of the 8 checks).

Not ported: routing rules and predicates, payload transforms, per-destination rate limits,
circuit breakers, secret rotation, source onboarding, delivery search and `/ops/overview`.
Because transforms are absent, the billing destination here receives the raw payload rather
than the transformed one the service sends.

## Honesty of the numbers

Signatures are real: HMAC-SHA256 computed by Web Crypto over the same
`<timestamp>.<body>` message the Python service signs, so a digest shown on the page can be
reproduced with `hmac` in a shell.

Durations are not real. The page runs on a virtual clock (`clock.ts`) advanced by a seeded
PRNG (`prng.ts`), so a delivery that "takes 180 ms" consumed no time and every run is
reproducible. Panels label wall-clock time separately from virtual time, and the numbers in
the hero that come from a real run of the service are labelled with the build, machine and
date they were measured on (`provenance.ts`). No request leaves the page.

## Self-check

`selfcheck.ts` is the port's test suite. It asserts the accept path, the three rejection
reasons, the replay guard (including a replay of a request that was deduplicated, which the
nonce store answers with 409), deduplication, bounded retries, the backoff bounds, single
replay under the same idempotency key, the ported smoke suite and the demo burst reproducing
the recorded run's counts. It runs in node through `npm run selfcheck`, and in the browser on
the dev server or with `?selfcheck` in the URL, where the footer shows the tally.
