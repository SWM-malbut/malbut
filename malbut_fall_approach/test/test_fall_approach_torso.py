"""The view stops once the person's torso is in view (the user, 2026-10-13)."""

import json
import math
from pathlib import Path

import pytest

from malbut_fall_approach.fall_approach import (
    TORSO_STABLE_S, Pose2D, TorsoWatch, spot_image_x, target_pose, torso_in_view, view_score,
)

NAMES = ('nose', 'left_eye', 'right_eye', 'left_ear', 'right_ear', 'left_shoulder',
         'right_shoulder', 'left_elbow', 'right_elbow', 'left_wrist', 'right_wrist', 'left_hip',
         'right_hip', 'left_knee', 'right_knee', 'left_ankle', 'right_ankle')


def pose(box=(0.3, 0.2, 0.7, 0.9), confidence=0.8, *, seen=NAMES, y=0.5):
    """A detector pose with ``seen`` keypoints visible at height ``y``."""
    return {'boxConfidence': confidence,
            'box': dict(zip(('left', 'top', 'right', 'bottom'), box)),
            'keypoints': [{'name': n, 'x': 0.5, 'y': y, 'confidence': 0.9 if n in seen else 0.05}
                          for n in NAMES]}


def persons(*poses):
    return [{'pose': p} for p in poses]


FACING = Pose2D(0.0, 0.0, 0.0)  # Looking along +x.


def test_the_spot_falls_across_the_image_by_its_direction():
    assert spot_image_x(FACING, (2.0, 0.0)) == pytest.approx(0.5)
    left = spot_image_x(FACING, (2.0, 0.5))
    assert left < 0.5  # To the robot's left is the image's left.
    assert spot_image_x(FACING, (2.0, -0.5)) == pytest.approx(1.0 - left)
    assert spot_image_x(FACING, (0.0, 2.0)) is None  # Beside the robot: out of view.
    assert spot_image_x(FACING, (-2.0, 0.0)) is None


def test_only_the_person_in_the_spots_direction_is_watched():
    here, other = pose((0.35, 0.2, 0.65, 0.9), 0.6), pose((0.0, 0.1, 0.2, 0.9), 0.95)
    assert target_pose(persons(other, here), FACING, (2.0, 0.0)) is here
    assert target_pose(persons(other), FACING, (2.0, 0.0)) is None  # Someone else in the room.
    weak = pose((0.35, 0.2, 0.65, 0.9), 0.3)
    assert target_pose(persons(weak), FACING, (2.0, 0.0)) is None
    # A box just short of the direction still counts: the measured spot is not exact.
    near = pose((0.65, 0.2, 0.95, 0.9), 0.7)
    assert target_pose(persons(near), FACING, (2.0, 0.0)) is near
    assert target_pose(persons(here), FACING, (0.0, 2.0)) is None  # The spot is out of view.
    assert target_pose('nonsense', FACING, (2.0, 0.0)) is None
    assert target_pose([{'pose': {'box': {}}}, None], FACING, (2.0, 0.0)) is None


def test_the_torso_is_a_shoulder_with_a_hip_or_knee():
    assert torso_in_view(pose(seen=('left_shoulder', 'left_hip')))
    # Seated, the thighs may hide the hips: a knee will do.
    assert torso_in_view(pose(seen=('right_shoulder', 'right_knee')))
    assert not torso_in_view(pose(seen=('left_hip', 'left_knee', 'left_ankle')))  # Legs only.
    assert not torso_in_view(pose(seen=('left_shoulder', 'left_elbow')))
    # A shoulder YOLO places on the top edge of a cut-off body does not count.
    assert not torso_in_view(pose(seen=('left_shoulder', 'left_hip'), y=0.01))
    # The head is not needed.
    assert torso_in_view(pose(seen=('left_shoulder', 'left_hip', 'left_knee')))


def test_the_torso_has_to_stay_in_view_half_a_second():
    watch = TorsoWatch()
    torso, legs = pose(), pose(seen=('left_knee', 'left_ankle'))
    watch.observe(10.0, FACING, torso)
    assert not watch.seen(10.0)
    watch.observe(10.2, FACING, torso)
    watch.observe(10.4, FACING, legs)  # Lost again: start over.
    watch.observe(10.6, FACING, torso)
    watch.observe(10.6 + TORSO_STABLE_S, FACING, torso)
    assert watch.seen(11.2)
    assert not watch.seen(12.5), 'no frames for over a second'
    watch.observe(11.0, FACING, None)  # A late, older frame changes nothing.
    assert watch.seen(11.2)
    watch.observe(11.3, FACING, None)  # The person left the spot's direction.
    assert not watch.seen(11.3)


def test_where_the_body_was_seen_best_is_kept():
    watch = TorsoWatch()
    near, far = Pose2D(1.0, 0.0, math.pi), Pose2D(2.2, 0.0, math.pi)
    watch.observe(1.0, near, pose(seen=('left_knee', 'left_ankle', 'right_ankle')))
    watch.observe(2.0, far, pose(seen=('left_hip', 'left_knee', 'left_ankle', 'right_ankle')))
    watch.observe(3.0, near, pose(seen=('left_ankle',)))
    assert watch.best == (4, far) and view_score(pose(seen=())) == 0
    watch.observe(4.0, near, None)
    assert watch.best == (4, far)
    watch.reset()
    assert watch.best is None and not watch.seen(4.0)


def test_the_1650_clip_would_have_stopped_at_about_1_23_m():
    """2026-10-13 16:51: backing from 1 m at 0.1 m/s, the torso came into view."""
    data = json.loads((Path(__file__).parent / 'data_1650_backup.json').read_text())
    watch, stopped = TorsoWatch(), None
    for frame in data['frames']:
        # The robot faced the spot straight on; the person lay across the image.
        watch.observe(frame['t'], FACING, target_pose(frame['persons'], FACING, (1.0, 0.0)))
        if watch.seen(frame['t']):
            stopped = frame['t']
            break
    assert stopped is not None
    distance = 1.0 + 0.1 * (stopped - 7.86)
    # Seen from 10.2 s (1.23 m); half a second of frames confirms it.
    assert 1.2 <= distance <= 1.35, distance
