"""Tests for two-stage person track association."""

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
