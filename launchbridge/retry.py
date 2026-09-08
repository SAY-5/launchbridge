"""Retry policy: bounded attempts, exponential backoff with jitter, outcome classification."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    SUCCESS = "success"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


RETRYABLE_STATUSES = frozenset({408, 425, 429})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.2
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delays must not be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def base_backoff(self, attempt: int) -> float:
        """Deterministic delay before the retry that follows `attempt` (1-based)."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        raw = self.base_delay_seconds * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_delay_seconds)

    def backoff(self, attempt: int, rng: random.Random | None = None) -> float:
        """Backoff with symmetric jitter applied; never exceeds max_delay_seconds."""
        base = self.base_backoff(attempt)
        if self.jitter == 0 or base == 0:
            return base
        rng = rng or random
        factor = 1 + rng.uniform(-self.jitter, self.jitter)
        return min(base * factor, self.max_delay_seconds)

    def should_retry(self, attempt: int, outcome: Outcome) -> bool:
        return outcome is Outcome.TRANSIENT and attempt < self.max_attempts


def classify(status_code: int | None) -> Outcome:
    """Map an HTTP status (None for a transport error) to a retry outcome."""
    if status_code is None:
        return Outcome.TRANSIENT
    if 200 <= status_code < 300:
        return Outcome.SUCCESS
    if status_code in RETRYABLE_STATUSES or status_code >= 500:
        return Outcome.TRANSIENT
    return Outcome.PERMANENT
