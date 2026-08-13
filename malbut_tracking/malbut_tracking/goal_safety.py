"""Project tracking destinations into open global-costmap space."""

from dataclasses import dataclass
import math

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
    radius_cells = max(1, math.ceil(search_radius_m / grid.resolution))
    candidates = []
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
            shift = distance(requested_position, point)
            candidates.append((shift, cell_x, cell_y, point))
            nearest_shift = min(nearest_shift, shift)
    if not candidates:
        return None

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
