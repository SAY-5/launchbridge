import random

import pytest

from launchbridge.retry import Outcome, RetryPolicy, classify


def test_base_backoff_grows_exponentially_and_caps():
    policy = RetryPolicy(base_delay_seconds=0.5, multiplier=2, max_delay_seconds=3, jitter=0)
    assert [policy.base_backoff(n) for n in range(1, 6)] == [0.5, 1.0, 2.0, 3.0, 3.0]


def test_jitter_stays_within_bounds_and_below_cap():
    policy = RetryPolicy(base_delay_seconds=1, multiplier=2, max_delay_seconds=6, jitter=0.25)
    for seed in range(200):
        rng = random.Random(seed)
        for attempt in range(1, 8):
            delay = policy.backoff(attempt, rng)
            base = policy.base_backoff(attempt)
            assert base * 0.75 <= delay <= base * 1.25
            assert delay <= policy.max_delay_seconds


def test_zero_jitter_is_deterministic():
    policy = RetryPolicy(base_delay_seconds=2, jitter=0)
    assert policy.backoff(3) == policy.base_backoff(3) == 8


def test_should_retry_is_bounded_by_max_attempts():
    policy = RetryPolicy(max_attempts=3)
    assert policy.should_retry(1, Outcome.TRANSIENT)
    assert policy.should_retry(2, Outcome.TRANSIENT)
    assert not policy.should_retry(3, Outcome.TRANSIENT)
    assert not policy.should_retry(1, Outcome.PERMANENT)
    assert not policy.should_retry(1, Outcome.SUCCESS)


def test_should_retry_honours_the_budget_recorded_on_the_delivery():
    """The worker passes the delivery's own budget, which outlives a configuration edit."""
    policy = RetryPolicy(max_attempts=3)
    assert policy.should_retry(3, Outcome.TRANSIENT, 5)
    assert not policy.should_retry(4, Outcome.TRANSIENT, 4)
    assert not policy.should_retry(1, Outcome.PERMANENT, 9)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, Outcome.SUCCESS),
        (204, Outcome.SUCCESS),
        (None, Outcome.TRANSIENT),
        (500, Outcome.TRANSIENT),
        (503, Outcome.TRANSIENT),
        (429, Outcome.TRANSIENT),
        (408, Outcome.TRANSIENT),
        (400, Outcome.PERMANENT),
        (404, Outcome.PERMANENT),
        (410, Outcome.PERMANENT),
        (301, Outcome.PERMANENT),
    ],
)
def test_classify(status, outcome):
    assert classify(status) is outcome


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay_seconds": -1},
        {"max_delay_seconds": 0.1, "base_delay_seconds": 1},
        {"multiplier": 0.5},
        {"jitter": 1.5},
        {"timeout_seconds": 0},
    ],
)
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_attempt_must_be_one_based():
    with pytest.raises(ValueError):
        RetryPolicy().base_backoff(0)
