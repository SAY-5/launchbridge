"""Token bucket: `rate` tokens per second refilled continuously, at most `burst` stored."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    rate: float
    burst: int
    tokens: float = field(init=False)
    updated: float | None = field(init=False, default=None)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be positive")
        if self.burst < 1:
            raise ValueError("burst must be at least 1")
        self.tokens = float(self.burst)

    def _refill(self, now: float) -> None:
        if self.updated is None:
            self.updated = now
            return
        elapsed = max(now - self.updated, 0.0)
        self.tokens = min(float(self.burst), self.tokens + elapsed * self.rate)
        self.updated = now

    def available(self, now: float) -> float:
        with self._lock:
            self._refill(now)
            return self.tokens

    def seconds_until(self, now: float, n: int = 1) -> float:
        """Time until `n` tokens are available; 0 when they already are."""
        with self._lock:
            self._refill(now)
            deficit = n - self.tokens
            return 0.0 if deficit <= 0 else deficit / self.rate

    def try_acquire(self, now: float, n: int = 1) -> bool:
        with self._lock:
            self._refill(now)
            if self.tokens < n:
                return False
            self.tokens -= n
            return True
