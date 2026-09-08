/** Virtual clock in milliseconds. Rendering never reads the wall clock. */

/** 2026-09-08T12:00:00Z, a fixed epoch so every run is reproducible. */
export const EPOCH_MS = Date.UTC(2026, 8, 8, 12, 0, 0);

export class VirtualClock {
  private ms: number;

  constructor(startMs: number = EPOCH_MS) {
    this.ms = startMs;
  }

  now(): number {
    return this.ms;
  }

  seconds(): number {
    return Math.floor(this.ms / 1000);
  }

  advance(deltaMs: number): void {
    if (deltaMs > 0) this.ms += deltaMs;
  }

  set(ms: number): void {
    if (ms > this.ms) this.ms = ms;
  }

  iso(ms: number = this.ms): string {
    return new Date(ms).toISOString().replace("Z", "+00:00");
  }
}
