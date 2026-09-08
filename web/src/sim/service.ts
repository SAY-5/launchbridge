/**
 * The service as one object: the API routes, the database, the worker and the receiver
 * fake, all in-process. Responses carry the same status codes and bodies as the FastAPI
 * app so the smoke suite and the demo burst run unmodified.
 */

import { VirtualClock } from "./clock";
import { DestinationRegistry } from "./destinations";
import { ingestEvent, recordRejection, SignatureReplayedError } from "./ingest";
import { pyDumps, type Json } from "./json";
import type { DeliveryAttemptRow, DeliveryRow, DeliveryStatus } from "./models";
import { Prng } from "./prng";
import { Receiver } from "./receiver";
import { failedDeliveries, replayDelivery, ReplayError, replayMany } from "./replay";
import { signHeaders, SIGNATURE_HEADER, SignatureError, TIMESTAMP_HEADER, verifySignature } from "./signing";
import { collectStats, type Stats } from "./stats";
import { Database } from "./store";
import { Worker, type WorkerOptions } from "./worker";

export type { Stats } from "./stats";

export interface ApiResponse<T = unknown> {
  status: number;
  body: T;
}

export interface ErrorBody {
  detail: string | { error: string; message: string };
}

export interface WebhookAccepted {
  event_id: string;
  deduplicated: boolean;
  delivery_ids: string[];
}

export interface AttemptOut {
  attempt_number: number;
  started_at: string;
  duration_ms: number;
  status_code: number | null;
  outcome: string;
  error: string | null;
  succeeded: boolean;
  backoff_ms: number | null;
}

export interface DeliveryOut {
  id: string;
  event_id: string;
  destination: string;
  idempotency_key: string;
  status: DeliveryStatus;
  series: number;
  attempts: number;
  max_attempts: number;
  next_attempt_at: string | null;
  last_status_code: number | null;
  last_error: string | null;
  replay_of: string | null;
  created_at: string;
  updated_at: string;
  delivered_at: string | null;
  replayed_at: string | null;
  latency_ms: number | null;
}

export interface DeliveryDetail extends DeliveryOut {
  source: string;
  attempt_log: AttemptOut[];
}

export const MAX_BODY_BYTES = 1_000_000;
export const RECORDED_HEADERS = new Set(["content-type", "user-agent", "x-event-id", "x-request-id"]);
const DELIVERY_STATUSES: DeliveryStatus[] = ["pending", "in_progress", "delivered", "failed", "replayed"];

export interface ServiceOptions {
  seed?: number;
  secrets?: Record<string, string>;
  apiKeys?: Record<string, string>;
  toleranceSeconds?: number;
  worker?: WorkerOptions;
}

export const DEFAULT_SECRETS: Record<string, string> = {
  smoke: "smoke-dev-secret",
  demo: "demo-dev-secret",
  orders: "orders-dev-secret",
};

export const DEFAULT_API_KEYS: Record<string, string> = { dev: "dev-admin-key" };

export class Service {
  readonly version = "0.1.0";
  readonly clock: VirtualClock;
  readonly rng: Prng;
  readonly db: Database;
  readonly registry: DestinationRegistry;
  readonly receiver: Receiver;
  readonly worker: Worker;
  readonly secrets: Record<string, string>;
  readonly apiKeys: Record<string, string>;
  readonly toleranceSeconds: number;
  /** Set to false to make /readyz answer 503, the way a lost database would. */
  databaseUp = true;

  constructor(options: ServiceOptions = {}) {
    this.clock = new VirtualClock();
    this.rng = new Prng(options.seed ?? 0x5eed);
    this.db = new Database(this.rng);
    this.registry = DestinationRegistry.default();
    this.receiver = new Receiver(this.rng);
    this.worker = new Worker(this.db, this.registry, this.receiver, this.clock, this.rng, options.worker);
    this.secrets = options.secrets ?? DEFAULT_SECRETS;
    this.apiKeys = options.apiKeys ?? DEFAULT_API_KEYS;
    this.toleranceSeconds = options.toleranceSeconds ?? 300;
  }

  // ops

  healthz(): ApiResponse<{ status: string; version: string }> {
    return { status: 200, body: { status: "ok", version: this.version } };
  }

  readyz(): ApiResponse<{ status: string; database: string }> {
    if (!this.databaseUp) {
      return { status: 503, body: { status: "degraded", database: "unreachable" } };
    }
    return { status: 200, body: { status: "ok", database: "ok" } };
  }

