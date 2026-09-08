/**
 * In-memory stand-in for the PostgreSQL schema. The two unique constraints on the dedup
 * ledger are enforced the way the database does it: (source, event_key) is the ON CONFLICT
 * arbiter that turns a repeat into a no-op, and signature raises an integrity error.
 */

import type {
  DeliveryAttemptRow,
  DeliveryRow,
  EventRow,
  ProcessedEventRow,
  ReplayRow,
  SignatureRejectionRow,
} from "./models";
import type { Prng } from "./prng";

export class IntegrityError extends Error {
  constructor(constraint: string) {
    super(`duplicate key value violates unique constraint "${constraint}"`);
    this.name = "IntegrityError";
  }
}

export class Database {
  readonly events = new Map<string, EventRow>();
  readonly processedEvents = new Map<number, ProcessedEventRow>();
  readonly deliveries = new Map<string, DeliveryRow>();
  readonly attempts: DeliveryAttemptRow[] = [];
  readonly replays: ReplayRow[] = [];
  readonly rejections: SignatureRejectionRow[] = [];

  private readonly ledgerBySourceKey = new Map<string, number>();
  private readonly ledgerBySignature = new Map<string, number>();
  private serial = 0;
  private readonly rng: Prng;

  constructor(rng: Prng) {
    this.rng = rng;
  }

  uuid(): string {
    return this.rng.uuid();
  }

  nextSerial(): number {
    this.serial += 1;
    return this.serial;
  }

  /** `SELECT id FROM processed_events WHERE signature = :sig`. */
  ledgerIdForSignature(signature: string): number | null {
    return this.ledgerBySignature.get(signature) ?? null;
  }

  /** `SELECT * FROM processed_events WHERE source = :source AND event_key = :key`. */
  ledgerRow(source: string, eventKey: string): ProcessedEventRow | null {
    const id = this.ledgerBySourceKey.get(`${source} ${eventKey}`);
    return id === undefined ? null : (this.processedEvents.get(id) ?? null);
  }

  /**
   * `INSERT ... ON CONFLICT (source, event_key) DO NOTHING RETURNING id`.
   * Returns the new id, or null when the ledger already holds the key.
   */
  insertProcessedEvent(row: Omit<ProcessedEventRow, "id">): number | null {
    if (this.ledgerBySignature.has(row.signature)) {
      throw new IntegrityError("uq_processed_events_signature");
    }
    const key = `${row.source} ${row.event_key}`;
    if (this.ledgerBySourceKey.has(key)) return null;
    const id = this.nextSerial();
    this.processedEvents.set(id, { ...row, id });
    this.ledgerBySourceKey.set(key, id);
    this.ledgerBySignature.set(row.signature, id);
    return id;
  }

  deleteProcessedEventsBefore(cutoff: number): number {
    let removed = 0;
    for (const [id, row] of this.processedEvents) {
      if (row.processed_at < cutoff) {
        this.processedEvents.delete(id);
        this.ledgerBySourceKey.delete(`${row.source} ${row.event_key}`);
        this.ledgerBySignature.delete(row.signature);
        removed += 1;
      }
    }
    return removed;
  }

  attemptsFor(deliveryId: string): DeliveryAttemptRow[] {
    return this.attempts
      .filter((a) => a.delivery_id === deliveryId)
      .sort((a, b) => a.attempt_number - b.attempt_number);
  }

  deliveriesForEvent(eventId: string): DeliveryRow[] {
    return [...this.deliveries.values()].filter((d) => d.event_id === eventId);
  }
}
