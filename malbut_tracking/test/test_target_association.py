"""Unit tests for automatic person acquisition and continuous association."""

from malbut_tracking.geometry import Point2D, distance
from malbut_tracking.target_association import (
    TargetCandidate,
    fuse_camera_bearing_with_lidar_range,
    select_target_candidate,
)


def _candidate(index, x, confidence, track_id):
    return TargetCandidate(
        source_index=index,
        position=Point2D(x, 0.0),
        confidence=confidence,
        observed_track_id=track_id,
    )


def test_first_observation_uses_highest_confidence_person():
    """Automatic acquisition should choose the clearest visible person."""
    selected = select_target_candidate(
        [_candidate(0, 1.0, 0.55, '4'), _candidate(1, 2.0, 0.85, '9')],
        predicted_position=None,
    )
    assert selected is not None
    assert selected.observed_track_id == '9'


def test_id_change_continues_nearest_predicted_person():
    """A nearby observation remains selected even when its ID changes."""
    selected = select_target_candidate(
        [_candidate(0, 2.2, 0.70, '12'), _candidate(1, 4.0, 0.95, '3')],
        predicted_position=Point2D(2.0, 0.0),
    )
    assert selected is not None
    assert selected.observed_track_id == '12'


def test_visible_camera_person_is_not_rejected_by_stale_prediction():
    """A visible person remains authoritative despite a large map jump."""
    selected = select_target_candidate(
        [_candidate(0, 5.0, 0.99, '7')],
        predicted_position=Point2D(1.0, 0.0),
    )
    assert selected is not None
    assert selected.observed_track_id == '7'


def test_stale_id_cannot_override_nearest_current_camera_person():
    """Detector IDs are diagnostic; map continuity owns visible selection."""
    selected = select_target_candidate(
        [
            _candidate(0, 5.0, 0.99, 'person-7'),
            _candidate(1, 1.1, 0.80, 'person-8'),
        ],
        predicted_position=Point2D(1.0, 0.0),
    )
    assert selected is not None
    assert selected.observed_track_id == 'person-8'


def test_id_change_still_selects_the_nearest_visible_camera_person():
    """A stale Re-ID value cannot suppress a current camera observation."""
    selected = select_target_candidate(
        [_candidate(0, 5.0, 0.99, 'person-8')],
        predicted_position=Point2D(1.0, 0.0),
    )
    assert selected is not None
    assert selected.observed_track_id == 'person-8'


def test_camera_lidar_fusion_uses_camera_bearing_and_lidar_range():
    """Identity bearing and metric range should form one target position."""
    fused = fuse_camera_bearing_with_lidar_range(
        Point2D(0.0, 0.0),
        Point2D(2.0, 0.2),
        Point2D(2.5, 0.35),
        maximum_lateral_error_m=0.20,
        maximum_range_error_m=1.0,
    )
    assert fused is not None
    assert abs(distance(fused, Point2D(0.0, 0.0)) - 2.5249) < 1e-3
    assert abs(fused.y / fused.x - 0.1) < 1e-6


def test_camera_lidar_fusion_rejects_a_neighboring_obstacle():
    """A range-compatible object off the person's camera ray is not fused."""
    assert fuse_camera_bearing_with_lidar_range(
        Point2D(0.0, 0.0),
        Point2D(2.0, 0.0),
        Point2D(2.0, 0.8),
        maximum_lateral_error_m=0.30,
        maximum_range_error_m=1.0,
    ) is None
