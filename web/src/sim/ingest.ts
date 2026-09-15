/** Inbound event recording: signature replay guard, dedup ledger insert, delivery fan-out. */

import type { DestinationRegistry } from "./destinations";
import { parseJson, type Json } from "./json";
import type { DeliveryRow, EventRow } from "./models";
import { contentHash } from "./signing";
import { Database, IntegrityError } from "./store";

export const MAX_EVENT_KEY_LENGTH = 255;

export class SignatureReplayedError extends Error {
  constructor(signature: string) {
    super(signature);
    this.name = "SignatureReplayedError";
  }
}

export interface IngestResult {
  event_id: string;
  deduplicated: boolean;
  delivery_ids: string[];
}

/** Prefer an explicit event id header, then a payload `id`, then the content hash. */
export function deriveEventKey(
  payload: Json | undefined,
  bodyHash: string,
  headerEventId: string | undefined,
): string {
  if (headerEventId) return `id:${headerEventId}`.slice(0, MAX_EVENT_KEY_LENGTH);
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const id = payload["id"];
    if (id !== undefined && id !== null && id !== "") {
      return `id:${String(id)}`.slice(0, MAX_EVENT_KEY_LENGTH);
    }
  }
  return `hash:${bodyHash}`;
}

export function parsePayload(body: string): Json | null {
  const parsed = parseJson(body);
  if (parsed === undefined) return null;
  return typeof parsed === "object" ? parsed : null;
}

export function idempotencyKey(eventId: string, destination: string): string {
  return `${eventId}:${destination}`;
}

export interface IngestInput {
  source: string;
  body: string;
  headers: Record<string, string>;
  signature: string;
  signedAt: number;
  now: number;
  registry: DestinationRegistry;
}

/**
 * Record an inbound request and enqueue deliveries unless it is a duplicate.
 * Throws SignatureReplayedError when the signature was seen before.
 */
export async function ingestEvent(db: Database, input: IngestInput): Promise<IngestResult> {
  const { source, body, headers, signature, signedAt, now, registry } = input;
  // The nonce store is written before anything else and is keyed by signature alone, so the
  // exact same request is refused whether its first arrival was accepted or deduplicated.
  if (!db.insertSignatureNonce(signature, now)) {
    throw new SignatureReplayedError(signature);
  }

  const payload = parsePayload(body);
  const hash = await contentHash(body);
  const eventKey = deriveEventKey(payload ?? undefined, hash, headers["x-event-id"]);
  const event: EventRow = {
    id: db.uuid(),
    source,
    event_key: eventKey,
    content_hash: hash,
    signature,
    signed_at: signedAt,
    payload,
    raw_body: body,
    headers,
    status: "accepted",
    received_at: now,
  };
  db.events.set(event.id, event);

  let inserted: number | null;
  try {
    inserted = db.insertProcessedEvent({
      source,
      event_key: eventKey,
      signature,
      event_id: event.id,
      processed_at: now,
    });
  } catch (error) {
    if (error instanceof IntegrityError) {
      db.events.delete(event.id);
      throw new SignatureReplayedError(signature);
    }
    throw error;
  }

  if (inserted === null) {
    event.status = "deduplicated";
    return { event_id: event.id, deduplicated: true, delivery_ids: [] };
  }

  const deliveries: DeliveryRow[] = registry.forSource(source).map((dest) => ({
    id: db.uuid(),
    event_id: event.id,
    destination: dest.name,
    idempotency_key: idempotencyKey(event.id, dest.name),
    status: "pending",
    series: 1,
    attempts: 0,
    max_attempts: dest.retry.maxAttempts,
    next_attempt_at: now,
    last_status_code: null,
    last_error: null,
    replay_of: null,
    created_at: now,
    updated_at: now,
    delivered_at: null,
    replayed_at: null,
    latency_ms: null,
  }));
  for (const d of deliveries) db.deliveries.set(d.id, d);
  return { event_id: event.id, deduplicated: false, delivery_ids: deliveries.map((d) => d.id) };
}

export function recordRejection(db: Database, source: string, reason: string, now: number): void {
  db.rejections.push({ id: db.nextSerial(), source, reason, rejected_at: now });
}
