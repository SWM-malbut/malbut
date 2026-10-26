"""Score scans against a saved map without ROS communication."""

import math

from malbut_relocalization.scan_match import (
    alignment_is_unique, assess_scan, distance_field, match_ratio,
)
import numpy as np
import pytest


def _field(room):
    grid = room.grid()
    info = grid.info
    return distance_field(info.width, info.height, info.resolution,
                          (info.origin.position.x, info.origin.position.y, 0.0), grid.data)


def test_true_pose_matches_and_a_moved_robot_does_not(room):
    """A robot moved while off leaves its old pose's scan in free space."""
    field = _field(room)
    scan = room.scan(0.8, 0.4, 0.3)
    ratio, beams = match_ratio(field, scan, (0.0, 0.0, 0.0), (0.8, 0.4, 0.3),
                               hit_distance_m=0.15, max_range_m=8.0)
    assert beams == 360 and ratio > 0.95
    for wrong in ((-0.9, -0.5, 0.3), (0.8, 0.4, 1.8), (0.1, 0.1, -2.0)):
        ratio, _ = match_ratio(field, scan, (0.0, 0.0, 0.0), wrong,
                               hit_distance_m=0.15, max_range_m=8.0)
        assert ratio < 0.5, wrong


def test_laser_offset_and_map_edges_are_respected(room):
    """The laser mount is applied, and endpoints beyond the map are misses."""
    field = _field(room)
    scan = room.scan(0.1, -0.2, math.pi / 2)
    # The same scan seen from a laser mounted 0.2 m ahead of a base 0.2 m behind.
    ratio, _ = match_ratio(field, scan, (0.2, 0.0, 0.0), (0.1, -0.4, math.pi / 2),
                           hit_distance_m=0.15, max_range_m=8.0)
    assert ratio > 0.95
    outside = room.scan(0.1, -0.2, 0.0)
    ratio, _ = match_ratio(field, outside, (0.0, 0.0, 0.0), (30.0, 30.0, 0.0),
                           hit_distance_m=0.15, max_range_m=8.0)
    assert ratio == 0.0


def test_scans_without_returns_score_zero(room):
    """No usable beam is not evidence for any pose."""
    scan = room.scan(0.0, 0.0, 0.0)
    scan.ranges = [math.inf] * len(scan.ranges)
    assert match_ratio(_field(room), scan, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0),
                       hit_distance_m=0.15, max_range_m=8.0) == (0.0, 0)


def test_the_tighter_radius_rejects_nearby_but_misaligned_points(room):
    field = _field(room)
    scan = room.scan(0.8, 0.4, 0.3)
    pose = (0.8 + 0.13, 0.4, 0.3)
    loose, _ = match_ratio(field, scan, (0.0, 0.0, 0.0), pose,
                           hit_distance_m=0.15, max_range_m=8.0)
    tight, _ = match_ratio(field, scan, (0.0, 0.0, 0.0), pose,
                           hit_distance_m=0.10, max_range_m=8.0)
    assert tight < loose


def _assess(room, scan, pose):
    return assess_scan(_field(room), scan, (0., 0., 0.), pose,
                       hit_distance_m=.1, max_range_m=8.)


def test_new_and_removed_objects_do_not_hide_matching_room_walls(room):
    pose = (.8, .4, .3)
    scan = room.scan(*pose)
    # A new obstacle and an opened/removed surface affect different sectors.
    ranges = list(scan.ranges)
    ranges[10:45] = [r*.5 for r in ranges[10:45]]
    ranges[185:220] = [r+1.0 for r in ranges[185:220]]
    scan.ranges = ranges
    score = _assess(room, scan, pose)
    assert score.accepted and score.ratio < .95
    assert score.reason == 'limited_map_difference'
    assert score.support_sectors >= 6


@pytest.mark.parametrize('offset', [(0.15, 0., 0.), (0., -.15, 0.), (0., 0., .18)])
def test_pose_errors_with_environment_changes_are_still_rejected(room, offset):
    pose = (.8, .4, .3)
    scan = room.scan(*pose)
    ranges = list(scan.ranges)
    ranges[10:55] = [r*.5 for r in ranges[10:55]]
    scan.ranges = ranges
    assert not _assess(room, scan, tuple(a+b for a, b in zip(pose, offset))).accepted


def test_too_much_changed_environment_is_not_success(room):
    pose = (.8, .4, .3)
    scan = room.scan(*pose)
    ranges = list(scan.ranges)
    ranges[:140] = [r*.4 for r in ranges[:140]]
    scan.ranges = ranges
    assert not _assess(room, scan, pose).accepted


def test_parallel_corridor_walls_cannot_confirm_position_along_corridor(room):
    grid = np.zeros((44, 1200), dtype=np.int16)
    grid[:2, :] = grid[-2:, :] = 100
    field = distance_field(1200, 44, .05, (-30., -1.1, 0.), grid)
    scan = room.scan(0., 0., 0.)
    angles = scan.angle_min + np.arange(360)*scan.angle_increment
    scan.ranges = [1./abs(math.sin(a)) if abs(math.sin(a)) > .001 else math.inf
                   for a in angles]
    score = assess_scan(field, scan, (0., 0., 0.), (0., 0., 0.),
                        hit_distance_m=.1, max_range_m=8.)
    assert score.ratio > .95
    assert not score.accepted and score.reason == 'ambiguous_geometry'


def test_sparse_scan_is_not_enough_even_with_perfect_overlap(room):
    pose = (.8, .4, .3)
    scan = room.scan(*pose, beams=30)
    assert not _assess(room, scan, pose).accepted


def test_an_identical_room_elsewhere_is_not_environment_change(room):
    pose = (.8, .4, .3)
    scan = room.scan(*pose)
    original = np.asarray(room.grid().data).reshape(80, 80)
    field = distance_field(160, 80, .05, (-2., -2., 0.),
                           np.concatenate((original, original), axis=1))
    assert assess_scan(field, scan, (0., 0., 0.), pose,
                       hit_distance_m=.1, max_range_m=8.).accepted
    assert not alignment_is_unique(field, scan, (0., 0., 0.), pose, max_range_m=8.)


def test_asymmetric_room_remains_identifiable_with_a_new_obstacle(room):
    pose = (.8, .4, .3)
    scan = room.scan(*pose)
    ranges = list(scan.ranges)
    ranges[10:45] = [r*.5 for r in ranges[10:45]]
    scan.ranges = ranges
    assert alignment_is_unique(_field(room), scan, (0., 0., 0.), pose, max_range_m=8.)
