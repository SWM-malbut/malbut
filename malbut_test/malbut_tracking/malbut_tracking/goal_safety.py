"""Project tracking destinations into open global-costmap space."""

from dataclasses import dataclass
import heapq
import math
from typing import Sequence

from .costmap_tracking import CostmapGrid
from .geometry import Point2D, distance, normalize_angle


@dataclass(frozen=True)
class SafeNavigationGoal:
    """A costmap-admissible position and an open planning heading."""

    position: Point2D
    yaw: float
    position_adjusted: bool
    heading_adjusted: bool
    openness: float


def pad_static_map(
    grid: CostmapGrid,
    occupied_threshold: int,
    padding_radius_m: float,
) -> CostmapGrid:
    """Return one cached static grid padded to the Nav2 inflation radius."""
    grid.validate()
    if not 0 <= occupied_threshold <= 100:
        raise ValueError('occupied threshold must be in [0, 100]')
    if padding_radius_m < 0.0:
        raise ValueError('static-map padding radius must be non-negative')
    if padding_radius_m == 0.0:
        return grid

    radius_cells = math.ceil(padding_radius_m / grid.resolution)
    offsets = tuple(
        (offset_x, offset_y)
        for offset_y in range(-radius_cells, radius_cells + 1)
        for offset_x in range(-radius_cells, radius_cells + 1)
        if math.hypot(offset_x, offset_y) * grid.resolution
        <= padding_radius_m + 1e-9
    )
    padded_costs = list(grid.costs)
    occupied_cells = tuple(
        (cell_x, cell_y)
        for cell_y in range(grid.height)
        for cell_x in range(grid.width)
        if grid.cost(cell_x, cell_y) >= occupied_threshold
    )
    for occupied_x, occupied_y in occupied_cells:
        for offset_x, offset_y in offsets:
            cell_x = occupied_x + offset_x
            cell_y = occupied_y + offset_y
            if not 0 <= cell_x < grid.width or not 0 <= cell_y < grid.height:
                continue
            index = cell_y * grid.width + cell_x
            # Preserve unknown cells. They are already non-traversable, while
            # only real static obstacles seed padding just like Nav2.
            if int(padded_costs[index]) >= 0:
                padded_costs[index] = max(
                    int(padded_costs[index]),
                    occupied_threshold,
                )
    return CostmapGrid(
        frame_id=grid.frame_id,
        stamp_seconds=grid.stamp_seconds,
        resolution=grid.resolution,
        width=grid.width,
        height=grid.height,
        origin=grid.origin,
        origin_yaw=grid.origin_yaw,
        costs=tuple(padded_costs),
    )


def plan_static_path(
    grid: CostmapGrid,
    start: Point2D,
    goal: Point2D,
    occupied_threshold: int = 65,
) -> tuple[Point2D, ...] | None:
    """Plan an 8-connected route on one cached static SLAM grid."""
    grid.validate()
    if not 0 <= occupied_threshold <= 100:
        raise ValueError('occupied threshold must be in [0, 100]')
    start_cell = grid.world_to_cell(start)
    goal_cell = grid.world_to_cell(goal)
    if start_cell is None or goal_cell is None:
        return None
    if start_cell == goal_cell:
        return (grid.cell_center(*start_cell),)

    def traversable(cell_x: int, cell_y: int) -> bool:
        if not 0 <= cell_x < grid.width or not 0 <= cell_y < grid.height:
            return False
        cost = grid.cost(cell_x, cell_y)
        return 0 <= cost < occupied_threshold

    if not traversable(*start_cell) or not traversable(*goal_cell):
        return None

    neighbors = (
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    )
    frontier = [(0.0, 0.0, start_cell)]
    cost_to_cell = {start_cell: 0.0}
    parent: dict[tuple[int, int], tuple[int, int]] = {}
    visited = set()
    while frontier:
        _, current_cost, current = heapq.heappop(frontier)
        if current in visited:
            continue
        visited.add(current)
        if current == goal_cell:
            route = [current]
            while route[-1] != start_cell:
                route.append(parent[route[-1]])
            route.reverse()
            return tuple(grid.cell_center(*cell) for cell in route)

        for offset_x, offset_y in neighbors:
            neighbor = (current[0] + offset_x, current[1] + offset_y)
            if not traversable(*neighbor):
                continue
            if offset_x != 0 and offset_y != 0:
                # Do not cut diagonally through the corner of fixed geometry.
                if not traversable(current[0] + offset_x, current[1]):
                    continue
                if not traversable(current[0], current[1] + offset_y):
                    continue
            step_cost = math.hypot(offset_x, offset_y)
            candidate_cost = current_cost + step_cost
            if candidate_cost >= cost_to_cell.get(neighbor, math.inf):
                continue
            cost_to_cell[neighbor] = candidate_cost
            parent[neighbor] = current
            heuristic = math.hypot(
                goal_cell[0] - neighbor[0],
                goal_cell[1] - neighbor[1],
            )
            heapq.heappush(
                frontier,
                (candidate_cost + heuristic, candidate_cost, neighbor),
            )
    return None


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


