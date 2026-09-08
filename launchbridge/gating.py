"""Per-destination dispatch gate: rate limit and circuit breaker in front of the HTTP call."""

from __future__ import annotations

from dataclasses import dataclass

from launchbridge.breaker import BreakerState, CircuitBreaker
from launchbridge.ratelimit import TokenBucket
from launchbridge.retry import Outcome

DEFER_CIRCUIT_OPEN = "circuit_open"
DEFER_RATE_LIMITED = "rate_limited"


@dataclass
class Gate:
    bucket: TokenBucket | None = None
    breaker: CircuitBreaker | None = None

    def check(self, now: float) -> tuple[str | None, float]:
        """Returns (None, 0) when a request may go out, else (reason, seconds to wait).

        The bucket is peeked before the breaker is consulted so a half-open probe slot is
        never spent on a request that the rate limit would hold back anyway.
        """
        if self.bucket is not None:
            wait = self.bucket.seconds_until(now)
            if wait > 0:
                return DEFER_RATE_LIMITED, wait
        if self.breaker is not None:
            allowed, wait = self.breaker.allow(now)
            if not allowed:
                return DEFER_CIRCUIT_OPEN, wait
        if self.bucket is not None:
            self.bucket.try_acquire(now)
        return None, 0.0

    def record(self, outcome: Outcome, now: float) -> BreakerState | None:
        """Feed the attempt outcome to the breaker; permanent failures are not its concern."""
        if self.breaker is None:
            return None
        if outcome is Outcome.SUCCESS:
            return self.breaker.record_success()
        if outcome is Outcome.TRANSIENT:
            return self.breaker.record_failure(now)
        return None

    @property
    def state(self) -> BreakerState:
        return BreakerState.CLOSED if self.breaker is None else self.breaker.state
