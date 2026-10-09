"""Score scans against a saved map without ROS communication."""

import math

from malbut_relocalization.scan_match import distance_field, match_ratio, refine


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


def test_a_pose_a_little_off_is_fitted_back_onto_the_walls(room):
    """AMCL after one turn is centimetres and degrees off; the fit removes that."""
    field = _field(room)
    scan = room.scan(0.8, 0.4, 0.3)
    start = (0.8 + 0.2, 0.4 - 0.15, 0.3 - math.radians(6))
    before, _ = match_ratio(field, scan, (0.0, 0.0, 0.0), start,
                            hit_distance_m=0.15, max_range_m=8.0)
    fitted = refine(field, scan, (0.0, 0.0, 0.0), start, max_range_m=8.0)
    after, _ = match_ratio(field, scan, (0.0, 0.0, 0.0), fitted,
                           hit_distance_m=0.15, max_range_m=8.0)
    assert before < 0.9 and after > 0.95
    assert math.hypot(fitted[0] - 0.8, fitted[1] - 0.4) < 0.02
    assert abs(fitted[2] - 0.3) < math.radians(0.5)


def test_the_fit_keeps_a_right_pose_and_a_scan_without_returns(room):
    field = _field(room)
    scan = room.scan(0.8, 0.4, 0.3)
    x, y, yaw = refine(field, scan, (0.0, 0.0, 0.0), (0.8, 0.4, 0.3), max_range_m=8.0)
    assert math.hypot(x - 0.8, y - 0.4) <= 0.01 and abs(yaw - 0.3) <= math.radians(0.4)
    scan.ranges = [math.inf] * len(scan.ranges)
    assert refine(field, scan, (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), max_range_m=8.0) == (
        1.0, 0.0, 0.0)
