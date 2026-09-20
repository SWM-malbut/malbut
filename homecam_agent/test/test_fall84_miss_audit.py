"""Diagnostic stage counts must not invent target identity or observation time."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audit_fall84_misses import frame_counts, low_runs, pattern, window  # noqa: E402


def observation(tid='a', usable=True, low=False):
    return dict(track_id=tid, features=dict(usable=usable, horizontal=low, compact_body=False))


def row(frame, *observations):
    return dict(frame_index=frame, timestamp_s=frame / 10, observations=list(observations),
                fall_analysis=dict(candidates=[]))


def test_unknown_time_is_explicit_whole_clip_not_invented_fall_onset():
    assert window({}) == dict(start_frame=0, basis='whole_clip_no_reviewed_boundary',
                              source_interval=None)


def test_down_boundary_uses_upper_edge_and_takes_precedence_over_onset():
    assert window(dict(first_down_frames=[12, 16], onset_frames=[6, 8])) == dict(
        start_frame=16, basis='first_down_frames', source_interval=[12, 16])
    assert window(dict(onset_frames=[6, 8]))['start_frame'] == 8


@pytest.mark.parametrize('interval', [[4, 2], [False, 2], [-1, 2], [1], [1, 2.5]])
def test_bad_time_boundary_rejected(interval):
    with pytest.raises(ValueError):
        window(dict(onset_frames=interval))


def test_counts_are_frames_not_boxes_or_people():
    counts = frame_counts([row(0, observation(), observation('b', low=True)), row(2)])
    assert counts['frames'] == 2 and counts['any_pose'] == counts['usable_pose'] == 1
    assert counts['low_pose'] == 1 and counts['requests'] == 0


@pytest.mark.parametrize('rows,expected', [
    ([], 'no_samples_in_window'),
    ([row(0)], 'no_pose_in_window'),
    ([row(0, observation(usable=False))], 'no_usable_pose_in_window'),
    ([row(0, observation())], 'usable_but_no_low_pose'),
    ([row(0, observation(low=True))], 'low_pose_seen_no_request'),
])
def test_patterns_only_describe_observed_stage(rows, expected):
    assert pattern(frame_counts(rows)) == expected


def test_missing_time_does_not_count_as_observed_low_posture():
    runs = low_runs([row(0, observation(low=True)), row(2, observation(low=True)),
                    row(4), row(6, observation(low=True))], .5)
    assert len(runs) == 2
    assert runs[0]['frames'] == [0, 2] and runs[0]['span_s'] == .2
    assert runs[0]['ended_by'] == 'missing_or_unassigned'
    assert runs[1]['span_s'] == 0


@pytest.mark.parametrize('middle,reason', [
    (observation(usable=False), 'insufficient_pose'),
    (observation(), 'other_posture'),
    (observation(tid=None, low=True), 'missing_or_unassigned'),
])
def test_low_runs_break_on_poor_joints_other_posture_and_unassigned(middle, reason):
    runs = low_runs([row(0, observation(low=True)), row(2, middle),
                    row(4, observation(low=True))], .5)
    assert len(runs) == 2 and runs[0]['ended_by'] == reason


def test_different_ids_and_large_gaps_do_not_join():
    runs = low_runs([row(0, observation(low=True)), row(2, observation('b', low=True)),
                    row(8, observation('b', low=True))], .5)
    assert len(runs) == 3 and all(r['span_s'] == 0 for r in runs)
    assert runs[1]['ended_by'] == 'sample_gap'