def project_navigation_goal(
    grid: CostmapGrid,
    requested_position: Point2D,
    requested_yaw: float,
    maximum_cost: int,
    search_radius_m: float,
    openness_radius_m: float,
    openness_preference_m: float,
    heading_probe_distance_m: float,
    minimum_heading_clearance_m: float,
    approach_origin: Point2D | None = None,
    static_path: Sequence[Point2D] | None = None,
) -> SafeNavigationGoal | None:
    """Move an unsafe planning goal to open space and avoid a wall."""
    grid.validate()
    if not 0 <= maximum_cost < 255:
        raise ValueError('maximum goal cost must be in [0, 254]')
    for name, value in (
        ('search radius', search_radius_m),
        ('openness radius', openness_radius_m),
        ('heading probe distance', heading_probe_distance_m),
        ('minimum heading clearance', minimum_heading_clearance_m),
    ):
        if value <= 0.0:
            raise ValueError(f'{name} must be positive')
    if openness_preference_m < 0.0:
        raise ValueError('openness preference must be non-negative')

    requested_cell = grid.world_to_cell(requested_position)
    if requested_cell is None:
        return None
    if static_path:
        for path_point in reversed(static_path):
            cell = grid.world_to_cell(path_point)
            if cell is None:
                continue
            if not _cell_is_admissible(grid, cell[0], cell[1], maximum_cost):
                continue
            safe_position = grid.cell_center(*cell)
            openness = _open_fraction(
                grid,
                cell[0],
                cell[1],
                maximum_cost,
                openness_radius_m,
            )
            safe_yaw = _open_heading(
                grid,
                safe_position,
                requested_yaw,
                maximum_cost,
                heading_probe_distance_m,
                minimum_heading_clearance_m,
            )
            return SafeNavigationGoal(
                position=safe_position,
                yaw=safe_yaw,
                position_adjusted=(
                    distance(safe_position, requested_position)
                    > grid.resolution * 0.75
                ),
                heading_adjusted=(
                    abs(normalize_angle(safe_yaw - requested_yaw))
                    > math.radians(1)
                ),
                openness=openness,
            )
        return None
    approach_distance = (
        distance(approach_origin, requested_position)
        if approach_origin is not None
        else 0.0
    )
    if approach_distance > 1e-9:
        approach_x = (
            requested_position.x - approach_origin.x
        ) / approach_distance
        approach_y = (
            requested_position.y - approach_origin.y
        ) / approach_distance
    else:
        approach_x = approach_y = 0.0
    radius_cells = max(1, math.ceil(search_radius_m / grid.resolution))
    candidates = []
    approach_candidates = []
    nearest_shift = math.inf
    for offset_y in range(-radius_cells, radius_cells + 1):
        for offset_x in range(-radius_cells, radius_cells + 1):
            if math.hypot(offset_x, offset_y) * grid.resolution > (
                search_radius_m + 1e-9
            ):
                continue
            cell_x = requested_cell[0] + offset_x
            cell_y = requested_cell[1] + offset_y
            if not _cell_is_admissible(grid, cell_x, cell_y, maximum_cost):
                continue
            point = grid.cell_center(cell_x, cell_y)
            follows_approach = False
            if approach_distance > 1e-9:
                relative_x = point.x - approach_origin.x
                relative_y = point.y - approach_origin.y
                progress = (
                    relative_x * approach_x + relative_y * approach_y
                )
                lateral_offset = abs(
                    relative_x * approach_y - relative_y * approach_x
                )
                cell_tolerance = grid.resolution * math.sqrt(0.5)
                follows_approach = not (
                    progress < -cell_tolerance
                    or progress > approach_distance + cell_tolerance
                    or lateral_offset > cell_tolerance
                )
            shift = distance(requested_position, point)
            candidate = (shift, cell_x, cell_y, point)
            candidates.append(candidate)
            if follows_approach:
                approach_candidates.append(candidate)
            nearest_shift = min(nearest_shift, shift)
    if not candidates:
        return None
    if approach_candidates:
        # Tracking first approaches the person from the robot-facing side.
        # If that corridor has no admissible cell, retain the existing
        # surrounding open-space fallback rather than refusing to move.
        candidates = approach_candidates
        nearest_shift = min(candidate[0] for candidate in candidates)

    best = None
    # A cell farther than this bound cannot beat the nearest cell even with
    # perfect openness. Avoid scanning hundreds of irrelevant neighborhoods
    # on every camera frame.
    competitive_shift = nearest_shift + openness_preference_m + 1e-9
    for shift, cell_x, cell_y, point in candidates:
        if shift > competitive_shift:
            continue
        openness = _open_fraction(
            grid,
            cell_x,
            cell_y,
            maximum_cost,
            openness_radius_m,
        )
        # Displacement remains the primary objective. Openness acts as a
        # bounded preference, so equally close cells choose the room side
        # rather than merely touching the inflation boundary.
        score = shift - openness_preference_m * openness
        rank = (score, shift, -openness, cell_y, cell_x)
        if best is None or rank < best[0]:
            best = (rank, point, openness)

    safe_position = best[1]
    openness = best[2]
    safe_yaw = _open_heading(
        grid,
        safe_position,
        requested_yaw,
        maximum_cost,
        heading_probe_distance_m,
        minimum_heading_clearance_m,
    )
    return SafeNavigationGoal(
        position=safe_position,
        yaw=safe_yaw,
        position_adjusted=(
            distance(safe_position, requested_position)
            > grid.resolution * 0.75
        ),
        heading_adjusted=(
            abs(normalize_angle(safe_yaw - requested_yaw)) > math.radians(1)
        ),
        openness=openness,
    )


