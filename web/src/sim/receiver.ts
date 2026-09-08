/**
 * Destination fake: verifies outbound signatures, records an inbox, injects failures.
 * Failure rules match the `tag` field of the delivered payload; `times` counts per
 * idempotency key so retries of the same delivery eventually succeed.
 */

import { parseJson } from "./json";
import type { Prng } from "./prng";
import {
  EVENT_ID_HEADER,
  IDEMPOTENCY_HEADER,
  SIGNATURE_HEADER,
  SignatureError,
  TIMESTAMP_HEADER,
  verifySignature,
} from "./signing";

export interface Rule {
  tag: string;
  status: number;
  times?: number;
}

export interface InboxEntry {
  idempotency_key: string;
  count: number;
  first_seen: number;
  last_seen: number;
  hook?: string;
  signature_valid?: boolean | null;
  reason?: string;
  event_id?: string;
  tag?: string | null;
  last_status?: number;
}

export interface HookResponse {
  status: number;
  body: Record<string, unknown>;
  /** Simulated round trip in milliseconds. */
  duration_ms: number;
}

export class TransportError extends Error {
  constructor(kind: string, detail: string) {
    super(detail);
    this.name = kind;
  }
}

export class Receiver {
  readonly secrets: Record<string, string>;
  readonly tolerance: number;
  readonly rules: Rule[] = [];
  readonly entries = new Map<string, InboxEntry>();
  private readonly ruleHits = new Map<string, number>();
  private readonly rng: Prng;
  /** When true every request fails at the transport layer (connection refused). */
  offline = false;
  /** Base latency range used for each simulated request. */
  latencyMs: [number, number] = [55, 135];

  constructor(rng: Prng, secrets?: Record<string, string>, tolerance = 300) {
    this.rng = rng;
    this.secrets = secrets ?? { crm: "crm-dev-secret", billing: "billing-dev-secret" };
    this.tolerance = tolerance;
  }

  reset(): void {
    this.entries.clear();
    this.ruleHits.clear();
  }

  clearRules(): void {
    this.rules.length = 0;
    this.ruleHits.clear();
  }

  addRule(rule: Rule): Rule {
    if (rule.status < 400 || rule.status > 599) throw new Error("status must be 4xx or 5xx");
    if (rule.times !== undefined && rule.times < 1) throw new Error("times must be >= 1");
    this.rules.push(rule);
    return rule;
  }

  injectedStatus(tag: string | null, key: string): number | null {
    if (tag === null) return null;
    for (let index = 0; index < this.rules.length; index++) {
      const rule = this.rules[index];
      if (rule.tag !== tag) continue;
      const hitKey = `${index}:${key}`;
      const hits = this.ruleHits.get(hitKey) ?? 0;
      if (rule.times !== undefined && hits >= rule.times) continue;
      this.ruleHits.set(hitKey, hits + 1);
      return rule.status;
    }
    return null;
  }

  record(key: string, now: number, fields: Partial<InboxEntry>): InboxEntry {
    let entry = this.entries.get(key);
    if (!entry) {
      entry = { idempotency_key: key, count: 0, first_seen: now, last_seen: now };
      this.entries.set(key, entry);
    }
    entry.count += 1;
    entry.last_seen = now;
    Object.assign(entry, fields);
    return { ...entry };
  }

  inbox(key: string): InboxEntry | undefined {
    const entry = this.entries.get(key);
    return entry ? { ...entry } : undefined;
  }

  /** `POST /hooks/{name}`; throws TransportError when the fake is offline. */
  async hook(
    name: string,
    body: string,
    headers: Record<string, string>,
    nowMs: number,
  ): Promise<HookResponse> {
    const duration = Math.round(this.rng.uniform(this.latencyMs[0], this.latencyMs[1]));
    if (this.offline) {
      throw new TransportError("ConnectError", "All connection attempts failed");
    }
    const key = headers[IDEMPOTENCY_HEADER];
    if (!key) {
      return { status: 400, body: { detail: "missing idempotency key" }, duration_ms: duration };
    }
    const secret = this.secrets[name];
    let signatureValid: boolean | null = null;
    if (secret !== undefined) {
      try {
        await verifySignature(
          secret,
          headers[TIMESTAMP_HEADER],
          headers[SIGNATURE_HEADER],
          body,
          this.tolerance,
          Math.floor(nowMs / 1000),
        );
        signatureValid = true;
      } catch (error) {
        if (error instanceof SignatureError) {
          this.record(key, nowMs, { hook: name, signature_valid: false, reason: error.reason });
          return { status: 401, body: { detail: error.reason }, duration_ms: duration };
        }
        throw error;
      }
    }

    let tag: string | null = null;
    const envelope = parseJson(body);
    if (envelope && typeof envelope === "object" && !Array.isArray(envelope)) {
      const payload = envelope["payload"];
      if (payload && typeof payload === "object" && !Array.isArray(payload)) {
        const t = payload["tag"];
        if (typeof t === "string") tag = t;
      }
    }

    const injected = this.injectedStatus(tag, key);
    const entry = this.record(key, nowMs, {
      hook: name,
      signature_valid: signatureValid,
      event_id: headers[EVENT_ID_HEADER],
      tag,
      last_status: injected ?? 200,
    });
    if (injected !== null) {
      return {
        status: injected,
        body: { ok: false, injected, count: entry.count },
        duration_ms: duration,
      };
    }
    return { status: 200, body: { ok: true, count: entry.count }, duration_ms: duration };
  }
}
