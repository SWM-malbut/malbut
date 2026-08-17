"""Tests for timestamp-based perception frame sampling."""

from malbut_perception.target_localizer_node import TimestampRateLimiter


def test_six_hz_sampler_accepts_every_second_twelve_hz_frame():
    limiter = TimestampRateLimiter()
    camera_period_ns = 83_333_333
    accepted = [
        index
        for index in range(12)
        if limiter.should_process(index * camera_period_ns, 6.0)
    ]
    assert accepted == [0, 2, 4, 6, 8, 10]


def test_unlimited_sampler_accepts_every_frame():
    limiter = TimestampRateLimiter()
    assert all(
        limiter.should_process(index * 10_000_000, 0.0)
        for index in range(5)
    )


def test_sampler_recovers_after_simulation_clock_reset():
    limiter = TimestampRateLimiter()
    assert limiter.should_process(1_000_000_000, 6.0)
    assert limiter.should_process(10_000_000, 6.0)
