/** Row shapes mirroring the PostgreSQL tables. Timestamps are virtual-clock milliseconds. */

import type { Json } from "./json";

export type EventStatus = "accepted" | "deduplicated";
export type DeliveryStatus = "pending" | "in_progress" | "delivered" | "failed" | "replayed";

export interface EventRow {
  id: string;
  source: string;
  event_key: string;
  content_hash: string;
  signature: string;
  signed_at: number;
  payload: Json | null;
  raw_body: string;
  headers: Record<string, string>;
  status: EventStatus;
  received_at: number;
}

export interface ProcessedEventRow {
  id: number;
  source: string;
  event_key: string;
  signature: string;
  event_id: string;
  processed_at: number;
}

export interface DeliveryRow {
  id: string;
  event_id: string;
  destination: string;
  idempotency_key: string;
  status: DeliveryStatus;
  series: number;
  attempts: number;
  max_attempts: number;
  next_attempt_at: number | null;
  last_status_code: number | null;
  last_error: string | null;
  replay_of: string | null;
  created_at: number;
  updated_at: number;
  delivered_at: number | null;
  replayed_at: number | null;
  latency_ms: number | null;
}

export interface DeliveryAttemptRow {
  id: number;
  delivery_id: string;
  attempt_number: number;
  started_at: number;
  duration_ms: number;
  status_code: number | null;
  outcome: "success" | "transient" | "permanent";
  error: string | null;
  succeeded: boolean;
  /** Backoff scheduled after this attempt, in ms (null when the attempt was terminal). */
  backoff_ms: number | null;
}

export interface ReplayRow {
  id: number;
  original_delivery_id: string;
  new_delivery_id: string;
  actor: string;
  mode: "single" | "bulk";
  reason: string | null;
  requested_at: number;
}

export interface SignatureRejectionRow {
  id: number;
  source: string;
  reason: string;
  rejected_at: number;
}
