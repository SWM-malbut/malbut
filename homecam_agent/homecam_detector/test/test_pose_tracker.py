"""Identity continuity tests; fabricated poses are not accuracy benchmarks."""

import pytest

from homecam_detector.pose import PersonPose, PoseKeypoint
from homecam_detector.pose_tracker import PersonPoseTracker


def pose(box=(0.1, 0.1, 0.3, 0.5), confidence=0.9):
    return PersonPose(confidence, box, (), 0)


def observed(result):
    return [track for track in result.tracks if track.pose is not None]


def test_distinct_people_keep_ids_when_score_order_changes():
    tracker = PersonPoseTracker()
    a, b = pose(), pose((0.6, 0.1, 0.8, 0.5), 0.6)
    first = observed(tracker.update([a, b], 0))
    second = observed(tracker.update([
        pose(b.box, 0.95), pose(a.box, 0.5)
    ], 0.2))
    assert {t.pose.box: t.track_id for t in first} == {
        t.pose.box: t.track_id for t in second
    }
    assert len({t.track_id for t in second}) == 2


def test_weak_person_gets_tentative_id_without_strong_detection():
    tracker = PersonPoseTracker()
    weak = pose(confidence=0.3)
    first = tracker.update([weak], 0).tracks[0]
    assert first.state == "tentative"
    assert first.confidence_level == "weak"
    tracker.update([weak], 0.2)
    third = tracker.update([weak], 0.4).tracks[0]
    assert third.track_id == first.track_id
    assert third.state == "tracked"
    assert third.confidence_level == "weak"  # repetition is not a confidence boost
    assert third.observation_count == 3
    assert len(third.history) == 3


def test_missing_frame_is_not_an_observed_pose_and_rejoins_within_gap():
    tracker = PersonPoseTracker(max_gap_sec=1)
    first = tracker.update([pose()], 1).tracks[0]
    missing = tracker.update([], 1.2).tracks[0]
    assert missing.track_id == first.track_id
    assert missing.state == "missing"
    assert missing.pose is None
    assert missing.observation_count == 1
    assert missing.consecutive_observations == 0
    assert missing.last_seen_age_sec == pytest.approx(0.2)
    third = tracker.update([pose(confidence=0.2)], 1.4).tracks[0]
    assert third.track_id == first.track_id
    assert third.confidence_level == "weak"
    assert third.consecutive_observations == 1
    assert len(third.history) == 2


def test_expired_and_reset_ids_are_never_reused():
    tracker = PersonPoseTracker(max_gap_sec=1)
    first = tracker.update([pose()], 1).tracks[0].track_id
    second = tracker.update([pose()], 2.1)
    assert second.expired_track_ids == (first,)
    second_id = second.tracks[0].track_id
    assert second_id != first
    assert tracker.reset() == (second_id,)
    third = tracker.update([pose()], 0).tracks[0].track_id
    assert third not in (first, second_id)


def test_ambiguous_merge_does_not_choose_one_person_silently():
    tracker = PersonPoseTracker()
    a = pose((0.1, 0.1, 0.3, 0.5))
    b = pose((0.3, 0.1, 0.5, 0.5))
    before = tracker.update([a, b], 0)
    merged = tracker.update([pose((0.2, 0.1, 0.4, 0.5))], 0.2)
    assert len(merged.unassigned) == 1
    assert merged.unassigned[0].reason == "association_ambiguous"
    assert all(t.state == "ambiguous" and t.pose is None for t in merged.tracks)
    assert {t.track_id for t in merged.tracks} == {t.track_id for t in before.tracks}
    assert all(t.observation_count == 1 for t in merged.tracks)


def test_near_duplicate_candidate_does_not_make_another_person():
    tracker = PersonPoseTracker()
    result = tracker.update([pose(), pose(confidence=0.2)], 0)
    assert len(result.tracks) == 1


def test_overlapping_but_distinct_people_remain_separate():
    tracker = PersonPoseTracker()
    sitting = pose((0.33, 0.2, 0.46, 0.5), 0.7)
    lying = pose((0.37, 0.37, 0.73, 0.52), 0.3)
    first = tracker.update([sitting, lying], 0)
    second = tracker.update([lying, sitting], 0.2)
    assert len(observed(first)) == len(observed(second)) == 2
    assert not second.unassigned
    assert {t.track_id for t in first.tracks} == {t.track_id for t in second.tracks}


def test_capacity_does_not_silently_discard_untracked_candidate():
    tracker = PersonPoseTracker(max_people=1)
    result = tracker.update([pose(), pose((0.6, 0.1, 0.8, 0.5), 0.3)], 0)
    assert len(result.tracks) == 1
    assert len(result.unassigned) == 1
    assert result.unassigned[0].reason == "track_capacity"


def test_history_is_bounded_and_backward_time_does_not_mutate_it():
    tracker = PersonPoseTracker()
    for i in range(40):
        result = tracker.update([pose()], i * 0.2)
    assert result.tracks[0].observation_count == 40
    assert len(result.tracks[0].history) == 30
    with pytest.raises(ValueError, match="time"):
        tracker.update([pose()], 0)
    final = tracker.update([pose()], 8)
    assert final.tracks[0].track_id == result.tracks[0].track_id
    assert final.tracks[0].observation_count == 41


def test_invalid_keypoint_does_not_partially_advance_tracking():
    tracker = PersonPoseTracker()
    first = tracker.update([pose()], 0).tracks[0]
    invalid = PersonPose(0.9, pose().box, (
        PoseKeypoint("nose", float("nan"), 0.2, 0.9),
    ), 1)
    with pytest.raises(ValueError, match="keypoint"):
        tracker.update([invalid], 0.2)
    result = tracker.update([pose()], 0.2).tracks[0]
    assert result.track_id == first.track_id
    assert result.observation_count == 2


def test_full_capacity_of_distinct_people_keeps_one_to_one_ids():
    tracker = PersonPoseTracker()
    people = [pose((col / 8, row / 4, col / 8 + 0.08, row / 4 + 0.15))
              for row in range(4) for col in range(8)]
    first = tracker.update(people, 0)
    second = tracker.update(list(reversed(people)), 0.2)
    assert len(observed(second)) == 32
    assert not second.unassigned
    assert {t.pose.box: t.track_id for t in first.tracks} == {
        t.pose.box: t.track_id for t in second.tracks
    }


@pytest.mark.parametrize("kwargs", [
    {"candidate_threshold": 0}, {"candidate_threshold": 0.5},
    {"strong_threshold": float("nan")}, {"max_gap_sec": 0},
    {"max_gap_sec": float("inf")}, {"min_observations": 1},
    {"max_people": 0}, {"max_people": 129},
])
def test_invalid_tracker_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        PersonPoseTracker(**kwargs)