  metrics(): ApiResponse<string> {
    const stats = collectStats(this.db);
    const lines = [
      "# HELP launchbridge_events_received_total Accepted inbound events.",
      "# TYPE launchbridge_events_received_total counter",
      `launchbridge_events_received_total ${stats.events.accepted}`,
      "# HELP launchbridge_events_deduplicated_total Inbound events dropped as duplicates.",
      "# TYPE launchbridge_events_deduplicated_total counter",
      `launchbridge_events_deduplicated_total ${stats.events.deduplicated}`,
      "# HELP launchbridge_signature_rejections_total Rejected inbound requests.",
      "# TYPE launchbridge_signature_rejections_total counter",
      `launchbridge_signature_rejections_total ${stats.signature_rejections}`,
      "# HELP launchbridge_deliveries Deliveries by status, read from the database.",
      "# TYPE launchbridge_deliveries gauge",
      ...DELIVERY_STATUSES.map((s) => `launchbridge_deliveries{status="${s}"} ${stats.deliveries[s]}`),
      "# HELP launchbridge_deliveries_retried_total Attempts beyond the first.",
      "# TYPE launchbridge_deliveries_retried_total counter",
      `launchbridge_deliveries_retried_total ${stats.retries}`,
      "# HELP launchbridge_deliveries_replayed_total Replays requested.",
      "# TYPE launchbridge_deliveries_replayed_total counter",
      `launchbridge_deliveries_replayed_total ${stats.replays.requested}`,
    ];
    return { status: 200, body: lines.join("\n") + "\n" };
  }

  // inbound

