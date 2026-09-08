/**
 * The 14 smoke checks, ported one to one. Each check runs against the in-process service,
 * the worker settles deliveries between steps, and PASS/FAIL/SKIP is reported the same way
 * `python -m smoke.smoke` prints it.
 */

import { pyDumps, type Json } from "./json";
import type { DeliveryDetail, Service, WebhookAccepted } from "./service";
import { signHeaders } from "./signing";

export interface CheckResult {
  name: string;
  passed: boolean | null;
  detail: string;
  /** Wall-clock time the check took to run in the browser. */
  wall_ms: number;
  /** Virtual service time consumed by the check. */
  virtual_ms: number;
}

export function checkLabel(result: CheckResult): "PASS" | "FAIL" | "SKIP" {
  return result.passed === null ? "SKIP" : result.passed ? "PASS" : "FAIL";
}

export class SmokeFailure extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SmokeFailure";
  }
}

export function expect(condition: boolean, message: string): asserts condition {
  if (!condition) throw new SmokeFailure(message);
}

export const SMOKE_CHECK_NAMES = [
  "health endpoint",
  "readiness endpoint (database)",
  "signed event accepted",
  "event delivered to destination",
  "receiver verified outbound signature",
  "duplicate event deduplicated",
  "wrong secret rejected",
  "stale timestamp rejected",
  "replayed signature rejected",
  "admin endpoints require API key",
  "bounded retries end in failed",
  "replay after fix delivers",
  "bulk replay by source and since",
  "metrics endpoint",
] as const;

export interface SmokeOptions {
  source?: string;
  secret?: string;
  apiKey?: string;
  /** Whether the receiver control API is reachable; false reports the four injection checks as SKIP. */
  receiver?: boolean;
  onResult?: (result: CheckResult, index: number) => void | Promise<void>;
  /** Called before each check starts, useful for a live checklist. */
  onStart?: (name: string, index: number) => void | Promise<void>;
  now?: () => number;
}

interface SmokeState {
  payload?: Json;
  event_id?: string;
  delivery_id?: string;
  key?: string;
  failed_id?: string;
  failed_key?: string;
  max_attempts?: number;
}

export class Smoke {
  readonly service: Service;
  readonly source: string;
  readonly secret: string;
  readonly admin: Record<string, string>;
  readonly hasReceiver: boolean;
  readonly runId: string;
  readonly results: CheckResult[] = [];
  private readonly opts: SmokeOptions;
  private readonly wall: () => number;

  constructor(service: Service, opts: SmokeOptions = {}) {
    this.service = service;
    this.opts = opts;
    this.source = opts.source ?? "smoke";
    this.secret = opts.secret ?? "smoke-dev-secret";
    this.admin = { "X-API-Key": opts.apiKey ?? "dev-admin-key" };
    this.hasReceiver = opts.receiver ?? true;
    this.runId = service.rng.hex(8);
    this.wall = opts.now ?? (() => (typeof performance !== "undefined" ? performance.now() : 0));
  }

  payload(fields: Record<string, Json> = {}): Json {
    return { id: `smoke-${this.runId}-${this.service.rng.hex(8)}`, kind: "smoke", ...fields };
  }

  async postEvent(payload: Json, opts: { secret?: string; timestamp?: number } = {}) {
    return this.service.post(this.source, payload, {
      secret: opts.secret ?? this.secret,
      timestamp: opts.timestamp,
    });
  }

  /** wait_for: settle the worker until the delivery reaches one of `statuses`. */
  async waitFor(deliveryId: string, statuses: Set<string>): Promise<DeliveryDetail> {
    const read = () => this.service.getDelivery(deliveryId, this.admin).body as DeliveryDetail;
    await this.service.worker.settle(() => statuses.has(read().status), 60_000);
    const last = read();
    if (!statuses.has(last.status)) {
      throw new SmokeFailure(`delivery ${deliveryId} still ${last.status} after 60s`);
    }
    return last;
  }

  addRule(tag: string, status: number, times?: number): void {
    this.service.receiver.addRule(times === undefined ? { tag, status } : { tag, status, times });
  }

  clearRules(): void {
    this.service.receiver.clearRules();
  }

