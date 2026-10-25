"""Geometry regressions for turning and return connections."""

import math

import numpy as np
import pytest

from malbut_patrol.coverage import CoverageGrid
from malbut_patrol.maneuver import ManeuverSpace


def corridor(blocked=False, yaw=0., narrow=False):
    cells = np.zeros((160, 120), dtype=np.int16)
    cells[[0, -1], :] = 100
    cells[:, [0, -1]] = 100
    # Two rooms joined by a passage with known turning clearance.
    half = 8 if narrow else 15
    cells[50:110, 1:60-half] = 100
    cells[50:110, 60+half:-1] = 100
    if blocked:
        cells[78:82, :] = 100
    return CoverageGrid(cells, .02, -1.2, -1.6, yaw)


def test_turn_then_drive_connects_rooms_without_lateral_motion():
    grid = corridor()
    space = ManeuverSpace(grid)
    home, current = (0., -1.25, 0.), (0., 1.25, 0.)
    assert space.connected(home, current)
    mask = space.observation_mask(current, home)
    assert mask[grid.world_to_cell(*current[:2])]
    assert mask[grid.world_to_cell(0., 0.)]


def test_free_centre_does_not_prove_room_to_turn():
    grid = corridor(narrow=True)
    space = ManeuverSpace(grid)
    centre = grid.world_to_cell(0., 0.)
    assert grid.cells[centre] == 0
    assert not space.connected((0., 0., 0.), (0., 1.25, 0.))
    assert not space.rotation_safe[centre]


def test_live_obstacle_can_close_an_otherwise_valid_escape_route():
    home, current = (0., -1.25, 0.), (0., 1.25, 0.)
    space = ManeuverSpace(corridor(True))
    assert space.rotation_safe[space.grid.world_to_cell(*current[:2])]
    assert not space.connected(home, current)
    assert not space.observation_mask(current, home).any()


def test_planned_spot_accounts_for_nav2_arrival_error_but_transit_stays_open():
    grid = corridor()
    space = ManeuverSpace(grid)
    home, current = (0., -1.25, 0.), (0., 1.25, 0.)
    assert space.connected(home, current)
    # An exact centre pose can turn in this passage, but a goal completed
    # 12 cm off-centre may not. Pick a room interior for a planned observation.
    assert space.observation_mask(current, home)[grid.world_to_cell(0., 0.)]
    mask = space.observation_mask(current, home, arrival_tolerance=.12)
    assert not mask[grid.world_to_cell(0., 0.)]
    assert mask[grid.world_to_cell(*current[:2])]


@pytest.mark.parametrize('tolerance', [-1., math.nan])
def test_invalid_arrival_tolerance(tolerance):
    with pytest.raises(ValueError):
        ManeuverSpace(corridor()).observation_mask((0., 0.), (0., 0.), tolerance)


def test_unknown_and_map_edges_cannot_supply_turning_space():
    cells = np.zeros((80, 80), dtype=np.int16)
    cells[:, 40:] = -1
    grid = CoverageGrid(cells, .02, 0., 0.)
    space = ManeuverSpace(grid)
    assert not space.rotation_safe[grid.world_to_cell(.75, .8)]
    assert not space.rotation_safe[grid.world_to_cell(.05, .8)]
    assert space.rotation_safe[grid.world_to_cell(.4, .8)]


def test_inflation_gradient_is_not_inflated_as_an_obstacle_again():
    cells = np.full((100, 100), 70, dtype=np.int16)
    grid = CoverageGrid(cells, .02, 0., 0.)
    space = ManeuverSpace(grid)
    assert space.rotation_safe[50, 50]
    assert space.connected((.8, 1., 0.), (1.2, 1., 0.))


def test_rotated_grid_connectivity():
    grid = corridor(yaw=.37)
    a = (*map(float, grid.cell_to_world(20, 60)), .37)
    b = (*map(float, grid.cell_to_world(140, 60)), .37)
    space = ManeuverSpace(grid)
    assert space.connected(a, b)
    assert not space.connected((99., 99., .37), b)


def test_safe_off_centre_arrival_connects_without_rounding_into_a_wall():
    cells = np.zeros((80, 80), dtype=np.int16)
    cells[:, :20] = 100
    grid = CoverageGrid(cells, .05, 0., 0.)
    space = ManeuverSpace(grid)
    # Wall ends at x=1.0. The actual body sweep clears it by 6 mm, but
    # demanding clearance for every point of this cell would reject it.
    pose, home = (1.195, 2., 0.), (2., 2., 0.)
    assert not space.rotation_safe[grid.world_to_cell(*pose[:2])]
    assert space.connected(pose, home)
    assert space.observation_mask(pose, home).any()
    assert not space.connected((1.18, 2., 0.), home)


def test_endpoint_connector_cannot_skip_a_corner_obstacle():
    cells = np.zeros((80, 80), dtype=np.int16)
    cells[40, 40] = 100
    space = ManeuverSpace(CoverageGrid(cells, .05, 0., 0.))
    assert space._sweep_clear((1.8, 2.2), (1.8, 2.2))
    assert space._sweep_clear((2.2, 1.8), (2.2, 1.8))
    assert not space._sweep_clear((1.8, 2.2), (2.2, 1.8))


@pytest.mark.parametrize('kwargs', [{'length': 0}, {'width': math.nan},
                                    {'padding': -1}])
def test_invalid_footprints_fail_closed(kwargs):
    with pytest.raises(ValueError):
        ManeuverSpace(corridor(), **kwargs)
