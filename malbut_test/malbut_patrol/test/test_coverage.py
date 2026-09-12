"""Verify patrol geometry and coverage using small synthetic occupancy maps."""

import math

import numpy as np
import pytest

from malbut_patrol.coverage import CoverageGrid, CoveragePlanner, CoverageProfile


def _planner(cells=None, *, rooms=None, range_m=3.0, spacing_m=0.8,
             ratio=0.9, clearance=0.0, robot=(0.55, 0.55), origin=(0.0, 0.0, 0.0)):
    if cells is None:
        cells = np.zeros((30, 30), dtype=np.int8)
    grid = CoverageGrid(cells, 0.1, *origin)
    return CoveragePlanner(
        grid, CoverageProfile(range_m, ratio, spacing_m), robot,
        robot_clearance_m=clearance, rooms=rooms, camera_fov_rad=math.pi / 2.0,
    )


def _room(name, x0, y0, x1, y1):
    return {
        'type': 'Feature',
        'properties': {'role': 'room', 'name': name},
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[[x0, y0], [x1, y0], [x1, y1],
                             [x0, y1], [x0, y0]]],
        },
    }


def test_grid_preserves_ros_order_and_rotated_origin():
    grid = CoverageGrid(np.zeros((12, 20)), 0.1, 10.0, -4.0, math.pi / 2.0)
    x, y = grid.cell_to_world(3, 7)
    assert (x, y) == pytest.approx((9.65, -3.25))
    assert grid.world_to_cell(x, y) == (3, 7)
    assert grid.world_to_cell(10.1, -4.0)[0] < 0
    assert not grid.cells.flags.writeable


def test_camera_frames_respect_horizontal_field_of_view():
    planner = _planner(robot=(1.55, 1.55))
    added = planner.mark_observed(1.55, 1.55, 0.0)
    observed = planner._observed.reshape(planner.grid.cells.shape)
    assert added > 0.0
    assert observed[15, 25]
    assert not observed[15, 5]
    assert not observed[25, 15]
    assert planner.mark_observed(1.55, 1.55, 0.0) == 0.0
    assert planner.mark_observed(1.55, 1.55, math.pi) > 0.0


@pytest.mark.parametrize('barrier', [100, -1])
def test_wall_and_unknown_cells_block_camera_sight(barrier):
    cells = np.zeros((40, 40), dtype=np.int8)
    cells[:32, 20] = barrier
    planner = _planner(cells, range_m=5.0, robot=(1.05, 1.05))
    # Both sides are reachable around the top of the wall and in the denominator.
    assert planner._target.reshape(cells.shape)[10, 30]
    planner.mark_observed(1.05, 1.05, 0.0, math.pi / 2.0)
    observed = planner._observed.reshape(cells.shape)
    assert observed[10, 18]
    assert not observed[10, 20]
    assert not observed[10, 30]


def test_diagonal_corner_is_not_a_passage_or_a_line_of_sight():
    cells = np.full((3, 3), 100, dtype=np.int8)
    cells[0, 0] = cells[1, 1] = 0
    grid = CoverageGrid(cells, 1.0, 0.0, 0.0)
    planner = CoveragePlanner(grid, CoverageProfile(5.0, 1.0, 1.0),
                              (0.5, 0.5), robot_clearance_m=0.0)
    assert planner.candidate_count == 1
    assert planner.target_area_m2 == 1.0
    planner.mark_observed(0.5, 0.5, 0.0, 2.0 * math.pi)
    assert not planner._observed.reshape(cells.shape)[1, 1]


def test_narrow_door_excludes_unreachable_room_without_endless_goals():
    cells = np.zeros((30, 60), dtype=np.int8)
    cells[:, 30] = 100
    cells[15, 30] = 0
    rooms = {'features': [_room('closed-room', 3.1, 0.0, 6.0, 3.0)]}
    planner = _planner(cells, rooms=rooms, clearance=0.16, robot=(1.05, 1.05))
    assert planner.inaccessible_room_names == ('closed-room',)
    assert not planner.unvisited_room_names
    assert not planner.reachable[:, 31:].any()
    for _ in range(planner.candidate_count):
        goal = planner.select((1.05, 1.05))
        if goal is None:
            break
        assert goal.x < 3.0
        planner.mark_attempted(goal.index)
    assert planner.select((1.05, 1.05)) is None
    assert not planner.complete


def test_room_seen_from_doorway_still_requires_entering_the_room():
    rooms = {'features': [_room('room', 1.5, 0.0, 3.0, 3.0)]}
    planner = _planner(rooms=rooms, range_m=5.0, robot=(1.05, 1.55), ratio=0.5)
    planner.mark_observed(1.05, 1.55, 0.0, 2.0 * math.pi)
    assert planner.coverage_ratio > 0.95
    assert planner.unvisited_room_names == ('room',)
    assert not planner.complete
    goal = planner.select((1.05, 1.55))
    assert goal is not None and goal.x > 1.5
    planner.mark_observed(goal.x, goal.y, goal.yaw, 2.0 * math.pi)
    assert planner.complete


