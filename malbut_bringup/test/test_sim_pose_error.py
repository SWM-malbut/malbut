"""Check the independent simulator pose comparison, including yaw wrapping."""

import math

from keepout_navigation_sim import pose_error_statistics
import pytest


def test_compare_at_measurement_time_and_across_yaw_wrap():
    """Sampling midway between odom messages must not invent motion error."""
    truth = [[0., 0., 0., math.radians(179)], [2., 2., 0., math.radians(-179)]]
    estimated = [[1., 1.06, .08, -math.pi], [3., 10., 10., 0.]]
    error = pose_error_statistics(truth, estimated)
    assert error['samples'] == 1
    assert error['excluded_samples'] == 1
    assert error['max_position_m'] == pytest.approx(.1)
    assert error['max_heading_rad'] == pytest.approx(0., abs=1e-12)


def test_do_not_claim_accuracy_without_overlapping_pose_timestamps():
    """An estimate outside the captured ground truth is not extrapolated."""
    with pytest.raises(ValueError, match='matching ground truth timestamp'):
        pose_error_statistics([[0., 0., 0., 0.], [1., 1., 0., 0.]], [[2., 2., 0., 0.]])
