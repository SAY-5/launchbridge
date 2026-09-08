/**
 * The demo burst from scripts/demo.py: 300 events (250 unique, 50 duplicates), 20 hard
 * failures, 30 flaky deliveries that answer 503 twice, three bad signatures, then a bulk
 * replay after the destination is repaired. Every number is read back from /stats.
 */

import { pyDumps, type Json } from "./json";
import { Service, type WebhookAccepted } from "./service";
import { signHeaders } from "./signing";
import { Smoke, summarize, type CheckResult } from "./smoke";
import type { Stats } from "./stats";

type StatsShape = Stats;

export const TOTAL_EVENTS = 300;
export const DUPLICATES = 50;
export const HARD_FAILURES = 20;
export const FLAKY = 30;
export const FLAKY_FAILURES_EACH = 2;
export const BAD_SIGNATURES = 3;
export const DEMO_SOURCE = "demo";
export const DEMO_SECRET = "demo-dev-secret";
export const ADMIN_API_KEY = "dev-admin-key";
/** Posts between worker polls while the burst is being ingested. */
const WORKER_POLL_EVERY = 8;

export type BurstPhase =
  | "smoke"
  | "first-pass"
  | "duplicates"
  | "rejections"
  | "settling"
  | "replay"
  | "resettling"
  | "done";

export interface BurstProgress {
  phase: BurstPhase;
  /** Events posted so far, first pass plus duplicates. */
  posted: number;
  accepted: number;
  deduplicated: number;
  rejected: number;
  stats: StatsShape | null;
  smoke: CheckResult[];
}

export interface BurstSummary {
  events: StatsShape["events"];
  deliveries: StatsShape["deliveries"];
  deliveredFirstPass: number;
  failedFirstPass: number;
  retries: number;
  replayed: number;
  replaysDelivered: number;
  rejections: number;
  latency: StatsShape["latency_ms"];
  smokePassed: number;
  smokeTotal: number;
  smokeSkipped: number;
  checks: { name: string; ok: boolean }[];
  lines: string[];
  rejectionStatuses: number[];
}

export interface BurstOptions {
  seed?: number;
  service?: Service;
  onProgress?: (progress: BurstProgress) => void | Promise<void>;
  /** Yield to the event loop every N posts so the page can repaint. */
  yieldEvery?: number;
}

const yieldNow = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

