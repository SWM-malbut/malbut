"""Weak detector boxes (clothes, bedding) stay out of the clip overlay; display only."""

import json

import pytest

from malbut_agent_server.application.fall_people_recorder import FallPeopleRecorder
from test_fall_detector_input import make_input
from test_fall_subject_evidence import checked_message

TARGET = (0.1, 0.2, 0.3, 0.8)
COAT = (0.6, 0.2, 0.7, 0.7)
BED = (0.7, 0.3, 0.9, 0.6)


def recorded(people, target='pose:0:a'):
    rec = FallPeopleRecorder(boot_id='boot-1')
    for n in range(6):
        rec.observe(100 + n * 0.2, people)
    rec.segment('incident-1', target, 0, 100.0, 101.0)
    (result,) = rec.due(103.0)
    return {track.key: track for track in result.tracks}, rec


def test_weak_other_boxes_are_left_out_but_the_incidents_own_person_is_kept():
    tracks, rec = recorded((('pose:0:a', TARGET, False), ('pose:0:coat', COAT, False),
                            ('pose:0:old', BED, None), ('pose:0:b', COAT, True)))
    key = rec._key
    assert set(tracks) == {key('pose:0:a'), key('pose:0:old'), key('pose:0:b')}
    assert tracks[key('pose:0:a')].target


def test_two_value_people_from_older_callers_are_still_drawn():
    tracks, rec = recorded((('pose:0:a', TARGET), ('pose:0:b', COAT)))
    assert len(tracks) == 2


def subject_frames(**row):
    adapter, monitor, clock, _ = make_input()
    seen = []
    original = monitor.ingest_subject_frame
    monitor.ingest_subject_frame = lambda frame: (seen.append(frame), original(frame))[1]
    data = json.loads(checked_message(1000, state='unknown', usable=False))
    data['tracks'][0].update(row)
    clock.value = 100
    adapter.candidates(json.dumps(data), source_now=1000.001, now=clock())
    return seen


@pytest.mark.parametrize('level,strong', [('strong', True), ('weak', False), (None, None)])
def test_detector_confidence_level_reaches_the_subject_pose(level, strong):
    (frame,) = subject_frames(confidenceLevel=level)
    assert frame.subjects[0].strong is strong


def test_unknown_confidence_level_is_rejected():
    with pytest.raises(ValueError):
        subject_frames(confidenceLevel='medium')


@pytest.mark.parametrize('motion,moving,stationary', [
    ('moving', True, False), ('stationary', False, True), ('unknown', False, False),
])
def test_robot_motion_is_kept_for_scene_place_comparison(motion, moving, stationary):
    adapter, monitor, clock, _ = make_input()
    seen = []
    original = monitor.ingest_subject_frame
    monitor.ingest_subject_frame = lambda frame: (seen.append(frame), original(frame))[1]
    data = json.loads(checked_message(1000, state='unknown', usable=False))
    data['robotMotion'] = motion
    clock.value = 100
    adapter.candidates(json.dumps(data), source_now=1000.001, now=clock())
    (frame,) = seen
    assert (frame.camera_moving, frame.camera_stationary) == (moving, stationary)
