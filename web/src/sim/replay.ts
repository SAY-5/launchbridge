/** Replay of failed deliveries: a new attempt series with the same idempotency key. */

import type { DeliveryRow } from "./models";
import type { Database } from "./store";

export class ReplayError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ReplayError";
  }
}

export interface ReplayOptions {
  actor: string;
  mode: "single" | "bulk";
  now: number;
  reason?: string | null;
}

/** Create a fresh pending delivery for a failed one and mark the original replayed. */
export function replayDelivery(db: Database, delivery: DeliveryRow, opts: ReplayOptions): DeliveryRow {
  if (delivery.status !== "failed") {
    throw new ReplayError(
      `delivery ${delivery.id} is ${delivery.status}, only failed can be replayed`,
    );
  }
  const replacement: DeliveryRow = {
    id: db.uuid(),
    event_id: delivery.event_id,
    destination: delivery.destination,
    idempotency_key: delivery.idempotency_key,
    status: "pending",
    series: delivery.series + 1,
    attempts: 0,
    max_attempts: delivery.max_attempts,
    next_attempt_at: opts.now,
    last_status_code: null,
    last_error: null,
    replay_of: delivery.id,
    created_at: opts.now,
    updated_at: opts.now,
    delivered_at: null,
    replayed_at: null,
    latency_ms: null,
  };
  db.deliveries.set(replacement.id, replacement);

  delivery.status = "replayed";
  delivery.replayed_at = opts.now;
  delivery.updated_at = opts.now;
  db.replays.push({
    id: db.nextSerial(),
    original_delivery_id: delivery.id,
    new_delivery_id: replacement.id,
    actor: opts.actor,
    mode: opts.mode,
    reason: opts.reason ?? null,
    requested_at: opts.now,
  });
  return replacement;
}

export interface FailedFilter {
  source?: string | null;
  since?: number | null;
  destination?: string | null;
  limit?: number;
}

export function failedDeliveries(db: Database, filter: FailedFilter): DeliveryRow[] {
  const limit = filter.limit ?? 500;
  return [...db.deliveries.values()]
    .filter((d) => {
      if (d.status !== "failed") return false;
      const event = db.events.get(d.event_id);
      if (!event) return false;
      if (filter.source && event.source !== filter.source) return false;
      if (filter.since != null && event.received_at < filter.since) return false;
      if (filter.destination && d.destination !== filter.destination) return false;
      return true;
    })
    .sort((a, b) => a.created_at - b.created_at)
    .slice(0, limit);
}

export function replayMany(
  db: Database,
  deliveries: DeliveryRow[],
  opts: { actor: string; now: number; reason?: string | null },
): string[] {
  return deliveries.map(
    (d) => replayDelivery(db, d, { actor: opts.actor, mode: "bulk", now: opts.now, reason: opts.reason }).id,
  );
}
