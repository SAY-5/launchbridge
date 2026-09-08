"""Circuit breaker per destination: closed -> open after consecutive transient failures,
open -> half_open after the recovery period, half_open -> closed on a successful probe or
back to open on a failed one. Deliveries are deferred, never failed, while the circuit is open.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum

HALF_OPEN_RETRY_SECONDS = 1.0


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    half_open_max: int = 1
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    half_open_probes: int = field(default=0, init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be positive")
        if self.half_open_max < 1:
            raise ValueError("half_open_max must be at least 1")
        self.state = BreakerState(self.state)

    def allow(self, now: float) -> tuple[bool, float]:
        """Whether a request may go out now, and otherwise how long to wait."""
        with self._lock:
            if self.state is BreakerState.OPEN:
                assert self.opened_at is not None
                reopen_at = self.opened_at + self.recovery_seconds
                if now < reopen_at:
                    return False, reopen_at - now
                self.state = BreakerState.HALF_OPEN
                self.half_open_probes = 0
            if self.state is BreakerState.HALF_OPEN:
                if self.half_open_probes >= self.half_open_max:
                    return False, HALF_OPEN_RETRY_SECONDS
                self.half_open_probes += 1
            return True, 0.0

    def record_success(self) -> BreakerState | None:
        """Returns the new state when a transition happened."""
        with self._lock:
            previous = self.state
            self.state = BreakerState.CLOSED
            self.consecutive_failures = 0
            self.opened_at = None
            self.half_open_probes = 0
            return None if previous is BreakerState.CLOSED else self.state

    def record_failure(self, now: float) -> BreakerState | None:
        with self._lock:
            self.consecutive_failures += 1
            if self.state is BreakerState.HALF_OPEN or (
                self.state is BreakerState.CLOSED
                and self.consecutive_failures >= self.failure_threshold
            ):
                self.state = BreakerState.OPEN
                self.opened_at = now
                self.half_open_probes = 0
                return self.state
            return None
