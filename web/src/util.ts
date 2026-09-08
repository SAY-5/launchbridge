export const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export function shortId(id: string | null | undefined, length = 8): string {
  if (!id) return "";
  return id.slice(0, length);
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.round(ms)} ms`;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** Split a signature into a short prefix and the rest, for compact display. */
export function splitSignature(sig: string): [string, string] {
  if (!sig.startsWith("sha256=")) return [sig, ""];
  return ["sha256=", sig.slice(7)];
}
