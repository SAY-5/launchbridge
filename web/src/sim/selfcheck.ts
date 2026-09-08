/**
 * Console self-check for the port. Runs in the browser at start-up (see main.tsx) and in
 * Node via `npm run selfcheck`. Every assertion mirrors a test in the Python suite.
 */

import { pyDumps } from "./json";
import { Service, type DeliveryDetail, type WebhookAccepted } from "./service";
import { signHeaders } from "./signing";
import { Smoke, summarize, checkLabel } from "./smoke";
import { runBurst } from "./burst";

export interface SelfCheckLine {
  ok: boolean;
  name: string;
  detail: string;
}

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
  const dup = await svc.post(source, { id: "order-1", amount: 42 });
  const dupBody = dup.body as WebhookAccepted;
  record(
    dup.status === 200 && dupBody.deduplicated === true && dupBody.delivery_ids.length === 0,
    "duplicate deduplicated",
    `status ${dup.status}, deduplicated ${dupBody.deduplicated}`,
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
