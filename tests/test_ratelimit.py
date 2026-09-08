import pytest

from launchbridge.ratelimit import TokenBucket


def test_bucket_starts_full_and_refills_at_rate():
    bucket = TokenBucket(rate=2, burst=4)
    assert bucket.available(now=100.0) == 4
    assert all(bucket.try_acquire(100.0) for _ in range(4))
    assert not bucket.try_acquire(100.0)
    assert bucket.seconds_until(100.0) == 0.5
    assert bucket.seconds_until(100.0, n=3) == 1.5
    assert not bucket.try_acquire(100.4)
    assert bucket.try_acquire(100.5)
    assert bucket.available(103.0) == pytest.approx(4), "never above burst"


def test_bucket_ignores_clock_going_backwards():
    bucket = TokenBucket(rate=1, burst=1)
    assert bucket.try_acquire(50.0)
    assert bucket.available(40.0) == 0
    assert bucket.available(41.0) == pytest.approx(1)


@pytest.mark.parametrize("kwargs", [{"rate": 0, "burst": 1}, {"rate": 1, "burst": 0}])
def test_invalid_buckets_are_rejected(kwargs):
    with pytest.raises(ValueError):
        TokenBucket(**kwargs)
