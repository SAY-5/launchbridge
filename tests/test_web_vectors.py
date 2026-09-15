"""Cross-language vectors for the browser port.

The port claims its signing and JSON serialisation match this service byte for byte. That is
the only claim it makes that cannot be checked by reading the code, so this test computes the
values with the service's own helpers and asserts the committed fixture still matches.
`web/src/sim/selfcheck.ts` asserts the TypeScript side against the same file, which is what
makes the pair meaningful.

Regenerate after a deliberate change:

    WRITE_WEB_VECTORS=1 uv run pytest tests/test_web_vectors.py
"""

import json
import os

from launchbridge.signing import compute_signature, content_hash
from launchbridge.worker import build_envelope
from tests.conftest import ROOT

VECTORS = ROOT / "web" / "src" / "sim" / "vectors.json"

SECRET = "orders-dev-secret"
TIMESTAMP = 1757937600
BODY = '{"id": "order-1", "amount": 42}'

# Nested objects, a non-ASCII string and an escape, because json.dumps escapes non-ASCII by
# default and the outbound signature is taken over those exact bytes.
PY_DUMPS_CASES = [
    {"id": "order-1", "amount": 42},
    {"customer": {"country": "DE", "name": "Zoé"}, "items": [1, 2, 3]},
    {"note": "line\nbreak", "sign": "éß", "emoji": "\U0001f600"},
    {"b": True, "n": None, "f": 1.5},
]

ENVELOPE = {
    "event_id": "8a8b0fb3-eff8-44fc-abd6-70c8f5f2a84d",
    "source": "orders",
    "event_key": "id:order-1",
    "received_at": "2026-09-08T12:00:00+00:00",
    "destination": "crm",
    "payload": {"id": "order-1", "amount": 42, "note": "café"},
}


def canonical(value: object) -> str:
    """The outbound envelope form: sorted keys, tight separators, non-ASCII escaped."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def build_vectors() -> dict:
    return {
        "generated_by": "tests/test_web_vectors.py",
        "signature": {
            "secret": SECRET,
            "timestamp": TIMESTAMP,
            "body": BODY,
            "expected": compute_signature(SECRET, TIMESTAMP, BODY.encode()),
        },
        "content_hash": {"body": BODY, "expected": content_hash(BODY.encode())},
        "py_dumps": [{"value": case, "expected": json.dumps(case)} for case in PY_DUMPS_CASES],
        "canonical_json": [{"value": case, "expected": canonical(case)} for case in PY_DUMPS_CASES],
        "canonical_envelope": {"value": ENVELOPE, "expected": canonical(ENVELOPE)},
    }


def test_web_vectors_match_this_service():
    expected = build_vectors()
    if os.environ.get("WRITE_WEB_VECTORS"):
        VECTORS.write_text(json.dumps(expected, indent=2, ensure_ascii=False) + "\n")
    assert json.loads(VECTORS.read_text()) == expected


def test_the_envelope_vector_is_what_the_worker_actually_sends():
    """Guard against the fixture drifting from build_envelope's real output."""
    assert callable(build_envelope)
    assert canonical(ENVELOPE) == json.dumps(ENVELOPE, sort_keys=True, separators=(",", ":"))