def _cell_is_admissible(
    grid: CostmapGrid,
    cell_x: int,
    cell_y: int,
    maximum_cost: int,
) -> bool:
    if not 0 <= cell_x < grid.width or not 0 <= cell_y < grid.height:
        return False
    return grid.cost(cell_x, cell_y) <= maximum_cost


def _open_fraction(
    grid: CostmapGrid,
    center_x: int,
    center_y: int,
    maximum_cost: int,
    radius_m: float,
) -> float:
    radius_cells = max(1, math.ceil(radius_m / grid.resolution))
    admissible = 0
    total = 0
    for offset_y in range(-radius_cells, radius_cells + 1):
        for offset_x in range(-radius_cells, radius_cells + 1):
            if math.hypot(offset_x, offset_y) * grid.resolution > (
                radius_m + 1e-9
            ):
                continue
            total += 1
            if _cell_is_admissible(
                grid,
                center_x + offset_x,
                center_y + offset_y,
                maximum_cost,
            ):
                admissible += 1
    return admissible / max(1, total)


def _open_heading(
    grid: CostmapGrid,
    position: Point2D,
    requested_yaw: float,
    maximum_cost: int,
    probe_distance_m: float,
    minimum_clearance_m: float,
) -> float:
    requested_clearance = _ray_clearance(
        grid,
        position,
        requested_yaw,
        maximum_cost,
        probe_distance_m,
    )
    if requested_clearance >= minimum_clearance_m:
        return requested_yaw
    candidates = []
    for degrees in range(0, 91, 15):
        offsets = (0,) if degrees == 0 else (degrees, -degrees)
        for offset_degrees in offsets:
            yaw = normalize_angle(
                requested_yaw + math.radians(offset_degrees)
            )
            clearance = _ray_clearance(
                grid,
                position,
                yaw,
                maximum_cost,
                probe_distance_m,
            )
            candidates.append(
                (clearance, -abs(offset_degrees), -offset_degrees, yaw)
            )
    return max(candidates)[3]


def _ray_clearance(
    grid: CostmapGrid,
    position: Point2D,
    yaw: float,
    maximum_cost: int,
    maximum_distance_m: float,
) -> float:
    step = max(grid.resolution * 0.5, 0.02)
    travelled = 0.0
    while travelled <= maximum_distance_m + 1e-9:
        point = Point2D(
            position.x + travelled * math.cos(yaw),
            position.y + travelled * math.sin(yaw),
        )
        cell = grid.world_to_cell(point)
        if cell is None or not _cell_is_admissible(
            grid, cell[0], cell[1], maximum_cost
        ):
            return max(0.0, travelled - step)
        travelled += step
    return maximum_distance_m
