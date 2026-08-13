"""Unit tests for bounded short-horizon target estimation."""

import pytest

from malbut_tracking.geometry import Point2D
from malbut_tracking.motion_estimator import TargetMotionEstimator


def test_prediction_uses_filtered_sensor_motion_and_bounded_horizon():
    """Prediction must never extrapolate beyond the configured short window."""
    estimator = TargetMotionEstimator(1.0, 1.0, 2.0)
    estimator.update(Point2D(0.0, 0.0), 10.0)
    estimator.update(Point2D(1.0, 0.0), 11.0)
    predicted = estimator.predict(13.0, 0.5)
    assert predicted == Point2D(1.5, 0.0)


def test_implausible_depth_jump_is_speed_limited():
    """A Depth outlier may not become an unbounded target velocity."""
    estimator = TargetMotionEstimator(1.0, 1.0, 2.0)
    estimator.update(Point2D(0.0, 0.0), 1.0)
    estimate = estimator.update(Point2D(10.0, 0.0), 2.0)
    assert estimate.velocity.x == pytest.approx(2.0)


def test_out_of_order_observation_does_not_rewind_estimate():
    """Late sensor messages must not replace the newest track position."""
    estimator = TargetMotionEstimator(1.0, 1.0, 2.0)
    newest = estimator.update(Point2D(1.0, 2.0), 5.0)
    returned = estimator.update(Point2D(9.0, 9.0), 4.0)
    assert returned is newest
    assert estimator.estimate is newest
