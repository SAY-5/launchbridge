"""HMAC-SHA256 request signing shared by inbound verification and outbound delivery.

The signed message is `<unix timestamp>.<raw body>`. Binding the timestamp into the
signature bounds how long a captured request stays valid; the nonce store closes the
remaining window. During a key rotation a request may verify against the current or the
previous secret, and outbound requests carry a second signature under `X-Signature-Previous`
so receivers that still hold the old key keep accepting them.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Signature"
PREVIOUS_SIGNATURE_HEADER = "X-Signature-Previous"
KEY_ID_HEADER = "X-Key-Id"
TIMESTAMP_HEADER = "X-Timestamp"
IDEMPOTENCY_HEADER = "X-Idempotency-Key"
EVENT_ID_HEADER = "X-Event-Id"
SIGNATURE_PREFIX = "sha256="


class SignatureError(Exception):
    """Raised when a request fails signature verification."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def compute_signature(secret: str, timestamp: int | str, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def sign_headers(
    secret: str,
    body: bytes,
    timestamp: int | None = None,
    *,
    previous_secret: str | None = None,
    key_id: str | None = None,
) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else int(timestamp)
    headers = {
        TIMESTAMP_HEADER: str(ts),
        SIGNATURE_HEADER: compute_signature(secret, ts, body),
    }
    if previous_secret:
        headers[PREVIOUS_SIGNATURE_HEADER] = compute_signature(previous_secret, ts, body)
    if key_id:
        headers[KEY_ID_HEADER] = key_id
    return headers


def verify_signature(
    secret: str,
    timestamp_header: str | None,
    signature_header: str | None,
    body: bytes,
    tolerance_seconds: int,
    now: float | None = None,
) -> int:
    """Verify headers against `body`; return the accepted timestamp.

    Raises SignatureError with a machine-readable `reason`:
    missing_timestamp, invalid_timestamp, stale_timestamp, missing_signature,
    invalid_signature.
    """
    if not timestamp_header:
        raise SignatureError("missing_timestamp", f"{TIMESTAMP_HEADER} header is required")
    try:
        timestamp = int(timestamp_header)
    except ValueError as exc:
        raise SignatureError("invalid_timestamp", "timestamp must be unix seconds") from exc

    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise SignatureError(
            "stale_timestamp",
            f"timestamp outside the {tolerance_seconds}s tolerance window",
        )

    if not signature_header:
        raise SignatureError("missing_signature", f"{SIGNATURE_HEADER} header is required")
    if not signature_header.startswith(SIGNATURE_PREFIX):
        raise SignatureError("invalid_signature", f"signature must start with {SIGNATURE_PREFIX}")

    expected = compute_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature_header):
        raise SignatureError("invalid_signature", "signature does not match body")
    return timestamp


def verify_signature_any(
    secrets: list[tuple[str, str]],
    timestamp_header: str | None,
    signature_header: str | None,
    body: bytes,
    tolerance_seconds: int,
    now: float | None = None,
) -> tuple[int, str]:
    """Verify against several `(label, secret)` candidates; return (timestamp, label).

    Every candidate is tried so the work done does not reveal which one matched. Header and
    timestamp errors are raised as by `verify_signature`; a body that matches none raises
    `invalid_signature`.
    """
    if not secrets:
        raise ValueError("at least one secret is required")
    matched: str | None = None
    timestamp = 0
    first_error: SignatureError | None = None
    for label, secret in secrets:
        try:
            timestamp = verify_signature(
                secret, timestamp_header, signature_header, body, tolerance_seconds, now
            )
        except SignatureError as exc:
            if exc.reason != "invalid_signature":
                raise
            first_error = first_error or exc
            continue
        if matched is None:
            matched = label
    if matched is None:
        assert first_error is not None
        raise first_error
    return timestamp, matched


def content_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
