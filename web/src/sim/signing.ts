/**
 * HMAC-SHA256 request signing shared by inbound verification and outbound delivery.
 * The signed message is `<unix timestamp>.<raw body>`; binding the timestamp into the
 * digest lets the receiver reject stale requests without a nonce store.
 */

export const SIGNATURE_HEADER = "X-Signature";
export const TIMESTAMP_HEADER = "X-Timestamp";
export const IDEMPOTENCY_HEADER = "X-Idempotency-Key";
export const EVENT_ID_HEADER = "X-Event-Id";
export const SIGNATURE_PREFIX = "sha256=";

export type RejectionReason =
  | "missing_timestamp"
  | "invalid_timestamp"
  | "stale_timestamp"
  | "missing_signature"
  | "invalid_signature"
  | "replayed_signature";

export class SignatureError extends Error {
  readonly reason: RejectionReason;
  readonly detail: string;

  constructor(reason: RejectionReason, detail: string) {
    super(detail);
    this.name = "SignatureError";
    this.reason = reason;
    this.detail = detail;
  }
}

const encoder = new TextEncoder();
const keyCache = new Map<string, Promise<CryptoKey>>();

function hmacKey(secret: string): Promise<CryptoKey> {
  let key = keyCache.get(secret);
  if (!key) {
    key = crypto.subtle.importKey(
      "raw",
      encoder.encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    keyCache.set(secret, key);
  }
  return key;
}

function toHex(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

/** The exact text that gets signed: `<timestamp>.` followed by the raw body. */
export function signedMessage(timestamp: number | string, body: string): string {
  return `${timestamp}.${body}`;
}

export async function computeSignature(
  secret: string,
  timestamp: number | string,
  body: string,
): Promise<string> {
  const key = await hmacKey(secret);
  const digest = await crypto.subtle.sign(
    "HMAC",
    key,
    encoder.encode(signedMessage(timestamp, body)),
  );
  return SIGNATURE_PREFIX + toHex(digest);
}

export async function signHeaders(
  secret: string,
  body: string,
  timestamp: number,
): Promise<Record<string, string>> {
  const ts = Math.floor(timestamp);
  return {
    [TIMESTAMP_HEADER]: String(ts),
    [SIGNATURE_HEADER]: await computeSignature(secret, ts, body),
  };
}

/** Constant-time string comparison, the browser counterpart of hmac.compare_digest. */
export function timingSafeEqual(a: string, b: string): boolean {
  const ab = encoder.encode(a);
  const bb = encoder.encode(b);
  let diff = ab.length ^ bb.length;
  const n = Math.max(ab.length, bb.length, 1);
  for (let i = 0; i < n; i++) {
    diff |= (ab[i % Math.max(ab.length, 1)] ?? 0) ^ (bb[i % Math.max(bb.length, 1)] ?? 0);
  }
  return diff === 0;
}

function parseUnixSeconds(text: string): number | null {
  const trimmed = text.trim();
  if (!/^[+-]?\d+$/.test(trimmed)) return null;
  return Number.parseInt(trimmed, 10);
}

/**
 * Verify headers against `body`; resolves to the accepted timestamp or throws
 * SignatureError with a machine-readable reason.
 */
export async function verifySignature(
  secret: string,
  timestampHeader: string | undefined,
  signatureHeader: string | undefined,
  body: string,
  toleranceSeconds: number,
  nowSeconds: number,
): Promise<number> {
  if (!timestampHeader) {
    throw new SignatureError("missing_timestamp", `${TIMESTAMP_HEADER} header is required`);
  }
  const timestamp = parseUnixSeconds(timestampHeader);
  if (timestamp === null) {
    throw new SignatureError("invalid_timestamp", "timestamp must be unix seconds");
  }
  if (Math.abs(nowSeconds - timestamp) > toleranceSeconds) {
    throw new SignatureError(
      "stale_timestamp",
      `timestamp outside the ${toleranceSeconds}s tolerance window`,
    );
  }
  if (!signatureHeader) {
    throw new SignatureError("missing_signature", `${SIGNATURE_HEADER} header is required`);
  }
  if (!signatureHeader.startsWith(SIGNATURE_PREFIX)) {
    throw new SignatureError("invalid_signature", `signature must start with ${SIGNATURE_PREFIX}`);
  }
  const expected = await computeSignature(secret, timestamp, body);
  if (!timingSafeEqual(expected, signatureHeader)) {
    throw new SignatureError("invalid_signature", "signature does not match body");
  }
  return timestamp;
}

export async function contentHash(body: string): Promise<string> {
  return toHex(await crypto.subtle.digest("SHA-256", encoder.encode(body)));
}
