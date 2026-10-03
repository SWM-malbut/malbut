"""Clip ranges are metadata; no recording, upload or playback happens here."""

import pytest

from malbut_agent_server.application.fall_clip_planner import FallClipPlanner


def spans(segments):
    return [(s.segment_index, s.start, s.end, s.revision) for s in segments]


def test_pose_motion_window_gets_ten_seconds_before_and_twenty_after():
    planner = FallClipPlanner()
    changed = planner.observe('incident-1', 100.0, 102.0, anchor_kind='pose_motion')
    assert spans(changed) == [(0, 90.0, 122.0, 1)]
    assert changed[0].anchor_kinds == ('pose_motion',) and not changed[0].found_down


def test_touching_evidence_extends_the_same_segment_and_bumps_revision():
    planner = FallClipPlanner()
    planner.observe('incident-1', 100.0, 102.0, anchor_kind='pose_motion')
    changed = planner.observe('incident-1', 105.0, 108.0, anchor_kind='cloud_window')
    assert spans(changed) == [(0, 90.0, 128.0, 2)]
    assert changed[0].anchor_kinds == ('pose_motion', 'cloud_window')


def test_repeated_evidence_inside_the_range_changes_nothing():
    planner = FallClipPlanner()
    planner.observe('incident-1', 100.0, 102.0, anchor_kind='pose_motion')
    assert planner.observe('incident-1', 101.0, 101.5, anchor_kind='pose_motion') == ()


def test_gap_after_the_range_starts_a_new_segment():
    planner = FallClipPlanner()
    planner.observe('incident-1', 100.0, 102.0, anchor_kind='pose_motion')
    changed = planner.observe('incident-1', 200.0, 201.0, anchor_kind='pose_motion')
    assert spans(changed) == [(1, 190.0, 221.0, 1)]
    assert spans(planner.segments('incident-1')) == [(0, 90.0, 122.0, 1), (1, 190.0, 221.0, 1)]


def test_long_incident_is_split_at_120_seconds():
    planner = FallClipPlanner()
    planner.observe('incident-1', 100.0, 100.0, anchor_kind='pose_motion')
    changed = planner.observe('incident-1', 110.0, 230.0, anchor_kind='cloud_window')
    assert spans(changed) == [(0, 90.0, 210.0, 2), (1, 210.0, 250.0, 1)]
    assert all(s.end - s.start <= 120.0 for s in planner.segments('incident-1'))


def test_found_down_is_recorded_and_kept():
    planner = FallClipPlanner()
    first = planner.observe('incident-1', 50.0, 50.0, anchor_kind='pose_found_down')
    assert first[0].found_down
    later = planner.observe('incident-1', 60.0, 61.0, anchor_kind='pose_motion')
    assert later[0].found_down


def test_earlier_cloud_window_widens_only_the_first_segment():
    planner = FallClipPlanner()
    planner.observe('incident-1', 100.0, 102.0, anchor_kind='pose_motion')
    changed = planner.observe('incident-1', 95.0, 101.0, anchor_kind='cloud_window')
    assert spans(changed) == [(0, 85.0, 122.0, 2)]


def test_incidents_are_independent_and_forgettable():
    planner = FallClipPlanner()
    planner.observe('incident-1', 100.0, 102.0, anchor_kind='pose_motion')
    planner.observe('incident-2', 100.0, 102.0, anchor_kind='pose_motion')
    planner.forget('incident-1')
    assert planner.segments('incident-1') == ()
    assert len(planner.segments('incident-2')) == 1


def test_segment_count_is_bounded():
    planner = FallClipPlanner(max_segments=2)
    for t in (100.0, 300.0, 500.0):
        planner.observe('incident-1', t, t, anchor_kind='pose_motion')
    assert len(planner.segments('incident-1')) == 2


@pytest.mark.parametrize('args', [
    (102.0, 100.0, 'pose_motion'), (100.0, 101.0, 'guess'), (float('nan'), 1.0, 'pose_motion'),
])
def test_invalid_observations_are_rejected(args):
    start, end, kind = args
    with pytest.raises(ValueError):
        FallClipPlanner().observe('incident-1', start, end, anchor_kind=kind)


def test_invalid_configuration_is_rejected():
    with pytest.raises(ValueError):
        FallClipPlanner(pre_s=-1)
    with pytest.raises(ValueError):
        FallClipPlanner(max_segment_s=30)


def test_tiny_split_remainder_is_dropped_not_sent_empty():
    planner = FallClipPlanner()
    planner.observe('incident-1', 100.0, 170.0, anchor_kind='pose_motion')
    changed = planner.observe('incident-1', 100.0, 190.0004, anchor_kind='cloud_window')
    assert spans(changed) == [(0, 90.0, 210.0, 2)]
    assert len(planner.segments('incident-1')) == 1 and planner.dropped_s > 0


def test_evidence_before_the_last_segment_does_not_touch_it():
    planner = FallClipPlanner()
    planner.observe('incident-1', 100.0, 100.0, anchor_kind='pose_motion')
    planner.observe('incident-1', 110.0, 230.0, anchor_kind='cloud_window')
    assert planner.observe('incident-1', 120.0, 125.0, anchor_kind='pose_found_down') == ()
    assert [s.revision for s in planner.segments('incident-1')] == [2, 1]
