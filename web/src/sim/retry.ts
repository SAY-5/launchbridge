/** Retry policy: bounded attempts, exponential backoff with jitter, outcome classification. */

import type { Prng } from "./prng";

export type Outcome = "success" | "transient" | "permanent";

export const RETRYABLE_STATUSES = new Set([408, 425, 429]);

export interface RetryPolicyInput {
  maxAttempts?: number;
  baseDelaySeconds?: number;
  maxDelaySeconds?: number;
  multiplier?: number;
  jitter?: number;
  timeoutSeconds?: number;
}

export class RetryPolicy {
  readonly maxAttempts: number;
  readonly baseDelaySeconds: number;
  readonly maxDelaySeconds: number;
  readonly multiplier: number;
  readonly jitter: number;
  readonly timeoutSeconds: number;

  constructor(input: RetryPolicyInput = {}) {
    this.maxAttempts = input.maxAttempts ?? 5;
    this.baseDelaySeconds = input.baseDelaySeconds ?? 0.5;
    this.maxDelaySeconds = input.maxDelaySeconds ?? 30;
    this.multiplier = input.multiplier ?? 2;
    this.jitter = input.jitter ?? 0.2;
    this.timeoutSeconds = input.timeoutSeconds ?? 5;
    if (this.maxAttempts < 1) throw new Error("max_attempts must be at least 1");
    if (this.baseDelaySeconds < 0 || this.maxDelaySeconds < 0) {
      throw new Error("delays must not be negative");
    }
    if (this.maxDelaySeconds < this.baseDelaySeconds) {
      throw new Error("max_delay_seconds must be >= base_delay_seconds");
    }
    if (this.multiplier < 1) throw new Error("multiplier must be at least 1");
    if (this.jitter < 0 || this.jitter > 1) throw new Error("jitter must be between 0 and 1");
    if (this.timeoutSeconds <= 0) throw new Error("timeout_seconds must be positive");
  }

  /** Deterministic delay before the retry that follows `attempt` (1-based). */
  baseBackoff(attempt: number): number {
    if (attempt < 1) throw new Error("attempt is 1-based");
    const raw = this.baseDelaySeconds * this.multiplier ** (attempt - 1);
    return Math.min(raw, this.maxDelaySeconds);
  }

  /** Backoff with symmetric jitter applied; never exceeds max_delay_seconds. */
  backoff(attempt: number, rng: Prng): number {
    const base = this.baseBackoff(attempt);
    if (this.jitter === 0 || base === 0) return base;
    const factor = 1 + rng.uniform(-this.jitter, this.jitter);
    return Math.min(base * factor, this.maxDelaySeconds);
  }

  shouldRetry(attempt: number, outcome: Outcome): boolean {
    return outcome === "transient" && attempt < this.maxAttempts;
  }
}

/** Map an HTTP status (null for a transport error) to a retry outcome. */
export function classify(statusCode: number | null): Outcome {
  if (statusCode === null) return "transient";
  if (statusCode >= 200 && statusCode < 300) return "success";
  if (RETRYABLE_STATUSES.has(statusCode) || statusCode >= 500) return "transient";
  return "permanent";
}