def test_unvisited_room_precedes_more_viewpoints_in_already_visited_room():
    rooms = {'features': [_room('left', 0.0, 0.0, 1.4, 3.0),
                          _room('right', 1.6, 0.0, 3.0, 3.0)]}
    planner = _planner(rooms=rooms, range_m=0.5, robot=(0.55, 0.55))
    planner.mark_observed(0.55, 0.55, 0.0)
    assert planner.unvisited_room_names == ('right',)
    goal = planner.select((0.55, 0.55))
    assert goal is not None and goal.x > 1.6


def test_every_reachable_room_needs_its_own_coverage_target():
    rooms = {'features': [_room('left', 0.0, 0.0, 1.4, 3.0),
                          _room('right', 1.6, 0.0, 3.0, 3.0)]}
    planner = _planner(rooms=rooms, range_m=0.6)
    planner.mark_observed(0.55, 0.55, 0.0)
    planner.mark_observed(2.05, 0.55, 0.0)
    assert not planner.unvisited_room_names
    assert set(planner.uncovered_room_names) == {'left', 'right'}
    assert not planner.complete


def test_rooms_with_identical_display_names_are_not_merged():
    rooms = {'features': [_room('bedroom', 0.0, 0.0, 1.4, 3.0),
                          _room('bedroom', 1.6, 0.0, 3.0, 3.0)]}
    planner = _planner(rooms=rooms, range_m=0.5)
    assert len(planner.unvisited_room_names) == 2
    planner.mark_observed(0.55, 0.55, 0.0)
    assert len(planner.unvisited_room_names) == 1
    assert planner.select((0.55, 0.55)).x > 1.6


def test_stronger_profile_samples_more_closely_and_credits_shorter_range():
    weak = _planner(range_m=4.0, spacing_m=1.5, ratio=0.8, robot=(1.55, 1.55))
    strong = _planner(range_m=1.0, spacing_m=0.5, ratio=0.95, robot=(1.55, 1.55))
    assert strong.candidate_count > weak.candidate_count
    weak.mark_observed(1.55, 1.55, 0.0, 2.0 * math.pi)
    strong.mark_observed(1.55, 1.55, 0.0, 2.0 * math.pi)
    assert weak.observed_area_m2 > strong.observed_area_m2
    assert weak.complete
    assert not strong.complete


def test_candidate_rotation_and_coverage_follow_map_origin_yaw():
    cells = np.zeros((15, 30), dtype=np.int8)
    base = _planner(cells, robot=(0.55, 0.55))
    rotated = _planner(cells, robot=(8.45, -1.45), origin=(9.0, -2.0, math.pi / 2))
    assert rotated.candidate_count == base.candidate_count
    first = base.select((0.55, 0.55))
    transformed = rotated.select((8.45, -1.45))
    assert transformed.index == first.index
    assert (transformed.x, transformed.y) == pytest.approx((9 - first.y, -2 + first.x))
    assert math.cos(transformed.yaw - first.yaw) == pytest.approx(0.0, abs=1e-10)
    base.mark_observed(0.55, 0.55, 0.0)
    rotated.mark_observed(8.45, -1.45, math.pi / 2)
    assert rotated.observed_area_m2 == pytest.approx(base.observed_area_m2)


@pytest.mark.parametrize('cells', [
    np.zeros((0, 0)), np.full((10, 10), -1), np.full((10, 10), 100),
])
def test_empty_unknown_and_occupied_maps_have_no_false_success(cells):
    planner = _planner(cells)
    assert planner.candidate_count == 0
    assert planner.target_area_m2 == 0.0
    assert planner.coverage_ratio == 0.0
    assert not planner.complete
    assert planner.select((0.55, 0.55)) is None
    assert planner.mark_observed(0.55, 0.55, 0.0) == 0.0


def test_rejected_or_failed_candidates_do_not_repeat():
    planner = _planner(range_m=0.7)
    assert planner.select((0.55, 0.55), allowed=lambda x, y: False) is None
    selected = set()
    while True:
        goal = planner.select((0.55, 0.55), allowed=lambda x, y: x >= 1.0)
        if goal is None:
            break
        assert goal.x >= 1.0
        assert goal.index not in selected
        selected.add(goal.index)
        planner.mark_attempted(goal.index)
    assert 0 < len(selected) <= planner.candidate_count
    assert not planner.complete


def test_frame_observations_can_complete_a_finite_patrol():
    planner = _planner(range_m=1.0, spacing_m=0.6, ratio=0.95)
    robot = (0.55, 0.55)
    for _ in range(planner.candidate_count):
        goal = planner.select(robot)
        if goal is None:
            break
        robot = goal.x, goal.y
        # Emulate frames captured while actually looking around at each goal.
        for yaw in np.linspace(-math.pi, math.pi, 12, endpoint=False):
            planner.mark_observed(*robot, yaw)
        planner.mark_attempted(goal.index)
    assert planner.complete
    assert planner.coverage_ratio >= 0.95
    assert planner.observed_area_m2 + planner.remaining_area_m2 == pytest.approx(
        planner.target_area_m2
    )


@pytest.mark.parametrize('profile', [
    (0.0, 0.9, 1.0), (3.0, 1.1, 1.0), (3.0, 0.9, -1.0), (math.inf, 0.9, 1.0),
])
def test_invalid_profiles_fail_before_planning(profile):
    with pytest.raises(ValueError):
        CoverageProfile(*profile)
