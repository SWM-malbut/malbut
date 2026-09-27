"""Live-costmap rays for the camera-range target and the short line fallback."""

import math

from .costmap_tracking import CostmapGrid
from .geometry import Point2D, distance


def find_reachable_approach_goal(
    grid: CostmapGrid,
    start: Point2D,
    requested_goal: Point2D,
    maximum_cost: int,
) -> Point2D | None:
    """Follow a checked ray, allowing only outward travel from soft inflation."""
    grid.validate()
    if not 0 <= maximum_cost < 255:
        raise ValueError('maximum goal cost must be in [0, 254]')
    if not all(math.isfinite(value) for value in (
        start.x, start.y, requested_goal.x, requested_goal.y,
    )):
        return None
    start_cell = grid.world_to_cell(start)
    if start_cell is None or distance(start, requested_goal) <= 1e-9:
        return None
    cosine, sine = math.cos(grid.origin_yaw), math.sin(grid.origin_yaw)
    offset_x, offset_y = start.x - grid.origin.x, start.y - grid.origin.y
    x = (cosine * offset_x + sine * offset_y) / grid.resolution
    y = (-sine * offset_x + cosine * offset_y) / grid.resolution
    world_dx, world_dy = requested_goal.x - start.x, requested_goal.y - start.y
    dx = (cosine * world_dx + sine * world_dy) / grid.resolution
    dy = (-sine * world_dx + cosine * world_dy) / grid.resolution
    if not all(math.isfinite(value) for value in (x, y, dx, dy)):
        return None
    step_x = 0 if abs(dx) < 1e-12 else 1 if dx > 0.0 else -1
    step_y = 0 if abs(dy) < 1e-12 else 1 if dy > 0.0 else -1
    cell_x, cell_y = start_cell
    # A ray exactly along a grid edge touches the cells on both sides.
    edge_x = step_x == 0 and abs(x - round(x)) < 1e-9
    edge_y = step_y == 0 and abs(y - round(y)) < 1e-9

    def cell_cost(xx, yy):
        costs = []
        for checked_y in (yy, yy - 1) if edge_y else (yy,):
            for checked_x in (xx, xx - 1) if edge_x else (xx,):
                if not (0 <= checked_x < grid.width and 0 <= checked_y < grid.height):
                    return None
                cost = grid.cost(checked_x, checked_y)
                # Nav2's 253/254/255 mean inscribed collision, occupied and
                # unknown. None of these may be crossed, including at start.
                if not 0 <= cost < 253:
                    return None
                costs.append(cost)
        return max(costs)

    start_cost = cell_cost(cell_x, cell_y)
    if start_cost is None:
        return None
    # Goal cost is a low-cost preference, not Nav2's collision boundary.
    # A robot already in the graded band may leave it, never go deeper or
    # stop inside that band. Once out, preserve the usual low-cost corridor.
    allowed_cost = max(maximum_cost, start_cost)

    def safe(xx, yy):
        cost = cell_cost(xx, yy)
        return cost is not None and cost <= allowed_cost
    delta_x = abs(1.0 / dx) if step_x else math.inf
    delta_y = abs(1.0 / dy) if step_y else math.inf
    crossing_x = (
        (cell_x + (step_x > 0) - x) / dx if step_x else math.inf
    )
    crossing_y = (
        (cell_y + (step_y > 0) - y) / dy if step_y else math.inf
    )
    entered_t = last_safe_t = 0.0
    # A monotone ray crosses at most width + height cells before leaving.
    for _ in range(grid.width + grid.height + 2):
        crossing_t = min(crossing_x, crossing_y)
        if crossing_t > 1.0:
            return requested_goal if allowed_cost <= maximum_cost else None
        if crossing_t > entered_t and allowed_cost <= maximum_cost:
            # Keep the candidate just inside the verified free segment rather
            # than on a boundary that world_to_cell may round into an obstacle.
            margin = min(
                (crossing_t - entered_t) * 0.5,
                1e-6 / max(abs(dx), abs(dy)),
            )
            last_safe_t = crossing_t - margin
        corner = math.isclose(crossing_x, crossing_y, rel_tol=0.0, abs_tol=1e-12)
        crosses_x = corner or crossing_x < crossing_y
        crosses_y = corner or crossing_y < crossing_x
        if corner:
            if not (safe(cell_x + step_x, cell_y)
                    and safe(cell_x, cell_y + step_y)):
                break
            next_x, next_y = cell_x + step_x, cell_y + step_y
        elif crossing_x < crossing_y:
            next_x, next_y = cell_x + step_x, cell_y
        else:
            next_x, next_y = cell_x, cell_y + step_y
        if not safe(next_x, next_y):
            break
        cell_x, cell_y = next_x, next_y
        allowed_cost = max(maximum_cost, cell_cost(cell_x, cell_y))
        entered_t = crossing_t
        if crossing_t >= 1.0:
            return requested_goal if allowed_cost <= maximum_cost else None
        if crosses_x:
            crossing_x += delta_x
        if crosses_y:
            crossing_y += delta_y
    if last_safe_t <= 0.0:
        return None
    candidate = Point2D(
        start.x + last_safe_t * world_dx,
        start.y + last_safe_t * world_dy,
    )
    return candidate if distance(start, candidate) > 1e-9 else None


def first_admissible_point_on_ray(
    grid: CostmapGrid,
    origin: Point2D,
    lower_bound: Point2D,
    maximum_cost: int,
) -> Point2D | None:
    """Return the first free point at or beyond a camera range bound."""
    grid.validate()
    if not 0 <= maximum_cost < 255:
        raise ValueError('maximum goal cost must be in [0, 254]')
    lower_bound_distance = distance(origin, lower_bound)
    if lower_bound_distance <= 1e-9:
        return None
    direction_x = (lower_bound.x - origin.x) / lower_bound_distance
    direction_y = (lower_bound.y - origin.y) / lower_bound_distance
    step_m = max(0.01, grid.resolution * 0.5)
    maximum_steps = 2 * (grid.width + grid.height) + 1
    for step in range(maximum_steps):
        range_m = lower_bound_distance + step * step_m
        point = Point2D(
            origin.x + direction_x * range_m,
            origin.y + direction_y * range_m,
        )
        cell = grid.world_to_cell(point)
        if cell is None:
            break
        if _cell_is_admissible(grid, cell[0], cell[1], maximum_cost):
            return point
    return None


def _cell_is_admissible(
    grid: CostmapGrid,
    cell_x: int,
    cell_y: int,
    maximum_cost: int,
) -> bool:
    if not 0 <= cell_x < grid.width or not 0 <= cell_y < grid.height:
        return False
    return grid.cost(cell_x, cell_y) <= maximum_cost
