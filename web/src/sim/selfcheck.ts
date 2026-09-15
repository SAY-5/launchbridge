/**
 * Console self-check for the port. Runs in the browser at start-up (see main.tsx) and in
 * Node via `npm run selfcheck`. Every assertion mirrors a test in the Python suite.
 */

import { canonicalJson, pyDumps, type Json } from "./json";
import { Service, type DeliveryDetail, type WebhookAccepted } from "./service";
import { computeSignature, contentHash, signHeaders, TIMESTAMP_HEADER } from "./signing";
import { Smoke, summarize, checkLabel } from "./smoke";
import { percentileCont } from "./stats";
import { runBurst } from "./burst";
import vectors from "./vectors.json";

export interface SelfCheckLine {
  ok: boolean;
  name: string;
  detail: string;
}

/**
 * One case from web/src/sim/vectors.json. The fixture is committed JSON, so TypeScript
 * infers a literal union for each entry that does not overlap the recursive Json type; the
 * cast goes through unknown for that reason and nothing else.
 */
interface VectorCase {
  value: Json;
  expected: string;
}

const asCases = (raw: unknown): VectorCase[] => raw as VectorCase[];

export async function runSelfCheck(
  log: (line: string) => void = () => {},
  options: { burst?: boolean } = {},
): Promise<SelfCheckLine[]> {
  const lines: SelfCheckLine[] = [];
  const record = (ok: boolean, name: string, detail: string) => {
    lines.push({ ok, name, detail });
    log(`[${ok ? "ok " : "BAD"}] ${name}  (${detail})`);
  };
  const svc = new Service({ seed: 7 });
  const admin = { "X-API-Key": "dev-admin-key" };
  const source = "orders";
  const secret = "orders-dev-secret";

  const body = pyDumps({ id: "order-1", amount: 42 });
  const headers = await signHeaders(secret, body, svc.clock.seconds());
  const accepted = await svc.webhook(source, body, headers);
  record(accepted.status === 202, "valid signature accepted", `status ${accepted.status}`);
  const acceptedBody = accepted.body as WebhookAccepted;
  record(
    acceptedBody.delivery_ids?.length === 2,
    "orders fan out to crm and billing",
    `${acceptedBody.delivery_ids?.length} deliveries`,
  );

  const wrong = await svc.post(source, { id: "order-2" }, { secret: "not-the-secret" });
  record(wrong.status === 401, "wrong secret rejected", errorOf(wrong.body));

  const stale = await svc.post(source, { id: "order-3" }, { timestamp: svc.clock.seconds() - 3600 });
  record(stale.status === 401 && errorOf(stale.body) === "stale_timestamp", "stale timestamp rejected", errorOf(stale.body));

  const replayed = await svc.webhook(source, body, headers);
  record(replayed.status === 409, "replayed signature rejected", errorOf(replayed.body));

  svc.clock.advance(1000);
  const duplicateBody = pyDumps({ id: "order-1", amount: 42 });
  const duplicateHeaders = await signHeaders(secret, duplicateBody, svc.clock.seconds());
  const dup = await svc.webhook(source, duplicateBody, duplicateHeaders);
  const dupBody = dup.body as WebhookAccepted;
  record(
    dup.status === 200 && dupBody.deduplicated === true && dupBody.delivery_ids.length === 0,
    "duplicate deduplicated",
    `status ${dup.status}, deduplicated ${dupBody.deduplicated}`,
  );

  // The nonce store is keyed by signature, not by the ledger row, so re-sending the request
  // that was just deduplicated is still a replay.
  const dedupReplay = await svc.webhook(source, duplicateBody, duplicateHeaders);
  record(
    dedupReplay.status === 409 && errorOf(dedupReplay.body) === "replayed_signature",
    "replay of a deduplicated request is 409",
    `status ${dedupReplay.status}, ${errorOf(dedupReplay.body)}`,
  );

  svc.receiver.addRule({ tag: "down", status: 503 });
  const failing = await svc.post(source, { id: "order-4", tag: "down" });
  const failingId = (failing.body as WebhookAccepted).delivery_ids[0];
  await svc.worker.settle();
  const failed = svc.getDelivery(failingId, admin).body as DeliveryDetail;
  record(
    failed.status === "failed" && failed.attempts === failed.max_attempts,
    "hard failure ends failed after bounded attempts",
    `${failed.status} after ${failed.attempts}/${failed.max_attempts}`,
  );
  const backoffs = failed.attempt_log.map((a) => a.backoff_ms).filter((b): b is number => b !== null);
  record(
    backoffs.length === failed.max_attempts - 1 && backoffs.every((b, i) => b >= 400 * 2 ** i && b <= 600 * 2 ** i),
    "backoff doubles with jitter inside 20 percent",
    backoffs.map((b) => `${b}ms`).join(" "),
  );

  svc.receiver.clearRules();
  const replay = svc.replayOne(failingId, admin, "repaired");
  const replayId = (replay.body as { replay_delivery_id: string }).replay_delivery_id;
  await svc.worker.settle();
  const delivered = svc.getDelivery(replayId, admin).body as DeliveryDetail;
  const entry = svc.receiver.inbox(delivered.idempotency_key);
  record(
    delivered.status === "delivered" &&
      delivered.idempotency_key === failed.idempotency_key &&
      delivered.series === 2 &&
      entry?.count === failed.max_attempts + 1,
    "replay delivers with the same idempotency key",
    `series ${delivered.series}, key ${delivered.idempotency_key}, receiver count ${entry?.count}`,
  );

  // the three header shapes the verifier rejects before it ever computes a digest
  const noHeaders = await svc.webhook(source, pyDumps({ id: "hdr-1" }), {});
  record(
    noHeaders.status === 401 && errorOf(noHeaders.body) === "missing_timestamp",
    "missing timestamp rejected",
    errorOf(noHeaders.body),
  );

  const badTs = await svc.webhook(source, pyDumps({ id: "hdr-2" }), {
    [TIMESTAMP_HEADER]: "not-a-number",
    "X-Signature": "sha256=00",
  });
  record(
    badTs.status === 401 && errorOf(badTs.body) === "invalid_timestamp",
    "non-numeric timestamp rejected",
    errorOf(badTs.body),
  );

  const noSig = await svc.webhook(source, pyDumps({ id: "hdr-3" }), {
    [TIMESTAMP_HEADER]: String(svc.clock.seconds()),
  });
  record(
    noSig.status === 401 && errorOf(noSig.body) === "missing_signature",
    "missing signature rejected",
    errorOf(noSig.body),
  );

  // the chip that skews the timestamp without leaving the tolerance window
  const skew = await svc.post(source, { id: "skew-1" }, { timestamp: svc.clock.seconds() + 299 });
  record(skew.status === 202, "timestamp skew inside the window is accepted", `status ${skew.status}`);

  // the chip that edits the body after it was signed
  const signedBody = pyDumps({ id: "tamper-1", amount: 42 });
  const tamperHeaders = await signHeaders(secret, signedBody, svc.clock.seconds());
  const tampered = await svc.webhook(source, pyDumps({ id: "tamper-1", amount: 43 }), tamperHeaders);
  record(
    tampered.status === 401 && errorOf(tampered.body) === "invalid_signature",
    "body edited after signing is rejected",
    errorOf(tampered.body),
  );

  // the offline destination the replay section uses
  svc.receiver.offline = true;
  const unreachable = await svc.post(source, { id: "offline-1" });
  const unreachableId = (unreachable.body as WebhookAccepted).delivery_ids[0];
  await svc.worker.settle();
  const offlineDelivery = svc.getDelivery(unreachableId, admin).body as DeliveryDetail;
  svc.receiver.offline = false;
  record(
    offlineDelivery.status === "failed" &&
      (offlineDelivery.last_error ?? "").startsWith("ConnectError"),
    "connection refused is transient and ends failed",
    `${offlineDelivery.status} after ${offlineDelivery.attempts}, ${offlineDelivery.last_error?.slice(0, 40)}`,
  );

  // bulk replay honours its since filter
  const future = svc.replayBulk({ since: svc.clock.now() + 60_000 }, admin).body as {
    replayed: number;
  };
  const fromEpoch = svc.replayBulk({ since: 0 }, admin).body as { replayed: number };
  record(
    future.replayed === 0 && fromEpoch.replayed >= 1,
    "bulk replay filters on since",
    `future ${future.replayed}, from epoch ${fromEpoch.replayed}`,
  );
  await svc.worker.settle();

  record(
    percentileCont([], 0.5) === null &&
      percentileCont([10], 0.95) === 10 &&
      percentileCont([10, 20], 0.5) === 15 &&
      percentileCont([1, 2, 3, 4], 0.5) === 2.5,
    "percentile_cont interpolates like PostgreSQL",
    "empty, single, midpoint and interpolated",
  );

  // parity with the Python service, from a fixture the pytest suite writes
  const sig = vectors.signature;
  const computed = await computeSignature(sig.secret, sig.timestamp, sig.body);
  record(computed === sig.expected, "signature matches the Python vector", computed.slice(0, 26));

  const hashed = await contentHash(vectors.content_hash.body);
  record(
    hashed === vectors.content_hash.expected,
    "content hash matches the Python vector",
    hashed.slice(0, 16),
  );

  const dumpsBad = asCases(vectors.py_dumps).filter((c) => pyDumps(c.value) !== c.expected);
  record(
    dumpsBad.length === 0,
    "json.dumps output matches for every vector",
    `${vectors.py_dumps.length} cases, including non-ASCII and escapes`,
  );

  const canonicalBad = asCases(vectors.canonical_json).filter(
    (c) => canonicalJson(c.value) !== c.expected,
  );
  record(
    canonicalBad.length === 0,
    "canonical JSON output matches for every vector",
    `${vectors.canonical_json.length} cases`,
  );

  const [envelope] = asCases([vectors.canonical_envelope]);
  record(
    canonicalJson(envelope.value) === envelope.expected,
    "outbound envelope bytes match the Python vector",
    `${envelope.expected.length} bytes`,
  );

  const smoke = new Smoke(new Service({ seed: 11 }));
  const results = await smoke.run();
  const s = summarize(results);
  record(s.failed === 0 && s.passed === 14, "smoke suite 14/14", `${s.passed} passed, ${s.failed} failed`);
  for (const r of results) log(`      [${checkLabel(r)}] ${r.name}  (${r.detail})`);

  if (options.burst === false) return lines;
  const burst = await runBurst({ seed: 13 });
  record(
    burst.checks.every((c) => c.ok) && burst.events.received === 300 && burst.events.deduplicated === 50,
    "demo burst reproduces the README numbers",
    `${burst.events.received} received, ${burst.events.deduplicated} deduplicated, ${burst.retries} retried, ${burst.failedFirstPass} failed then ${burst.replaysDelivered} replayed`,
  );
  for (const line of burst.lines) log(`      ${line}`);

  return lines;
}

function errorOf(body: unknown): string {
  const detail = (body as { detail?: { error?: string } | string }).detail;
  if (detail && typeof detail === "object" && detail.error) return detail.error;
  return String(detail);
}

declare const process: { exitCode?: number; argv?: string[] } | undefined;

if (typeof process !== "undefined" && process?.argv?.[1]?.includes("selfcheck")) {
  runSelfCheck((line) => console.log(line)).then((lines) => {
    const bad = lines.filter((l) => !l.ok).length;
    console.log(`selfcheck: ${lines.length - bad} ok, ${bad} bad`);
    if (bad) process!.exitCode = 1;
  });
}
