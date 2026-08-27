"""Tests for two-stage person track association."""

import numpy as np

from malbut_perception.detector.base import BoundingBox, ImageDetection
from malbut_perception.tracker.bytetrack_tracker import ByteTrackTracker


def _detection(left: float, score: float) -> ImageDetection:
    return ImageDetection(
        BoundingBox(left, 10.0, left + 40.0, 110.0),
        score,
    )


def test_tracker_preserves_id_through_low_confidence_detection():
    tracker = ByteTrackTracker(
        high_threshold=0.5,
        low_threshold=0.1,
        match_iou_threshold=0.3,
        min_confirmed_hits=1,
    )
    first = tracker.update([_detection(10.0, 0.9)])
    second = tracker.update([_detection(12.0, 0.3)])
    assert first[0].track_id == 1
    assert second[0].track_id == 1


def test_low_confidence_detection_does_not_create_a_track():
    tracker = ByteTrackTracker(
        high_threshold=0.5,
        low_threshold=0.1,
        min_confirmed_hits=1,
    )
    assert tracker.update([_detection(10.0, 0.3)]) == []


def test_tentative_track_requires_multiple_hits():
    tracker = ByteTrackTracker(min_confirmed_hits=2)
    assert tracker.update([_detection(10.0, 0.9)]) == []
    confirmed = tracker.update([_detection(11.0, 0.9)])
    assert len(confirmed) == 1
    assert confirmed[0].track_id == 1
    assert confirmed[0].hits == 2


def test_expired_track_is_not_reused():
    tracker = ByteTrackTracker(
        max_missed_frames=1,
        min_confirmed_hits=1,
    )
    assert tracker.update([_detection(10.0, 0.9)])[0].track_id == 1
    tracker.update([])
    tracker.update([])
    assert tracker.update([_detection(10.0, 0.9)])[0].track_id == 2


def test_reidentification_restores_id_after_track_expires():
    tracker = ByteTrackTracker(
        max_missed_frames=1,
        min_confirmed_hits=1,
        reid_threshold=0.2,
    )
    feature = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    first = tracker.update([_detection(10.0, 0.9)], [feature])
    tracker.update([])
    tracker.update([])
    restored = tracker.update([_detection(300.0, 0.9)], [feature])
    assert first[0].track_id == 1
    assert restored[0].track_id == 1


def test_zero_inactive_limit_preserves_identity_for_process_lifetime():
    """A zero inactive limit must retain a retired identity indefinitely."""
    tracker = ByteTrackTracker(
        max_missed_frames=0,
        min_confirmed_hits=1,
        reid_threshold=0.2,
        reid_max_inactive_frames=0,
        feature_budget=30,
    )
    first_feature = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    tracker.update([_detection(10.0, 0.9)], [first_feature])
    tracker.update([])
    for _ in range(500):
        tracker.update([])

    restored = tracker.update(
        [_detection(300.0, 0.9)],
        [first_feature],
    )

    assert restored[0].track_id == 1


def test_reidentification_rejects_different_appearance():
    tracker = ByteTrackTracker(
        max_missed_frames=0,
        min_confirmed_hits=1,
        reid_threshold=0.2,
    )
    tracker.update(
        [_detection(10.0, 0.9)],
        [np.array([1.0, 0.0], dtype=np.float32)],
    )
    tracker.update([])
    different = tracker.update(
        [_detection(300.0, 0.9)],
        [np.array([0.0, 1.0], dtype=np.float32)],
    )
    assert different[0].track_id == 2


def test_appearance_association_handles_large_image_displacement():
    tracker = ByteTrackTracker(min_confirmed_hits=1)
    feature = np.array([0.5, 0.5], dtype=np.float32)
    first = tracker.update([_detection(10.0, 0.9)], [feature])
    moved = tracker.update([_detection(300.0, 0.9)], [feature])
    assert first[0].track_id == moved[0].track_id


def test_appearance_count_must_match_detections():
    tracker = ByteTrackTracker()
    try:
        tracker.update([_detection(10.0, 0.9)], [])
    except ValueError as error:
        assert 'count must match' in str(error)
    else:
        raise AssertionError('mismatched appearance features were accepted')


def test_tracker_requests_appearance_only_when_geometry_is_insufficient():
    tracker = ByteTrackTracker(min_confirmed_hits=1)
    first = _detection(10.0, 0.9)
    assert tracker.needs_appearance_features([first])

    feature = np.array([1.0, 0.0], dtype=np.float32)
    tracker.update([first], [feature])
    assert not tracker.needs_appearance_features([_detection(12.0, 0.9)])
    assert tracker.needs_appearance_features([_detection(300.0, 0.9)])
