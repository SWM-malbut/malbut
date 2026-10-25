"""Check closed-tour geometry, useful coverage, fixed endpoints and replanning."""

import math

import numpy as np
import pytest

from malbut_patrol.coverage import CoverageGrid, CoveragePlanner, CoverageProfile
from malbut_patrol.route import ReturnRoutePlanner, improve_order


def planner(cells=None, start=(.55, .55)):
    if cells is None:
        cells = np.zeros((40, 80), dtype=np.int8)
    return CoveragePlanner(CoverageGrid(cells, .1, 0., 0.),
                           CoverageProfile(2., .9, 1.), start, .15)


def test_planned_coverage_is_not_evidence_and_replanning_removes_visited_goals():
    p = planner()
    routes = ReturnRoutePlanner(p, (.55, .55))
    before = p._observed.copy()
    first = routes.plan((.55, .55))
    assert len(first.viewpoints) > 1 and first.predicted_complete
    assert np.array_equal(p._observed, before) and not p._attempted
    reached = first.viewpoints[0]
    p.mark_attempted(reached.index)
    p.mark_observed(reached.x, reached.y, reached.yaw, 2*math.pi)
    remaining = routes.plan((reached.x, reached.y))
    assert reached.index not in [v.index for v in remaining.viewpoints]
    for v in remaining.viewpoints:
        p.mark_observed(v.x, v.y, v.yaw, 2*math.pi)
    assert p.complete
    done = routes.plan((reached.x, reached.y))
    assert not done.viewpoints and done.distance_m > 0


def test_return_distance_follows_wall_detour_and_rejects_disconnected_home():
    cells = np.zeros((60, 60), dtype=np.int8)
    cells[:45, 30] = 100
    start, home = (1.05, 1.05), (4.55, 1.05)
    p = planner(cells, start)
    route = ReturnRoutePlanner(p, home).plan(start, maximum_stops=0)
    assert route.distance_m > 9.0 > math.dist(start, home)
    cells[:, 30] = 100
    with pytest.raises(ValueError, match='return connection'):
        ReturnRoutePlanner(planner(cells, start), home)


def test_two_opt_shortens_crossed_tour_without_moving_endpoints():
    points = [(0, 0), (1, 1), (0, 1), (1, 0), (0, 0)]

    def distance(a, b):
        return math.dist(points[a], points[b])

    order = list(range(5))
    improved = improve_order(order, distance)

    def length(v):
        return sum(distance(a, b) for a, b in zip(v, v[1:]))

    assert improved[0] == 0 and improved[-1] == 4
    assert sorted(improved) == order
    assert length(improved) == pytest.approx(4.)
    assert length(improved) < length(order)


def test_current_and_final_locations_change_the_planned_order():
    p = planner()
    current = (3.55, 1.55)
    left = ReturnRoutePlanner(p, (.55, 1.55)).plan(current)
    right = ReturnRoutePlanner(p, (7.05, 1.55)).plan(current)
    assert left.viewpoints and right.viewpoints
    assert [v.index for v in left.viewpoints] != [v.index for v in right.viewpoints]


def test_blocked_goal_is_removed_without_erasing_observation_history():
    p = planner()
    routes = ReturnRoutePlanner(p, (.55, .55))
    initial = routes.plan((.55, .55))
    blocked = initial.viewpoints[0]
    retry = routes.plan(
        (.55, .55), lambda x, y: math.dist((x, y), (blocked.x, blocked.y)) > .01)
    assert blocked.index not in [v.index for v in retry.viewpoints]
    assert not p._observed.any()


def test_planning_can_be_interrupted_during_distance_search():
    routes = ReturnRoutePlanner(planner(), (.55, .55))

    def cancel():
        raise RuntimeError('canceled by caller')

    with pytest.raises(RuntimeError, match='canceled by caller'):
        routes.plan((.55, .55), checkpoint=cancel)


def test_no_redundant_spins_when_only_unavailable_room_entry_remains():
    room = {'features': [{'properties': {'name': 'living'}, 'geometry': {
        'type': 'Polygon', 'coordinates': [[[4, 0], [8, 0], [8, 4], [4, 4], [4, 0]]]}}]}
    p = CoveragePlanner(CoverageGrid(np.zeros((40, 80)), .1, 0, 0),
                        CoverageProfile(2, .9, 1), (.55, .55), .15, room)
    # Coverage already exceeds the target, but the required room was not entered.
    p._observed[:] = p._target
    left = np.flatnonzero(p._target & (np.tile(np.arange(80), 40) < 30))
    p._observed[left[:10]] = False
    p._observed_count = int(p._observed.sum())
    assert not p.complete and p.coverage_ratio > .9
    route = ReturnRoutePlanner(p, (.55, .55)).plan((.55, .55), lambda x, y: x < 4)
    assert route.viewpoints == () and not route.predicted_complete


def test_added_live_viewpoints_can_be_planned_with_a_fixed_return():
    p = planner()
    original = set(p._candidates)

    def allowed(x, y):
        return p.grid.world_to_cell(x, y) not in original
    assert p.refresh_candidates(allowed) > 0
    route = ReturnRoutePlanner(p, (.55, .55)).plan((.55, .55), allowed)
    assert route.viewpoints and math.isfinite(route.distance_m)
    assert all(allowed(v.x, v.y) for v in route.viewpoints)
    assert not p._observed.any()
