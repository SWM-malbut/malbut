"""Tests for threshold-free tracking duration measurements."""

import pytest

from malbut_tracking.tracking_metrics import TrackingDurationMetrics


def test_one_lap_integrates_tracking_loss_and_reacquisition():
    """A lap reports measured durations without deciding pass or fail."""
    metrics = TrackingDurationMetrics()
    metrics.start(0.0)
    metrics.sample(3.0, tracking_active=False, recovery_active=True)
    metrics.sample(5.0, tracking_active=True, recovery_active=False)
    metrics.sample(9.0, tracking_active=True, recovery_active=False)

    report = metrics.report(9.0)

    assert report == {
        'elapsed_s': 9.0,
        'tracking_duration_s': 7.0,
        'tracking_ratio': pytest.approx(7.0 / 9.0),
        'longest_continuous_tracking_s': 4.0,
        'recovery_duration_s': 2.0,
        'reacquisition_count': 1,
    }
    assert 'passed' not in report
    assert 'failed' not in report


def test_sample_time_must_be_monotonic():
    """A backwards simulation clock must not corrupt duration totals."""
    metrics = TrackingDurationMetrics()
    metrics.start(10.0)

    with pytest.raises(ValueError, match='monotonic'):
        metrics.sample(9.0, tracking_active=True, recovery_active=False)
