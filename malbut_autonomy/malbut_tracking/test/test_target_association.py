"""Unit tests for automatic person acquisition and continuous association."""

from malbut_tracking.geometry import Point2D
from malbut_tracking.target_association import (
    CameraObservationGate,
    TargetCandidate,
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


def test_reidentified_person_is_restored_after_a_large_motion():
    """A matching OSNet-backed ID may reacquire beyond the spatial gate."""
    selected = select_target_candidate(
        [_candidate(0, 5.0, 0.80, 'person-7')],
        predicted_position=Point2D(1.0, 0.0),
        preferred_track_id='person-7',
    )
    assert selected is not None
    assert selected.observed_track_id == 'person-7'


def test_id_change_still_selects_the_nearest_visible_camera_person():
    """A stale Re-ID value cannot suppress a current camera observation."""
    selected = select_target_candidate(
        [_candidate(0, 5.0, 0.99, 'person-8')],
        predicted_position=Point2D(1.0, 0.0),
        preferred_track_id='person-7',
    )
    assert selected is not None
    assert selected.observed_track_id == 'person-8'


def test_camera_jump_requires_two_consistent_observations():
    """One distant false detection cannot replace a continuous target."""
    gate = CameraObservationGate(2, 0.50)

    assert not gate.accept(
        Point2D(5.0, 0.0),
        Point2D(1.0, 0.0),
        continuity_radius_m=1.0,
        lidar_supported=False,
    )
    assert gate.accept(
        Point2D(5.1, 0.0),
        Point2D(1.0, 0.0),
        continuity_radius_m=1.0,
        lidar_supported=False,
    )


def test_lidar_support_accepts_a_discontinuous_camera_reacquisition():
    """A separated person cluster lets RGB-D reacquire without extra delay."""
    gate = CameraObservationGate(2, 0.50)

    assert gate.accept(
        Point2D(3.0, 0.0),
        Point2D(1.0, 0.0),
        continuity_radius_m=1.0,
        lidar_supported=True,
    )