export async function runBurst(options: BurstOptions = {}): Promise<BurstSummary> {
  const service = options.service ?? new Service({ seed: options.seed ?? 0x5eed });
  const admin = { "X-API-Key": ADMIN_API_KEY };
  const runId = service.rng.hex(8);
  const yieldEvery = options.yieldEvery ?? 25;
  const progress: BurstProgress = {
    phase: "smoke",
    posted: 0,
    accepted: 0,
    deduplicated: 0,
    rejected: 0,
    stats: null,
    smoke: [],
  };
  const report = async () => {
    await options.onProgress?.({ ...progress, smoke: [...progress.smoke] });
  };

  await report();
  const smokeResults = await new Smoke(service, {
    onResult: async (r) => {
      progress.smoke.push(r);
      await report();
    },
  }).run();
  const smokeSummary = summarize(smokeResults);

  const hardTag = `hard-${runId}`;
  const flakyTag = `flaky-${runId}`;
  service.receiver.clearRules();
  service.receiver.addRule({ tag: hardTag, status: 400 });
  service.receiver.addRule({ tag: flakyTag, status: 503, times: FLAKY_FAILURES_EACH });

  const since = service.clock.now();
  const unique = TOTAL_EVENTS - DUPLICATES;
  const payloads: Record<string, Json>[] = [];
  for (let index = 0; index < unique; index++) {
    const payload: Record<string, Json> = {
      id: `demo-${runId}-${String(index).padStart(4, "0")}`,
      sequence: index,
      kind: "burst",
    };
    if (index < HARD_FAILURES) payload.tag = hardTag;
    else if (index < HARD_FAILURES + FLAKY) payload.tag = flakyTag;
    payloads.push(payload);
  }
  const plain = payloads.filter((p) => !("tag" in p));
  const duplicates = Array.from({ length: DUPLICATES }, (_, i) => plain[i % plain.length]);

  // The first event is sent by hand so its exact request can be replayed later.
  progress.phase = "first-pass";
  const leadBody = pyDumps(payloads[0]);
  const leadHeaders = await signHeaders(DEMO_SECRET, leadBody, service.clock.seconds());
  leadHeaders["Content-Type"] = "application/json";
  const firstPass: number[] = [(await service.webhook(DEMO_SOURCE, leadBody, leadHeaders)).status];
  progress.posted = 1;
  progress.accepted = firstPass[0] === 202 ? 1 : 0;
  for (let i = 1; i < payloads.length; i++) {
    const r = await service.post(DEMO_SOURCE, payloads[i], { secret: DEMO_SECRET });
    firstPass.push(r.status);
    progress.posted += 1;
    if (r.status === 202) progress.accepted += 1;
    service.clock.advance(3);
    if (i % WORKER_POLL_EVERY === 0) await service.worker.runOnce(); // the worker polls beside the API
    if (i % yieldEvery === 0) {
      await report();
      await yieldNow();
    }
  }
  await report();

  progress.phase = "duplicates";
  service.clock.advance(1050); // fresh timestamps so duplicates are dedup hits, not signature replays
  const secondPass: boolean[] = [];
  for (let i = 0; i < duplicates.length; i++) {
    const r = await service.post(DEMO_SOURCE, duplicates[i], { secret: DEMO_SECRET });
    const dedup = (r.body as WebhookAccepted).deduplicated === true;
    secondPass.push(dedup);
    progress.posted += 1;
    if (dedup) progress.deduplicated += 1;
    service.clock.advance(3);
    if (i % WORKER_POLL_EVERY === 0) await service.worker.runOnce();
    if (i % yieldEvery === 0) {
      await report();
      await yieldNow();
    }
  }
  await report();

  progress.phase = "rejections";
  const stale = service.clock.seconds() - 3600;
  const rejections = [
    (await service.post(DEMO_SOURCE, { id: `bad-${runId}-1` }, { secret: "wrong-secret" })).status,
    (await service.post(DEMO_SOURCE, { id: `bad-${runId}-2` }, { secret: DEMO_SECRET, timestamp: stale })).status,
    (await service.webhook(DEMO_SOURCE, leadBody, leadHeaders)).status,
  ];
  progress.rejected = rejections.filter((s) => s === 401 || s === 409).length;
  await report();

  progress.phase = "settling";
  const readStats = () => service.stats({ source: DEMO_SOURCE, since }, admin).body as Stats;
  await settleWithProgress(service, async () => {
    progress.stats = readStats();
    await report();
  });
  const before = readStats();
  const failedFirstPass = before.deliveries.failed;
  const deliveredFirstPass = before.deliveries.delivered;

  progress.phase = "replay";
  service.receiver.clearRules();
  const replay = service.replayBulk({ source: DEMO_SOURCE, since }, admin).body as {
    replayed: number;
    delivery_ids: string[];
  };
  progress.stats = readStats();
  await report();

  progress.phase = "resettling";
  await settleWithProgress(service, async () => {
    progress.stats = readStats();
    await report();
  });
  const after = readStats();
  progress.stats = after;

  const checks = [
    { name: "deduplicated == duplicates", ok: after.events.deduplicated === DUPLICATES },
    { name: "failed before replay == hard failures", ok: failedFirstPass === HARD_FAILURES },
    { name: "replayed == hard failures", ok: replay.replayed === HARD_FAILURES },
    { name: "all replays delivered", ok: after.replays.delivered === replay.replayed },
    { name: "nothing left failed", ok: after.deliveries.failed === 0 },
    { name: "signature rejections == bad requests", ok: after.signature_rejections === BAD_SIGNATURES },
    { name: "smoke suite green", ok: smokeSummary.failed === 0 },
  ];
  const latency = after.latency_ms;
  const lines = [
    "== LaunchBridge demo summary ==",
    `events received:        ${after.events.received}`,
    `  unique accepted:      ${after.events.accepted}`,
    `  deduplicated:         ${after.events.deduplicated}   (duplicates sent: ${DUPLICATES})`,
    `deliveries delivered:   ${after.deliveries.delivered}   (${deliveredFirstPass} first pass + ${after.replays.delivered} after replay)`,
    `deliveries retried:     ${after.retries}   (attempts beyond the first)`,
    `deliveries failed:      ${failedFirstPass}   (hard failures injected: ${HARD_FAILURES})`,
    `replayed after fix:     ${replay.replayed}   -> delivered ${after.replays.delivered}, still failed ${after.deliveries.failed}`,
    `signature rejections:   ${after.signature_rejections}   (sent: wrong secret, stale timestamp, replayed signature)`,
    `dispatch latency:       p50 ${latency.p50} ms   p95 ${latency.p95} ms`,
    `smoke checks passed:    ${smokeSummary.passed}/${smokeResults.length}` +
      (smokeSummary.skipped ? `   (${smokeSummary.skipped} skipped)` : ""),
    ...checks.map((c) => `check ${c.ok ? "ok " : "BAD"}  ${c.name}`),
  ];

  progress.phase = "done";
  await report();
  return {
    events: after.events,
    deliveries: after.deliveries,
    deliveredFirstPass,
    failedFirstPass,
    retries: after.retries,
    replayed: replay.replayed,
    replaysDelivered: after.replays.delivered,
    rejections: after.signature_rejections,
    latency,
    smokePassed: smokeSummary.passed,
    smokeTotal: smokeResults.length,
    smokeSkipped: smokeSummary.skipped,
    checks,
    lines,
    rejectionStatuses: rejections,
  };
}

/** Settle in steps so the page can show deliveries draining between backoff waits. */
async function settleWithProgress(service: Service, tick: () => Promise<void>): Promise<void> {
  const worker = service.worker;
  for (let i = 0; i < 400; i++) {
    const processed = await worker.drain();
    await tick();
    const open = [...service.db.deliveries.values()].filter(
      (d) => d.status === "pending" || d.status === "in_progress",
    );
    if (open.length === 0) return;
    if (processed === 0) {
      const nextDue = Math.min(...open.map((d) => d.next_attempt_at ?? Number.POSITIVE_INFINITY));
      if (!Number.isFinite(nextDue)) return;
      service.clock.set(nextDue);
    }
    await yieldNow();
  }
}
