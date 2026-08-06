"""Tests for runtime detector health semantics."""

import pytest

from homecam_detector.inference_health import InferenceHealth


def test_missing_model_is_never_healthy() -> None:
    health = InferenceHealth(model_available=False, active=False, now=10.0)
    assert not health.healthy(10.0)
    health.set_active(True, 11.0)
    health.record_success(12.0)
    assert not health.healthy(12.0)


def test_active_detector_requires_recent_success_after_grace() -> None:
    health = InferenceHealth(model_available=True, active=True, now=10.0)
    assert health.healthy(19.9)
    assert not health.healthy(20.1)
    health.record_success(21.0)
    assert health.healthy(30.9)
    assert not health.healthy(31.1)


def test_three_consecutive_failures_mark_unhealthy_until_success() -> None:
    health = InferenceHealth(model_available=True, active=True, now=10.0)
    health.record_success(11.0)
    health.record_failure()
    health.record_failure()
    assert health.healthy(12.0)
    health.record_failure()
    assert not health.healthy(12.0)
    health.record_success(13.0)
    assert health.healthy(13.0)


def test_reactivation_starts_new_health_grace() -> None:
    health = InferenceHealth(model_available=True, active=True, now=0.0)
    health.record_success(1.0)
    assert not health.healthy(11.1)
    health.set_active(False, 12.0)
    assert health.healthy(100.0)
    health.set_active(True, 100.0)
    assert health.healthy(109.9)
    assert not health.healthy(110.1)


@pytest.mark.parametrize(
    ("stale_after_sec", "failure_limit"),
    [(0.0, 3), (10.0, 0)],
)
def test_rejects_invalid_limits(stale_after_sec: float, failure_limit: int) -> None:
    with pytest.raises(ValueError):
        InferenceHealth(
            model_available=True,
            active=True,
            now=0.0,
            stale_after_sec=stale_after_sec,
            failure_limit=failure_limit,
        )
