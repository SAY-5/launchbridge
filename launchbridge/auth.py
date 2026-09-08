"""API-key authentication for admin endpoints."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from launchbridge.config import Settings, get_settings

API_KEY_HEADER = "X-API-Key"
_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def resolve_api_key(presented: str | None, keys: dict[str, str]) -> str | None:
    """Return the label of the matching key, comparing in constant time."""
    if not presented:
        return None
    matched: str | None = None
    for label, key in keys.items():
        if hmac.compare_digest(key.encode(), presented.encode()):
            matched = label
    return matched


def require_api_key(
    presented: str | None = Security(_header),
    settings: Settings = Depends(get_settings),
) -> str:
    if not settings.admin_api_keys:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="admin API keys are not configured"
        )
    actor = resolve_api_key(presented, settings.admin_api_keys)
    if actor is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")
    return actor