  inbox(key: string) {
    const entry = this.service.receiver.inbox(key);
    expect(entry !== undefined, `receiver never saw ${key}`);
    return entry;
  }

  private async check(name: string, fn: () => Promise<string | void> | string | void, needsReceiver = false) {
    const index = this.results.length;
    await this.opts.onStart?.(name, index);
    const wallStart = this.wall();
    const virtualStart = this.service.clock.now();
    let result: CheckResult;
    if (needsReceiver && !this.hasReceiver) {
      result = { name, passed: null, detail: "no receiver URL configured", wall_ms: 0, virtual_ms: 0 };
    } else {
      try {
        const detail = await fn();
        result = { name, passed: true, detail: detail ?? "", wall_ms: 0, virtual_ms: 0 };
      } catch (error) {
        const err = error as Error;
        result = { name, passed: false, detail: `${err.name}: ${err.message}`, wall_ms: 0, virtual_ms: 0 };
      }
    }
    result.wall_ms = Math.max(0, Math.round((this.wall() - wallStart) * 10) / 10);
    result.virtual_ms = this.service.clock.now() - virtualStart;
    this.results.push(result);
    await this.opts.onResult?.(result, index);
  }

  async run(): Promise<CheckResult[]> {
    const svc = this.service;
    const state: SmokeState = {};

    await this.check("health endpoint", () => {
      const r = svc.healthz();
      expect(r.status === 200, `healthz returned ${r.status}`);
      return `version ${r.body.version}`;
    });

    await this.check("readiness endpoint (database)", () => {
      const r = svc.readyz();
      expect(r.status === 200, `readyz returned ${r.status}`);
      return `database ${r.body.database}`;
    });

    await this.check("signed event accepted", async () => {
      state.payload = this.payload();
      const r = await this.postEvent(state.payload);
      expect(r.status === 202, `expected 202, got ${r.status}`);
      const body = r.body as WebhookAccepted;
      expect(body.deduplicated === false, "fresh event flagged as duplicate");
      expect(body.delivery_ids.length >= 1, "no deliveries enqueued");
      state.event_id = body.event_id;
      state.delivery_id = body.delivery_ids[0];
      return `event ${body.event_id} with ${body.delivery_ids.length} deliveries`;
    });

    await this.check("event delivered to destination", async () => {
      const d = await this.waitFor(state.delivery_id!, new Set(["delivered", "failed"]));
      expect(d.status === "delivered", `delivery ended ${d.status}`);
      state.key = d.idempotency_key;
      return `${d.destination} in ${d.latency_ms} ms`;
    });

    await this.check(
      "receiver verified outbound signature",
      () => {
        const entry = this.inbox(state.key!);
        expect(entry.signature_valid === true, "receiver rejected the outbound signature");
        expect(entry.count === 1, `receiver saw the key ${entry.count} times`);
        return "signature valid, seen once";
      },
      true,
    );

    await this.check("duplicate event deduplicated", async () => {
      svc.clock.advance(1050);
      const r = await this.postEvent(state.payload!);
      expect(r.status === 200, `expected 200, got ${r.status}`);
      const body = r.body as WebhookAccepted;
      expect(body.deduplicated === true, "duplicate not flagged");
      expect(body.delivery_ids.length === 0, "duplicate enqueued deliveries");
      return "deduplicated: true, no deliveries";
    });

    await this.check("wrong secret rejected", async () => {
      const r = await this.postEvent(this.payload(), { secret: "not-the-secret" });
      expect(r.status === 401, `expected 401, got ${r.status}`);
      return errorCode(r.body);
    });

    await this.check("stale timestamp rejected", async () => {
      const r = await this.postEvent(this.payload(), { timestamp: svc.clock.seconds() - 3600 });
      expect(r.status === 401, `expected 401, got ${r.status}`);
      return errorCode(r.body);
    });

    await this.check("replayed signature rejected", async () => {
      const body = pyDumps(this.payload());
      const headers = await signHeaders(this.secret, body, svc.clock.seconds());
      const first = await svc.webhook(this.source, body, headers);
      expect(first.status === 202, `first send returned ${first.status}`);
      const second = await svc.webhook(this.source, body, headers);
      expect(second.status === 409, `expected 409, got ${second.status}`);
      return errorCode(second.body);
    });

    await this.check("admin endpoints require API key", () => {
      expect(svc.listDeliveries({}, {}).status === 401, "missing key accepted");
      expect(svc.listDeliveries({}, { "X-API-Key": "wrong" }).status === 401, "wrong key accepted");
      expect(svc.listDeliveries({}, this.admin).status === 200, "valid key rejected");
      return "401 without key, 200 with key";
    });

    await this.check(
      "bounded retries end in failed",
      async () => {
        const tag = `smoke-down-${this.runId}`;
        this.addRule(tag, 503);
        const r = await this.postEvent(this.payload({ tag }));
        expect(r.status === 202, "failing event not accepted");
        state.failed_id = (r.body as WebhookAccepted).delivery_ids[0];
        const d = await this.waitFor(state.failed_id, new Set(["delivered", "failed"]));
        expect(d.status === "failed", `delivery ended ${d.status}`);
        expect(
          d.attempts === d.max_attempts,
          `${d.attempts} attempts, expected ${d.max_attempts}`,
        );
        expect((d.last_error ?? "").includes("exhausted"), "last_error lacks exhaustion");
        state.failed_key = d.idempotency_key;
        state.max_attempts = d.max_attempts;
        expect(this.inbox(state.failed_key).count === d.max_attempts, "count");
        return `failed after ${d.attempts}/${d.max_attempts} attempts`;
      },
      true,
    );

    await this.check(
      "replay after fix delivers",
      async () => {
        this.clearRules();
        const r = svc.replayOne(state.failed_id!, this.admin, "smoke: destination repaired");
        expect(r.status === 202, `replay returned ${r.status}`);
        const newId = (r.body as { replay_delivery_id: string }).replay_delivery_id;
        const d = await this.waitFor(newId, new Set(["delivered", "failed"]));
        expect(d.status === "delivered", `replay ended ${d.status}`);
        expect(d.series === 2, "replay is not series 2");
        const original = svc.getDelivery(state.failed_id!, this.admin).body as DeliveryDetail;
        expect(original.status === "replayed", "original not marked replayed");
        const entry = this.inbox(state.failed_key!);
        expect(
          entry.count === state.max_attempts! + 1,
          `receiver saw the key ${entry.count} times`,
        );
        return `same idempotency key, receiver count ${entry.count}`;
      },
      true,
    );

    await this.check(
      "bulk replay by source and since",
      async () => {
        const tag = `smoke-broken-${this.runId}`;
        const since = svc.clock.now();
        this.addRule(tag, 400);
        const r = await this.postEvent(this.payload({ tag }));
        const failedId = (r.body as WebhookAccepted).delivery_ids[0];
        await this.waitFor(failedId, new Set(["failed"]));
        this.clearRules();
        const bulk = svc.replayBulk({ source: this.source, since }, this.admin);
        expect(bulk.status === 202, `bulk replay returned ${bulk.status}`);
        const body = bulk.body as { replayed: number; delivery_ids: string[] };
        expect(body.replayed >= 1, "bulk replay found nothing");
        const outcomes: string[] = [];
        for (const id of body.delivery_ids) {
          outcomes.push((await this.waitFor(id, new Set(["delivered", "failed"]))).status);
        }
        expect(outcomes.every((o) => o === "delivered"), `outcomes ${outcomes.join(",")}`);
        return `replayed ${body.delivery_ids.length}, all delivered`;
      },
      true,
    );

    await this.check("metrics endpoint", () => {
      const text = svc.metrics().body;
      for (const name of ["launchbridge_events_received_total", "launchbridge_deliveries"]) {
        expect(text.includes(name), `${name} missing from /metrics`);
      }
      return "prometheus series present";
    });

    this.clearRules();
    return this.results;
  }
}

function errorCode(body: unknown): string {
  const detail = (body as { detail?: { error?: string } | string }).detail;
  if (detail && typeof detail === "object" && detail.error) return detail.error;
  return String(detail);
}

export function summarize(results: CheckResult[]): { passed: number; failed: number; skipped: number } {
  return {
    passed: results.filter((r) => r.passed === true).length,
    failed: results.filter((r) => r.passed === false).length,
    skipped: results.filter((r) => r.passed === null).length,
  };
}
