import pytest

from launchbridge.breaker import HALF_OPEN_RETRY_SECONDS, BreakerState, CircuitBreaker
from launchbridge.gating import DEFER_CIRCUIT_OPEN, DEFER_RATE_LIMITED, Gate
from launchbridge.ratelimit import TokenBucket
from launchbridge.retry import Outcome


def test_closed_opens_after_consecutive_failures_and_recovers_via_half_open():
    breaker = CircuitBreaker(failure_threshold=3, recovery_seconds=10, half_open_max=2)
    assert breaker.allow(0.0) == (True, 0.0)
    assert breaker.record_failure(1.0) is None
    assert breaker.record_success() is None, "a success in closed is not a transition"
    assert breaker.consecutive_failures == 0
    assert breaker.record_failure(2.0) is None
    assert breaker.record_failure(3.0) is None
    assert breaker.record_failure(4.0) is BreakerState.OPEN
    assert breaker.opened_at == 4.0

    assert breaker.allow(5.0) == (False, 9.0)
    assert breaker.allow(13.9) == (False, pytest.approx(0.1))
    assert breaker.allow(14.0) == (True, 0.0), "first probe after recovery"
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow(14.0) == (True, 0.0), "second probe allowed by half_open_max"
    assert breaker.allow(14.0) == (False, HALF_OPEN_RETRY_SECONDS)

    assert breaker.record_success() is BreakerState.CLOSED
    assert breaker.consecutive_failures == 0 and breaker.opened_at is None
    assert breaker.allow(14.0) == (True, 0.0)


def test_failed_probe_reopens_with_a_fresh_recovery_window():
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=5)
    assert breaker.record_failure(0.0) is BreakerState.OPEN
    assert breaker.allow(5.0) == (True, 0.0)
    assert breaker.record_failure(5.5) is BreakerState.OPEN
    assert breaker.opened_at == 5.5
    assert breaker.allow(9.0) == (False, 1.5)


@pytest.mark.parametrize(
    "kwargs",
    [{"failure_threshold": 0}, {"recovery_seconds": 0}, {"half_open_max": 0}],
)
def test_invalid_breakers_are_rejected(kwargs):
    with pytest.raises(ValueError):
        CircuitBreaker(**kwargs)


def test_gate_orders_rate_limit_before_breaker_and_ignores_permanent_failures():
    gate = Gate(
        bucket=TokenBucket(rate=1, burst=1),
        breaker=CircuitBreaker(failure_threshold=1, recovery_seconds=10),
    )
    assert gate.check(0.0) == (None, 0.0)
    assert gate.check(0.0) == (DEFER_RATE_LIMITED, 1.0)
    assert gate.record(Outcome.PERMANENT, 0.0) is None
    assert gate.state is BreakerState.CLOSED
    assert gate.record(Outcome.TRANSIENT, 0.0) is BreakerState.OPEN
    assert gate.check(1.0) == (DEFER_CIRCUIT_OPEN, 9.0)
    assert gate.bucket.available(1.0) == 1, "an open circuit does not spend tokens"
    assert gate.check(10.0) == (None, 0.0)
    assert gate.record(Outcome.SUCCESS, 10.0) is BreakerState.CLOSED
    assert Gate().check(0.0) == (None, 0.0) and Gate().record(Outcome.TRANSIENT, 0.0) is None
