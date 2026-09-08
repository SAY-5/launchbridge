"""Per-source inbound secrets: environment bootstrap, database rotation with an overlap window."""

from __future__ import annotations

import secrets as _secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from launchbridge.config import Settings
from launchbridge.models import SourceSecret

CURRENT = "current"
PREVIOUS = "previous"


class UnknownSourceError(LookupError):
    pass


def generate_secret() -> str:
    return _secrets.token_urlsafe(32)


def source_exists(session: Session, settings: Settings, source: str) -> bool:
    return source in settings.webhook_secrets or session.get(SourceSecret, source) is not None


def source_secrets(
    session: Session, settings: Settings, source: str, now: datetime
) -> list[tuple[str, str]] | None:
    """Candidate `(label, secret)` pairs for a source, or None when the source is unknown.

    A database row overrides the environment; its previous secret is only offered while the
    overlap window is open.
    """
    row = session.get(SourceSecret, source)
    if row is None:
        configured = settings.webhook_secrets.get(source)
        return None if configured is None else [(CURRENT, configured)]
    candidates = [(CURRENT, row.current_secret)]
    if row.previous_secret and row.previous_expires_at and row.previous_expires_at > now:
        candidates.append((PREVIOUS, row.previous_secret))
    return candidates


def rotate_source(
    session: Session,
    settings: Settings,
    source: str,
    *,
    now: datetime,
    new_secret: str | None = None,
    overlap_seconds: int,
) -> SourceSecret:
    """Make `new_secret` current and keep the old one accepted for `overlap_seconds`.

    Rotating again inside the window replaces the previous secret; only two are ever live.
    Commits.
    """
    row = session.get(SourceSecret, source, with_for_update=True)
    if row is None:
        configured = settings.webhook_secrets.get(source)
        if configured is None:
            raise UnknownSourceError(source)
        row = SourceSecret(source=source, current_secret=configured, rotated_at=now, created_at=now)
        session.add(row)
    row.previous_secret = row.current_secret
    row.previous_expires_at = now + timedelta(seconds=overlap_seconds)
    row.current_secret = new_secret or generate_secret()
    row.rotated_at = now
    session.commit()
    return row


def list_sources(session: Session, settings: Settings, now: datetime) -> list[dict]:
    rows = {row.source: row for row in session.scalars(select(SourceSecret))}
    names = sorted(set(settings.webhook_secrets) | set(rows))
    result = []
    for name in names:
        row = rows.get(name)
        result.append(
            {
                "source": name,
                "secret_from": "database" if row else "environment",
                "created_at": row.created_at if row else None,
                "rotated_at": row.rotated_at if row else None,
                "previous_expires_at": row.previous_expires_at if row else None,
                "previous_active": bool(
                    row and row.previous_secret and row.previous_expires_at > now
                ),
            }
        )
    return result