  async webhook(
    source: string,
    body: string,
    rawHeaders: Record<string, string>,
  ): Promise<ApiResponse<WebhookAccepted | ErrorBody>> {
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(rawHeaders)) headers[k.toLowerCase()] = v;
    const secret = this.secrets[source];
    if (secret === undefined) {
      return { status: 404, body: { detail: `unknown source '${source}'` } };
    }
    if (new TextEncoder().encode(body).length > MAX_BODY_BYTES) {
      return { status: 413, body: { detail: "body too large" } };
    }
    const now = this.clock.now();
    const signature = headers[SIGNATURE_HEADER.toLowerCase()];
    let signedAt: number;
    try {
      signedAt = await verifySignature(
        secret,
        headers[TIMESTAMP_HEADER.toLowerCase()],
        signature,
        body,
        this.toleranceSeconds,
        Math.floor(now / 1000),
      );
    } catch (error) {
      if (error instanceof SignatureError) {
        recordRejection(this.db, source, error.reason, now);
        return { status: 401, body: { detail: { error: error.reason, message: error.detail } } };
      }
      throw error;
    }
    const recorded: Record<string, string> = {};
    for (const [k, v] of Object.entries(headers)) if (RECORDED_HEADERS.has(k)) recorded[k] = v;
    try {
      const result = await ingestEvent(this.db, {
        source,
        body,
        headers: recorded,
        signature: signature ?? "",
        signedAt,
        now,
        registry: this.registry,
      });
      return { status: result.deduplicated ? 200 : 202, body: result };
    } catch (error) {
      if (error instanceof SignatureReplayedError) {
        recordRejection(this.db, source, "replayed_signature", now);
        return {
          status: 409,
          body: { detail: { error: "replayed_signature", message: "signature already accepted" } },
        };
      }
      throw error;
    }
  }

  /** Sign and post a JSON payload the way a source would. */
  async post(
    source: string,
    payload: Json,
    opts: { secret?: string; timestamp?: number; eventId?: string } = {},
  ): Promise<ApiResponse<WebhookAccepted | ErrorBody>> {
    const body = pyDumps(payload);
    const secret = opts.secret ?? this.secrets[source] ?? "";
    const headers = await signHeaders(secret, body, opts.timestamp ?? this.clock.seconds());
    headers["Content-Type"] = "application/json";
    if (opts.eventId) headers["X-Event-Id"] = opts.eventId;
    return this.webhook(source, body, headers);
  }

  // admin

  private actor(headers: Record<string, string>): string | null {
    const presented = headers["X-API-Key"] ?? headers["x-api-key"];
    if (!presented) return null;
    for (const [label, key] of Object.entries(this.apiKeys)) if (key === presented) return label;
    return null;
  }

  private unauthorized(): ApiResponse<ErrorBody> {
    return { status: 401, body: { detail: "invalid or missing API key" } };
  }

  serializeDelivery(d: DeliveryRow): DeliveryOut {
    const iso = (v: number | null) => (v === null ? null : this.clock.iso(v));
    return {
      id: d.id,
      event_id: d.event_id,
      destination: d.destination,
      idempotency_key: d.idempotency_key,
      status: d.status,
      series: d.series,
      attempts: d.attempts,
      max_attempts: d.max_attempts,
      next_attempt_at: iso(d.next_attempt_at),
      last_status_code: d.last_status_code,
      last_error: d.last_error,
      replay_of: d.replay_of,
      created_at: this.clock.iso(d.created_at),
      updated_at: this.clock.iso(d.updated_at),
      delivered_at: iso(d.delivered_at),
      replayed_at: iso(d.replayed_at),
      latency_ms: d.latency_ms,
    };
  }

  serializeAttempt(a: DeliveryAttemptRow): AttemptOut {
    return {
      attempt_number: a.attempt_number,
      started_at: this.clock.iso(a.started_at),
      duration_ms: a.duration_ms,
      status_code: a.status_code,
      outcome: a.outcome,
      error: a.error,
      succeeded: a.succeeded,
      backoff_ms: a.backoff_ms,
    };
  }

  listDeliveries(
    params: { status?: string; source?: string; destination?: string; event_id?: string; since?: number; limit?: number; offset?: number },
    headers: Record<string, string>,
  ): ApiResponse<{ items: DeliveryOut[]; count: number } | ErrorBody> {
    if (!this.actor(headers)) return this.unauthorized();
    if (params.status && !DELIVERY_STATUSES.includes(params.status as DeliveryStatus)) {
      return { status: 422, body: { detail: "unknown status" } };
    }
    const rows = [...this.db.deliveries.values()].filter((d) => {
      const event = this.db.events.get(d.event_id);
      if (params.status && d.status !== params.status) return false;
      if (params.source && event?.source !== params.source) return false;
      if (params.destination && d.destination !== params.destination) return false;
      if (params.event_id && d.event_id !== params.event_id) return false;
      if (params.since != null && d.created_at < params.since) return false;
      return true;
    });
    rows.sort((a, b) => b.created_at - a.created_at);
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 100;
    return {
      status: 200,
      body: { items: rows.slice(offset, offset + limit).map((d) => this.serializeDelivery(d)), count: rows.length },
    };
  }

  getDelivery(id: string, headers: Record<string, string>): ApiResponse<DeliveryDetail | ErrorBody> {
    if (!this.actor(headers)) return this.unauthorized();
    const d = this.db.deliveries.get(id);
    if (!d) return { status: 404, body: { detail: "delivery not found" } };
    const event = this.db.events.get(d.event_id);
    return {
      status: 200,
      body: {
        ...this.serializeDelivery(d),
        source: event?.source ?? "",
        attempt_log: this.db.attemptsFor(d.id).map((a) => this.serializeAttempt(a)),
      },
    };
  }

  replayOne(
    id: string,
    headers: Record<string, string>,
    reason?: string,
  ): ApiResponse<{ original_delivery_id: string; replay_delivery_id: string } | ErrorBody> {
    const actor = this.actor(headers);
    if (!actor) return this.unauthorized();
    const d = this.db.deliveries.get(id);
    if (!d) return { status: 404, body: { detail: "delivery not found" } };
    try {
      const replacement = replayDelivery(this.db, d, { actor, mode: "single", now: this.clock.now(), reason });
      return { status: 202, body: { original_delivery_id: d.id, replay_delivery_id: replacement.id } };
    } catch (error) {
      if (error instanceof ReplayError) return { status: 409, body: { detail: error.message } };
      throw error;
    }
  }

  replayBulk(
    params: { source?: string; since?: number; destination?: string; reason?: string; limit?: number },
    headers: Record<string, string>,
  ): ApiResponse<{ replayed: number; delivery_ids: string[] } | ErrorBody> {
    const actor = this.actor(headers);
    if (!actor) return this.unauthorized();
    if (!params.source && params.since == null && !params.destination) {
      return { status: 422, body: { detail: "provide at least one of source, since, destination" } };
    }
    const targets = failedDeliveries(this.db, {
      source: params.source,
      since: params.since,
      destination: params.destination,
      limit: params.limit ?? 500,
    });
    const ids = replayMany(this.db, targets, { actor, now: this.clock.now(), reason: params.reason });
    return { status: 202, body: { replayed: ids.length, delivery_ids: ids } };
  }

  listReplays(headers: Record<string, string>) {
    if (!this.actor(headers)) return this.unauthorized();
    const items = [...this.db.replays]
      .sort((a, b) => b.requested_at - a.requested_at)
      .map((r) => ({ ...r, requested_at: this.clock.iso(r.requested_at) }));
    return { status: 200, body: { items, count: items.length } };
  }

  stats(params: { source?: string; since?: number }, headers: Record<string, string>): ApiResponse<Stats | ErrorBody> {
    if (!this.actor(headers)) return this.unauthorized();
    return { status: 200, body: collectStats(this.db, params) };
  }
}
