"""A fresh LiDAR/map match authorizes keepout recovery; old success flags do not."""

from copy import deepcopy
import math

import numpy as np
import pytest
from rclpy.time import Time

from malbut_relocalization.localization_confidence import LocalizationConfidence

TRUE_POSE = (0.8, 0.4, 0.3)


def covariance():
    return np.diag([.01, .01, 0, 0, 0, .01]).ravel().tolist()


def check(verifier, room, stamp=100.0, **kwargs):
    scan = room.scan(*TRUE_POSE)
    scan.header.stamp = Time(seconds=stamp).to_msg()
    args = dict(grid=room.grid(), scan=scan, covariance=covariance(),
                robot_pose=TRUE_POSE, laser_pose=(0, 0, 0), now=stamp + .02)
    args.update(kwargs)
    return verifier.check(**args)


def confirmed(room):
    verifier = LocalizationConfidence(room.map_file)
    assert not check(verifier, room, 100.0)
    assert not check(verifier, room, 100.1)
    assert check(verifier, room, 100.2)
    return verifier


def test_three_distinct_scans_are_required(room):
    verifier = LocalizationConfidence(room.map_file)
    for _ in range(10):
        assert not check(verifier, room, 100.0)
    assert not check(verifier, room, 100.1)
    assert check(verifier, room, 100.2)


@pytest.mark.parametrize('changes', [
    {'robot_pose': (-.9, -.5, 2.0)},  # Small covariance at the wrong pose is insufficient.
    {'robot_pose': (math.nan, .4, .3)},
    {'now': 102.0},
    {'now': 99.0},
    {'grid': None},
    {'covariance': np.diag([.1, .1, 0, 0, 0, .01]).ravel().tolist()},
    {'covariance': [math.nan] * 36},
    {'covariance': [-.01] * 36},
])
def test_bad_evidence_revokes_a_previous_confirmation(room, changes):
    verifier = confirmed(room)
    assert not check(verifier, room, 100.3, **changes)
    assert not check(verifier, room, 100.4)
    assert not check(verifier, room, 100.5)
    assert check(verifier, room, 100.6)


def test_covariance_must_be_symmetric_and_positive_semidefinite(room):
    verifier = confirmed(room)
    cov = covariance()
    cov[1] = cov[6] = .03
    assert not check(verifier, room, 100.3, covariance=cov)
    cov[6] = 0
    assert not check(verifier, room, 100.4, covariance=cov)


@pytest.mark.parametrize('mutation', [
    'cell', 'origin', 'resolution', 'frame', 'rotation', 'nan_origin', 'nan_rotation',
])
def test_same_size_but_different_map_never_reuses_confirmation(room, mutation):
    verifier = confirmed(room)
    grid = room.grid()
    if mutation == 'cell':
        grid.data[0] = 100 if grid.data[0] != 100 else 0
    elif mutation == 'origin':
        grid.info.origin.position.x += .05
    elif mutation == 'resolution':
        grid.info.resolution *= 2
    elif mutation == 'frame':
        grid.header.frame_id = 'odom'
    elif mutation == 'nan_origin':
        grid.info.origin.position.x = math.nan
    elif mutation == 'nan_rotation':
        grid.info.origin.orientation.x = math.nan
    else:
        grid.info.origin.orientation.z = .1
    assert not check(verifier, room, 100.3, grid=grid)


def test_sparse_or_one_wall_scan_is_not_sufficient(room):
    verifier = confirmed(room)
    scan = room.scan(*TRUE_POSE)
    scan.header.stamp = Time(seconds=100.3).to_msg()
    for keep in (2, 70):
        sparse = deepcopy(scan)
        sparse.ranges = [value if i < keep else math.inf for i, value in enumerate(scan.ranges)]
        assert not check(verifier, room, 100.3, scan=sparse)


def test_lost_scan_and_clock_rollback_require_new_confirmation(room):
    verifier = confirmed(room)
    assert not check(verifier, room, 102.0)
    assert not check(verifier, room, 99.0)
    assert not check(verifier, room, 99.1)
    assert check(verifier, room, 99.2)


def test_missing_or_replaced_selected_map_is_not_trusted(room, tmp_path):
    assert not check(LocalizationConfidence(tmp_path / 'missing.yaml'), room)
    assert not check(LocalizationConfidence(), room)
    room.map_file.write_text('mode: raw\n')
    assert not check(LocalizationConfidence(room.map_file), room)
