/**
 * Delivery worker: claims due deliveries, posts signed requests, applies the retry policy.
 * Time is virtual: each batch runs in waves of `concurrency`, and the clock moves forward
 * by the slowest request in each wave, the way a thread pool would.
 */

import type { VirtualClock } from "./clock";
import { canonicalJson, type Json } from "./json";
import type { Destination, DestinationRegistry } from "./destinations";
import type { DeliveryAttemptRow, DeliveryRow, DeliveryStatus, EventRow } from "./models";
import type { Prng } from "./prng";
import { Receiver, TransportError } from "./receiver";
import { classify } from "./retry";
import { EVENT_ID_HEADER, IDEMPOTENCY_HEADER, signHeaders } from "./signing";
import type { Database } from "./store";

export const STALE_IN_PROGRESS_SECONDS = 300;

export interface AttemptEvent {
  delivery: DeliveryRow;
  attempt: DeliveryAttemptRow;
  state: DeliveryStatus;
}

export interface WorkerOptions {
  batchSize?: number;
  concurrency?: number;
  processedEventsTtlMs?: number;
  /** Nonces are kept for twice the timestamp tolerance, 600s by default. */
  nonceTtlMs?: number;
  onAttempt?: (event: AttemptEvent) => void;
}

/** Canonical outbound body; identical across retries and replays of one event. */
export function buildEnvelope(event: EventRow, delivery: DeliveryRow, iso: string): string {
  const envelope: Json = {
    event_id: event.id,
    source: event.source,
    event_key: event.event_key,
    received_at: iso,
    destination: delivery.destination,
    payload: event.payload !== null ? event.payload : event.raw_body,
  };
  return canonicalJson(envelope);
}

export async function outboundHeaders(
  destination: Destination,
  delivery: DeliveryRow,
  body: string,
  nowSeconds: number,
): Promise<Record<string, string>> {
  const headers = await signHeaders(destination.secret, body, nowSeconds);
  headers[IDEMPOTENCY_HEADER] = delivery.idempotency_key;
  headers[EVENT_ID_HEADER] = delivery.event_id;
  headers["Content-Type"] = "application/json";
  headers["User-Agent"] = "launchbridge-worker";
  return headers;
}

export class Worker {
  readonly db: Database;
  readonly registry: DestinationRegistry;
  readonly receiver: Receiver;
  readonly clock: VirtualClock;
  readonly rng: Prng;
  readonly batchSize: number;
  readonly concurrency: number;
  readonly processedEventsTtlMs: number;
  readonly nonceTtlMs: number;
  onAttempt?: (event: AttemptEvent) => void;

  constructor(
    db: Database,
    registry: DestinationRegistry,
    receiver: Receiver,
    clock: VirtualClock,
    rng: Prng,
    options: WorkerOptions = {},
  ) {
    this.db = db;
    this.registry = registry;
    this.receiver = receiver;
    this.clock = clock;
    this.rng = rng;
    this.batchSize = options.batchSize ?? 50;
    this.concurrency = options.concurrency ?? 8;
    this.processedEventsTtlMs = options.processedEventsTtlMs ?? 72 * 3600 * 1000;
    this.nonceTtlMs = options.nonceTtlMs ?? 600_000;
    this.onAttempt = options.onAttempt;
  }

  /** The claim query: pending rows that are due, oldest first, marked in_progress. */
  claim(now: number): string[] {
    const due = [...this.db.deliveries.values()]
      .filter((d) => d.status === "pending" && d.next_attempt_at !== null && d.next_attempt_at <= now)
      .sort((a, b) => (a.next_attempt_at ?? 0) - (b.next_attempt_at ?? 0))
      .slice(0, this.batchSize);
    for (const d of due) {
      d.status = "in_progress";
      d.updated_at = now;
    }
    return due.map((d) => d.id);
  }

