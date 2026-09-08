/** Aggregate counts and latency percentiles, the /stats endpoint. */

import type { DeliveryStatus } from "./models";
import type { Database } from "./store";

export interface Stats {
  events: { accepted: number; deduplicated: number; received: number };
  deliveries: Record<DeliveryStatus, number>;
  retries: number;
  replays: { requested: number; delivered: number };
  signature_rejections: number;
  latency_ms: { p50: number | null; p95: number | null };
}

/** percentile_cont: linear interpolation between sorted values. */
export function percentileCont(values: number[], fraction: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const position = (sorted.length - 1) * fraction;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if (lower === upper) return sorted[lower];
  const weight = position - lower;
  return sorted[lower] * (1 - weight) + sorted[upper] * weight;
}

export function collectStats(
  db: Database,
  filter: { source?: string | null; since?: number | null } = {},
): Stats {
  const events = [...db.events.values()].filter((e) => {
    if (filter.source && e.source !== filter.source) return false;
    if (filter.since != null && e.received_at < filter.since) return false;
    return true;
  });
  const eventIds = new Set(events.map((e) => e.id));
  const accepted = events.filter((e) => e.status === "accepted").length;
  const deduplicated = events.filter((e) => e.status === "deduplicated").length;

  const deliveries = [...db.deliveries.values()].filter((d) => eventIds.has(d.event_id));
  const deliveryIds = new Set(deliveries.map((d) => d.id));
  const byStatus: Record<DeliveryStatus, number> = {
    pending: 0,
    in_progress: 0,
    delivered: 0,
    failed: 0,
    replayed: 0,
  };
  let retried = 0;
  const latencies: number[] = [];
  for (const d of deliveries) {
    byStatus[d.status] += 1;
    retried += Math.max(d.attempts - 1, 0);
    if (d.status === "delivered" && d.latency_ms !== null) latencies.push(d.latency_ms);
  }
  const replayed = db.replays.filter((r) => deliveryIds.has(r.original_delivery_id)).length;
  const replayedDelivered = deliveries.filter(
    (d) => d.replay_of !== null && d.status === "delivered",
  ).length;

  const rejections = db.rejections.filter((r) => {
    if (filter.source && r.source !== filter.source) return false;
    if (filter.since != null && r.rejected_at < filter.since) return false;
    return true;
  }).length;

  const round1 = (v: number | null) => (v === null ? null : Math.round(v * 10) / 10);
  return {
    events: { accepted, deduplicated, received: accepted + deduplicated },
    deliveries: byStatus,
    retries: retried,
    replays: { requested: replayed, delivered: replayedDelivered },
    signature_rejections: rejections,
    latency_ms: {
      p50: round1(percentileCont(latencies, 0.5)),
      p95: round1(percentileCont(latencies, 0.95)),
    },
  };
}
