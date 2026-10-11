"""Open person incidents never outnumber the people in view."""

import pytest

from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, FallCandidate, SubjectCheckState, SubjectFrame, SubjectPose,
)
from test_cloud_fall_monitor import enable, make

# 2026-10-10 18:44, one person lying on the floor, boxes from the incident clips.
FIRST = (.018, .034, .808, .650)    # 사람 1, no incident
LYING = (.138, .083, .963, .626)    # 사람 3, first incident; its last box
SPLIT = (.055, .135, .953, .630)    # 사람 4, together with 사람 3
RIGHT = (.576, .062, 1.0, .567)     # 사람 5, 2.2 s after 사람 3 vanished
SEATED = (.097, .004, .289, .291)   # 사람 2, another person at the desk


def replay(monitor, clock, tracks, candidates, *, end, moving=()):
    """Feed 4 Hz Pose frames; tracks: key -> (box, from, until)."""
    opened, t = {}, 0.0
    while t <= end:
        clock.value = 100 + t
        for key, at, kind in candidates:
            if abs(at - t) < 1e-6:
                opened[key] = monitor.candidate(FallCandidate(
                    'c-' + key, key, 'yolo_pose', kind, clock.value))
        monitor.ingest_subject_frame(SubjectFrame(clock.value, tuple(
            SubjectPose(key, box, SubjectCheckState.SUSPECTED, True, True)
            for key, (box, start, until) in tracks.items() if start <= t <= until), .5,
            camera_moving=any(a <= t <= b for a, b in moving)))
        t = round(t + .25, 2)
    return opened


def setup():
    monitor, clock, _ = make()
    enable(monitor)
    return monitor, clock


def test_one_lying_person_split_into_tracks_is_one_incident():
    monitor, clock = setup()
    down = CandidateKind.ALREADY_DOWN
    opened = replay(monitor, clock, {
        'pose:0:1': (FIRST, 0.25, 8.0), 'pose:0:3': (LYING, 9.25, 17.25),
        'pose:0:4': (SPLIT, 9.75, 29.75), 'pose:0:5': (RIGHT, 19.5, 29.75),
        'pose:0:2': (SEATED, 0.25, 29.75),
    }, [('pose:0:3', 10.25, down), ('pose:0:4', 12.0, down), ('pose:0:5', 20.5, down),
        ('pose:0:2', 25.0, down)], end=29.75)
    assert opened['pose:0:3'] is not None
    assert opened['pose:0:4'] == opened['pose:0:5'] == opened['pose:0:3']
    assert opened['pose:0:2'] not in (None, opened['pose:0:3']), 'another person'
    assert [e.kind for e in monitor.drain_events()].count('incident_opened') == 2


def test_a_fall_seen_on_an_overlapping_track_opens_its_own_incident():
    monitor, clock = setup()
    opened = replay(monitor, clock, {
        'pose:0:3': (LYING, 0.0, 5.0), 'pose:0:4': (SPLIT, 0.0, 5.0),
    }, [('pose:0:3', 1.0, CandidateKind.ALREADY_DOWN),
        ('pose:0:4', 2.0, CandidateKind.MOTION_SEEN)], end=5.0)
    assert None not in opened.values()
    assert opened['pose:0:3'] != opened['pose:0:4']


class Places:
    def __init__(self, points):
        self.points = points

    def locate_near(self, observed_at, box, *, tolerance_s=0.25):
        return self.points.get(box)

    def clear(self):
        pass


def test_overlapping_boxes_more_than_1m_apart_on_the_map_are_two_people():
    monitor, clock = setup()
    monitor._place = Places({LYING: (2.0, 1.0), SPLIT: (3.5, 1.0)})
    opened = replay(monitor, clock, {
        'pose:0:3': (LYING, 0.0, 5.0), 'pose:0:4': (SPLIT, 0.0, 5.0),
    }, [('pose:0:3', 1.0, CandidateKind.ALREADY_DOWN),
        ('pose:0:4', 2.0, CandidateKind.ALREADY_DOWN)], end=5.0)
    assert opened['pose:0:3'] != opened['pose:0:4']


@pytest.mark.parametrize('appears,moving', [(14.0, ()), (11.0, ((9.5, 10.5),))],
                         ids=['over 5 s later', 'camera moved'])
def test_a_track_where_another_vanished_is_new_after_a_gap_or_movement(appears, moving):
    monitor, clock = setup()
    opened = replay(monitor, clock, {
        'pose:0:3': (LYING, 0.0, 8.0), 'pose:0:5': (RIGHT, appears, appears + 3),
    }, [('pose:0:3', 1.0, CandidateKind.ALREADY_DOWN),
        ('pose:0:5', appears + 1, CandidateKind.ALREADY_DOWN)], end=appears + 3,
        moving=moving)
    assert opened['pose:0:3'] != opened['pose:0:5']


def test_later_candidates_of_the_same_person_stay_on_the_incident():
    monitor, clock = setup()
    down = CandidateKind.ALREADY_DOWN
    opened = replay(monitor, clock, {
        'pose:0:3': (LYING, 0.0, 3.0), 'pose:0:4': (SPLIT, 0.0, 9.0),
    }, [('pose:0:3', 1.0, down), ('pose:0:4', 2.0, down)], end=9.0)
    # 사람 3 is gone; 사람 4 alone no longer overlaps anything open.
    again = monitor.candidate(FallCandidate('c-4b', 'pose:0:4', 'yolo_pose', down, clock.value))
    assert again == opened['pose:0:3'] == opened['pose:0:4']
