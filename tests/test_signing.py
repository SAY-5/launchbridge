import time

import pytest

from launchbridge.signing import (
    SIGNATURE_PREFIX,
    SignatureError,
    compute_signature,
    content_hash,
    sign_headers,
    verify_signature,
    verify_signature_any,
)

SECRET = "top-secret"
BODY = b'{"id": "evt_1", "amount": 10}'


def test_roundtrip_accepts_valid_signature():
    now = int(time.time())
    headers = sign_headers(SECRET, BODY, now)
    accepted = verify_signature(
        SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY, 300, now=now
    )
    assert accepted == now
    assert headers["X-Signature"].startswith(SIGNATURE_PREFIX)


def test_wrong_secret_is_rejected():
    now = int(time.time())
    headers = sign_headers("other-secret", BODY, now)
    with pytest.raises(SignatureError) as exc:
        verify_signature(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY, 300, now)
    assert exc.value.reason == "invalid_signature"


def test_tampered_body_is_rejected():
    now = int(time.time())
    headers = sign_headers(SECRET, BODY, now)
    with pytest.raises(SignatureError) as exc:
        verify_signature(
            SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY + b" ", 300, now
        )
    assert exc.value.reason == "invalid_signature"


@pytest.mark.parametrize("skew", [301, -301, 4000])
def test_stale_timestamp_is_rejected(skew):
    now = int(time.time())
    headers = sign_headers(SECRET, BODY, now + skew)
    with pytest.raises(SignatureError) as exc:
        verify_signature(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY, 300, now)
    assert exc.value.reason == "stale_timestamp"


def test_timestamp_inside_tolerance_is_accepted():
    now = int(time.time())
    headers = sign_headers(SECRET, BODY, now - 299)
    verify_signature(SECRET, headers["X-Timestamp"], headers["X-Signature"], BODY, 300, now)


@pytest.mark.parametrize(
    ("timestamp", "signature", "reason"),
    [
        (None, "sha256=abc", "missing_timestamp"),
        ("yesterday", "sha256=abc", "invalid_timestamp"),
        ("0", None, "stale_timestamp"),
    ],
)
def test_malformed_headers(timestamp, signature, reason):
    with pytest.raises(SignatureError) as exc:
        verify_signature(SECRET, timestamp, signature, BODY, 300, now=1_000_000)
    assert exc.value.reason == reason


def test_missing_signature_and_bad_prefix():
    now = 1_000_000
    with pytest.raises(SignatureError) as exc:
        verify_signature(SECRET, str(now), None, BODY, 300, now=now)
    assert exc.value.reason == "missing_signature"
    digest = compute_signature(SECRET, now, BODY)[len(SIGNATURE_PREFIX) :]
    with pytest.raises(SignatureError) as exc:
        verify_signature(SECRET, str(now), f"md5={digest}", BODY, 300, now=now)
    assert exc.value.reason == "invalid_signature"


def test_signature_binds_timestamp():
    assert compute_signature(SECRET, 1, BODY) != compute_signature(SECRET, 2, BODY)


def test_content_hash_is_stable_sha256():
    assert content_hash(BODY) == content_hash(bytes(BODY))
    assert len(content_hash(BODY)) == 64


def test_verify_any_reports_which_secret_matched():
    now = int(time.time())
    candidates = [("current", "new-secret"), ("previous", SECRET)]
    headers = sign_headers(SECRET, BODY, now)
    assert verify_signature_any(
        candidates, headers["X-Timestamp"], headers["X-Signature"], BODY, 300, now
    ) == (now, "previous")
    headers = sign_headers("new-secret", BODY, now)
    assert verify_signature_any(
        candidates, headers["X-Timestamp"], headers["X-Signature"], BODY, 300, now
    ) == (now, "current")
    with pytest.raises(SignatureError) as exc:
        verify_signature_any(candidates, str(now), "sha256=nope", BODY, 300, now)
    assert exc.value.reason == "invalid_signature"
    with pytest.raises(SignatureError) as exc:
        verify_signature_any(candidates, str(now - 900), headers["X-Signature"], BODY, 300, now)
    assert exc.value.reason == "stale_timestamp"
    with pytest.raises(ValueError):
        verify_signature_any([], str(now), headers["X-Signature"], BODY, 300, now)


def test_sign_headers_adds_previous_signature_and_key_id():
    now = int(time.time())
    headers = sign_headers("new", BODY, now, previous_secret="old", key_id="2026-09")
    assert headers["X-Signature"] == compute_signature("new", now, BODY)
    assert headers["X-Signature-Previous"] == compute_signature("old", now, BODY)
    assert headers["X-Key-Id"] == "2026-09"
    assert set(sign_headers("new", BODY, now)) == {"X-Timestamp", "X-Signature"}
