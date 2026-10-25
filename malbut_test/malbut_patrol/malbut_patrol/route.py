"""
Plan a bounded observation tour with a fixed return point and measured coverage.

Greedy coverage gain / cheapest insertion chooses a useful subset of viewpoints.
Fixed-endpoint 2-opt improves its order. Distances are shortest four-neighbor
paths on the static clearance grid, not Euclidean shortcuts or exact Nav2 costs.
Replanning never credits predicted visibility as an actual observation.
"""

from collections import OrderedDict
from dataclasses import dataclass
import math

import numpy as np

from malbut_patrol.coverage import Viewpoint, _flood_distances


def improve_order(order, distance, checkpoint=None, passes=8):
    """Apply bounded 2-opt on a symmetric metric while preserving both endpoints."""
    order = list(order)
    for _ in range(passes):
        changed = False
        for first in range(1, len(order)-2):
            if checkpoint:
                checkpoint()
            for last in range(first+1, len(order)-1):
                a, b = order[first-1], order[first]
                c, d = order[last], order[last+1]
                if distance(a, c)+distance(b, d) < distance(a, b)+distance(c, d)-1e-9:
                    order[first:last+1] = reversed(order[first:last+1])
                    changed = True
        if not changed:
            break
    return order


@dataclass(frozen=True)
class PatrolRoute:
    """Separate planned camera goals from the fixed final return location."""

    viewpoints: tuple
    distance_m: float
    predicted_complete: bool


class ReturnRoutePlanner:
    """Reuse static distances, but rebuild the remaining tour from current evidence."""

    def __init__(self, coverage, return_xy):
        self.coverage = coverage
        self.return_xy = tuple(return_xy)
        home = coverage._nearest_cell(coverage.reachable, self.return_xy,
                                      coverage._clearance_m)
        if home is None:
            raise ValueError('Patrol start has no safe return connection')
        self.cells = coverage._candidates + [home]
        self.home = len(coverage._candidates)
        self._rows, self._cols = np.asarray(self.cells, dtype=int).T
        # Keep candidate distance vectors, not many full-map distance arrays.
        self._cache = OrderedDict()

    def _distances(self, cell, checkpoint):
        if cell not in self._cache:
            field = _flood_distances(self.coverage.reachable, cell, checkpoint)
            values = field[self._rows, self._cols].astype(float)
            values[values < 0] = np.inf
            values *= self.coverage.grid.resolution
            self._cache[cell] = values
            if len(self._cache) > 66:
                self._cache.popitem(last=False)
        self._cache.move_to_end(cell)
        return self._cache[cell]

    def plan(self, robot_xy, allowed=None, checkpoint=None, maximum_stops=64):
        """Return a finite tour; camera feedback determines whether to execute more."""
        p = self.coverage
        start = p._nearest_cell(p.reachable, robot_xy, p._clearance_m)
        if start is None:
            raise ValueError('Current pose has no safe route to patrol start')
        current = self._distances(start, checkpoint)
        if not math.isfinite(current[self.home]):
            raise ValueError('Patrol start is unreachable on the saved map')

        def distance(a, b):
            if a == -1:
                return current[b]
            if b == -1:
                return current[a]
            return self._distances(self.cells[a], checkpoint)[b]

        predicted = p._observed.copy()
        visits = set(p._visit_names)
        available = []
        for index, cell in enumerate(p._candidates):
            if index in p._attempted or not math.isfinite(current[index]):
                continue
            xy = tuple(map(float, p.grid.cell_to_world(*cell)))
            if allowed is None or allowed(*xy):
                available.append(index)
        route = [-1, self.home]
        complete = False
        for _ in range(maximum_stops+1):
            if checkpoint:
                checkpoint()
            missing_visits = set(p._room_cells)-visits if p.require_room_entry else set()
            deficient = {name for name, cells in p._room_cells.items()
                         if not cells.size or np.count_nonzero(predicted[cells])
                         < cells.size*p.profile.target_ratio}
            complete = (p._target_count > 0
                        and np.count_nonzero(predicted & p._target)
                        >= p._target_count*p.profile.target_ratio
                        and not missing_visits and not deficient)
            if complete or len(route)-2 >= maximum_stops:
                break
            useful = p._target.copy()
            if np.count_nonzero(predicted & p._target) >= p._target_count*p.profile.target_ratio:
                # Once total coverage is sufficient, extra pixels in completed
                # rooms cannot satisfy a missing room visit. Only pursue the
                # remaining room coverage or an actual entry into that room.
                useful[:] = False
                for name in deficient:
                    useful[p._room_cells[name]] = True
            best = None
            for index in available:
                visible = p._candidate_visible[index]
                gain = np.count_nonzero(~predicted[visible] & useful[visible])*p.grid.resolution**2
                rooms = set(p._candidate_rooms[index])
                visit = bool(rooms & missing_visits)
                if gain <= 0 and not visit:
                    continue
                extra, at = min((distance(a, index)+distance(b, index)-distance(a, b), k)
                                for k, (a, b) in enumerate(zip(route, route[1:]), 1))
                priority = 2 if visit else int(bool(rooms & deficient))
                key = priority, gain/(1+max(0., extra)), -extra, -index
                if best is None or key > best[0]:
                    best = key, index, at
            if best is None:
                break
            _, index, at = best
            route.insert(at, index)
            available.remove(index)
            predicted[p._candidate_visible[index]] = True
            visits.update(p._candidate_rooms[index])
        route = improve_order(route, distance, checkpoint)
        viewpoints = []
        for index in route[1:-1]:
            x, y = map(float, p.grid.cell_to_world(*p._candidates[index]))
            unseen = ~p._observed[p._candidate_visible[index]]
            viewpoints.append(Viewpoint(x, y, p._best_yaw(index, unseen), index))
        return PatrolRoute(tuple(viewpoints), float(sum(
            distance(a, b) for a, b in zip(route, route[1:]))), complete)