  /** One attempt for one delivery at virtual time `now`; returns the resulting state. */
  async process(deliveryId: string, now: number): Promise<DeliveryStatus | "missing"> {
    const delivery = this.db.deliveries.get(deliveryId);
    if (!delivery) return "missing";
    const event = this.db.events.get(delivery.event_id);
    const destination = this.registry.get(delivery.destination);
    if (!event || !destination) {
      delivery.status = "failed";
      delivery.last_error = event ? "unknown destination" : "event missing";
      delivery.updated_at = now;
      return delivery.status;
    }

    const attemptNumber = delivery.attempts + 1;
    const body = buildEnvelope(event, delivery, this.clock.iso(event.received_at));
    const headers = await outboundHeaders(destination, delivery, body, Math.floor(now / 1000));
    let statusCode: number | null = null;
    let error: string | null = null;
    let durationMs = 0;
    try {
      const hookName = destination.url.slice(destination.url.lastIndexOf("/") + 1);
      const response = await this.receiver.hook(hookName, body, headers, now);
      statusCode = response.status;
      durationMs = response.duration_ms;
      if (statusCode < 200 || statusCode >= 300) {
        error = `HTTP ${statusCode}: ${JSON.stringify(response.body).slice(0, 200)}`;
      }
    } catch (exc) {
      if (exc instanceof TransportError) {
        error = `${exc.name}: ${exc.message}`.slice(0, 500);
        durationMs = Math.round(this.rng.uniform(8, 30));
      } else {
        throw exc;
      }
    }

    const outcome = classify(statusCode);
    const finishedAt = now + durationMs;
    const attempt: DeliveryAttemptRow = {
      id: this.db.nextSerial(),
      delivery_id: delivery.id,
      attempt_number: attemptNumber,
      started_at: now,
      duration_ms: durationMs,
      status_code: statusCode,
      outcome,
      error,
      succeeded: outcome === "success",
      backoff_ms: null,
    };
    this.db.attempts.push(attempt);
    delivery.attempts = attemptNumber;
    delivery.last_status_code = statusCode;
    delivery.last_error = error;
    delivery.updated_at = finishedAt;
    const policy = destination.retry;

    if (outcome === "success") {
      delivery.status = "delivered";
      delivery.delivered_at = finishedAt;
      delivery.next_attempt_at = null;
      delivery.latency_ms = Math.max(finishedAt - delivery.created_at, 0);
    } else if (outcome === "transient" && attemptNumber < delivery.max_attempts) {
      const delayMs = Math.round(policy.backoff(attemptNumber, this.rng) * 1000);
      attempt.backoff_ms = delayMs;
      delivery.status = "pending";
      delivery.next_attempt_at = finishedAt + delayMs;
    } else {
      delivery.status = "failed";
      delivery.next_attempt_at = null;
      if (outcome === "transient") {
        delivery.last_error = `${error}; retries exhausted after ${attemptNumber} attempts`;
      }
    }
    this.onAttempt?.({ delivery: { ...delivery }, attempt: { ...attempt }, state: delivery.status });
    return delivery.status;
  }

  /** Claim and process one batch; returns the number of deliveries processed. */
  async runOnce(now: number = this.clock.now()): Promise<number> {
    const ids = this.claim(now);
    if (ids.length === 0) return 0;
    let waveStart = now;
    for (let offset = 0; offset < ids.length; offset += this.concurrency) {
      const wave = ids.slice(offset, offset + this.concurrency);
      let slowest = 0;
      for (const id of wave) {
        await this.process(id, waveStart);
        const row = this.db.deliveries.get(id);
        if (row) slowest = Math.max(slowest, row.updated_at - waveStart);
      }
      waveStart += slowest;
    }
    this.clock.set(waveStart);
    return ids.length;
  }

  /** Process batches until nothing is due at the current virtual time. */
  async drain(maxBatches = 100): Promise<number> {
    let total = 0;
    for (let i = 0; i < maxBatches; i++) {
      const processed = await this.runOnce(this.clock.now());
      if (processed === 0) break;
      total += processed;
    }
    return total;
  }

  private openDeliveries(): DeliveryRow[] {
    return [...this.db.deliveries.values()].filter(
      (d) => d.status === "pending" || d.status === "in_progress",
    );
  }

  /**
   * Run until every delivery is terminal, advancing the virtual clock across backoff
   * waits. `until` stops early once it returns true (the smoke suite's wait_for).
   */
  async settle(until?: () => boolean, maxVirtualMs = 180_000): Promise<number> {
    const deadline = this.clock.now() + maxVirtualMs;
    let total = 0;
    for (;;) {
      if (until?.()) return total;
      const processed = await this.drain();
      total += processed;
      if (until?.()) return total;
      const open = this.openDeliveries();
      if (open.length === 0) return total;
      const nextDue = Math.min(...open.map((d) => d.next_attempt_at ?? Number.POSITIVE_INFINITY));
      if (!Number.isFinite(nextDue) || nextDue > deadline) return total;
      this.clock.set(nextDue);
    }
  }

  /** Return deliveries stuck in_progress (a crashed worker) to the queue. */
  releaseStale(now: number): number {
    const cutoff = now - STALE_IN_PROGRESS_SECONDS * 1000;
    let released = 0;
    for (const d of this.db.deliveries.values()) {
      if (d.status === "in_progress" && d.updated_at < cutoff) {
        d.status = "pending";
        d.next_attempt_at = now;
        d.updated_at = now;
        released += 1;
      }
    }
    return released;
  }

  cleanupProcessedEvents(now: number): number {
    return this.db.deleteProcessedEventsBefore(now - this.processedEventsTtlMs);
  }

  /** Drop nonces older than the TTL; the timestamp check alone rejects them by then. */
  cleanupNonces(now: number): number {
    return this.db.deleteSignatureNoncesBefore(now - this.nonceTtlMs);
  }
}
