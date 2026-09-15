"""Pure occupancy-grid frontier exploration, shared by robot and simulation."""

from __future__ import annotations

from dataclasses import dataclass
import math

import cv2
import numpy as np


FREE_THRESHOLD = 0.196
OCCUPIED_THRESHOLD = 0.65


@dataclass(frozen=True)
class MapGrid:
    """Immutable occupancy-grid snapshot in ROS map coordinates."""

    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    cells: np.ndarray
    stamp_ns: int = 0

    def world(self, row: int, column: int) -> tuple[float, float]:
        """Return the center of one grid cell in the map frame."""
        local_x = (column + 0.5) * self.resolution
        local_y = (row + 0.5) * self.resolution
        cosine = math.cos(self.origin_yaw)
        sine = math.sin(self.origin_yaw)
        return (
            self.origin_x + cosine * local_x - sine * local_y,
            self.origin_y + sine * local_x + cosine * local_y,
        )


@dataclass(frozen=True)
class Frontier:
    """One navigable boundary between known and unknown map cells."""

    x: float
    y: float
    yaw: float
    cell_count: int
    clearance_m: float
    distance_m: float


@dataclass(frozen=True)
class FrontierSearch:
    """Keep remaining boundaries separate from currently usable destinations."""

    candidates: list[Frontier]
    frontier_count: int


def map_grid_from_message(message: object) -> MapGrid:
    """Copy a ROS OccupancyGrid-like message into an immutable snapshot."""
    width = int(message.info.width)
    height = int(message.info.height)
    cells = np.asarray(message.data, dtype=np.int16)
    if width <= 0 or height <= 0 or cells.size != width * height:
        raise ValueError("invalid occupancy-grid dimensions")
    resolution = float(message.info.resolution)
    if not math.isfinite(resolution) or resolution <= 0.0:
        raise ValueError("invalid occupancy-grid resolution")
    orientation = message.info.origin.orientation
    yaw = math.atan2(
        2.0 * (
            orientation.w * orientation.z
            + orientation.x * orientation.y
        ),
        1.0 - 2.0 * (
            orientation.y * orientation.y
            + orientation.z * orientation.z
        ),
    )
    stamp = getattr(message.header, "stamp", None)
    stamp_ns = 0
    if stamp is not None:
        stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
    cells = cells.reshape((height, width)).copy()
    cells.setflags(write=False)
    return MapGrid(
        width=width,
        height=height,
        resolution=resolution,
        origin_x=float(message.info.origin.position.x),
        origin_y=float(message.info.origin.position.y),
        origin_yaw=yaw,
        cells=cells,
        stamp_ns=stamp_ns,
    )


def map_statistics(grid: MapGrid) -> dict:
    """Return progress indicators without claiming a false floor-plan total."""
    known = grid.cells >= 0
    free = (grid.cells >= 0) & (grid.cells <= 19)
    occupied = grid.cells >= 65
    cell_area = grid.resolution * grid.resolution
    return {
        "known_area_m2": round(float(np.count_nonzero(known)) * cell_area, 2),
        "free_area_m2": round(float(np.count_nonzero(free)) * cell_area, 2),
        "occupied_area_m2": round(
            float(np.count_nonzero(occupied)) * cell_area, 2
        ),
        "known_cells": int(np.count_nonzero(known)),
    }


# 아주 큰 군집끼리는 크기 차이가 의미 없으므로 여기서 잘라 비교한다.
FRONTIER_CELL_CAP = 200
# 1 m 이동을 이만큼의 미탐색 셀과 맞바꾼다. 200셀 상한과 함께 보면,
# 8 m 떨어진 최대 군집이 바로 옆 104셀 군집과 비슷한 값이 된다.
FRONTIER_DISTANCE_PENALTY_CELLS_PER_M = 12.0


def search_frontiers(
    grid: MapGrid,
    robot_xy: tuple[float, float] | None,
    *,
    minimum_cells: int = 8,
    minimum_clearance_m: float = 0.30,
    minimum_goal_distance_m: float = 0.45,
    blacklisted: tuple[tuple[float, float], ...] = (),
) -> FrontierSearch:
    """Find frontier approaches connected to the robot through known free space."""
    if robot_xy is None:
        return FrontierSearch([], 0)
    free = ((grid.cells >= 0) & (grid.cells <= 19)).astype(np.uint8)
    dx = robot_xy[0] - grid.origin_x
    dy = robot_xy[1] - grid.origin_y
    cosine, sine = math.cos(grid.origin_yaw), math.sin(grid.origin_yaw)
    column = math.floor((cosine * dx + sine * dy) / grid.resolution)
    row = math.floor((-sine * dx + cosine * dy) / grid.resolution)
    if not (0 <= row < grid.height and 0 <= column < grid.width and free[row, column]):
        raise ValueError('robot pose is outside known free map space')
    # Flood only through edge-connected free cells. Unknown space and diagonal
    # contact between obstacle corners do not establish a traversable connection.
    cv2.floodFill(free, None, (column, row), 2, flags=4)
    free = (free == 2).astype(np.uint8)
    unknown = (grid.cells < 0).astype(np.uint8)
    step = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    frontier_mask = free & cv2.dilate(unknown, step, iterations=1)
    count, labels, statistics, _centroids = cv2.connectedComponentsWithStats(
        frontier_mask, connectivity=8
    )
    clearance = (
        cv2.distanceTransform(free, cv2.DIST_L2, 5)
        * grid.resolution
    )
    candidates = []
    nearby_candidates = []
    frontier_count = 0
    approach_cells = max(1, int(math.ceil(0.60 / grid.resolution)))
    for label in range(1, count):
        cell_count = int(statistics[label, cv2.CC_STAT_AREA])
        if cell_count < minimum_cells:
            continue
        frontier_count += 1
        cluster = (labels == label).astype(np.uint8)
        approach = cluster
        # Limit every expansion step to free space, not only its endpoint.
        # A single geometric dilation can otherwise jump across a thin wall.
        for _ in range(approach_cells):
            approach = cv2.dilate(approach, step) & free
        safe_approach = (
            (approach > 0)
            & (free > 0)
            & (clearance >= minimum_clearance_m)
        )
        rows, columns = np.where(safe_approach)
        if rows.size == 0:
            continue
        points = np.asarray([grid.world(int(row), int(column)) for row, column in zip(
            rows, columns
        )])
        distances = np.hypot(points[:, 0] - robot_xy[0], points[:, 1] - robot_xy[1])
        eligible = np.ones(rows.shape, dtype=bool)
        for bx, by in blacklisted:
            eligible &= np.hypot(points[:, 0] - bx, points[:, 1] - by) >= 0.75
        if not np.any(eligible):
            continue
        distant = eligible & (distances >= minimum_goal_distance_m)
        nearby = not np.any(distant)
        if not nearby:
            eligible = distant
        eligible_indices = np.flatnonzero(eligible)
        # All candidates already meet clearance. Prefer the nearby useful end
        # of a long boundary instead of walking to its global centroid.
        order = np.lexsort((
            -clearance[rows[eligible], columns[eligible]], distances[eligible],
        ))
        selected = eligible_indices[int(order[0])]
        row = int(rows[selected])
        column = int(columns[selected])
        x, y = grid.world(row, column)
        # Find the nearest boundary through free space, then face an actual
        # adjacent unknown cell. Averaging a whole U-shaped boundary can point
        # toward a wall or already mapped space in the middle of the room.
        wave = np.zeros_like(free)
        wave[row, column] = 1
        for _ in range(approach_cells + 1):
            local_boundary = wave & cluster
            if np.any(local_boundary):
                break
            wave = cv2.dilate(wave, step) & free
        unknown_rows, unknown_columns = np.where(
            unknown & cv2.dilate(local_boundary, step))
        if unknown_rows.size:
            nearest = int(np.argmin(np.hypot(unknown_rows - row, unknown_columns - column)))
            unknown_x, unknown_y = grid.world(
                int(unknown_rows[nearest]), int(unknown_columns[nearest]))
            yaw = math.atan2(unknown_y - y, unknown_x - x)
        else:
            yaw = math.atan2(y - robot_xy[1], x - robot_xy[0])
        distance = float(distances[selected])
        destinations = nearby_candidates if nearby else candidates
        destinations.append(Frontier(
            x=x,
            y=y,
            yaw=yaw,
            cell_count=cell_count,
            clearance_m=float(clearance[row, column]),
            distance_m=distance,
        ))
    # 군집 크기만 1순위로 두면 거리는 동점일 때만 쓰이므로, 집 반대편의
    # 조금 더 큰 군집을 먼저 고르며 온 집을 횡단해 왕복한다. 크기와 이동
    # 비용을 한 점수로 합쳐 큰 공간을 선호하되 가까운 곳부터 정리한다.
    return FrontierSearch(sorted(
        candidates or nearby_candidates,
        key=lambda item: (
            -(
                min(item.cell_count, FRONTIER_CELL_CAP)
                - FRONTIER_DISTANCE_PENALTY_CELLS_PER_M * item.distance_m
            ),
            item.distance_m,
        ),
    ), frontier_count)


def find_frontiers(
    grid: MapGrid,
    robot_xy: tuple[float, float] | None,
    *,
    minimum_cells: int = 8,
    minimum_clearance_m: float = 0.30,
    minimum_goal_distance_m: float = 0.45,
    blacklisted: tuple[tuple[float, float], ...] = (),
) -> list[Frontier]:
    """Preserve the list-only API used by the older simulation web explorer."""
    return search_frontiers(
        grid, robot_xy, minimum_cells=minimum_cells,
        minimum_clearance_m=minimum_clearance_m,
        minimum_goal_distance_m=minimum_goal_distance_m,
        blacklisted=blacklisted,
    ).candidates


def path_is_known_free(grid: MapGrid, points_xy) -> bool:
    """Reject paths crossing unknown/occupied cells, including diagonal corner cuts."""
    try:
        points = np.asarray(tuple(points_xy), dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        return False
    if (points.ndim != 2 or points.shape[0] == 0 or points.shape[1] != 2
            or not np.all(np.isfinite(points))):
        return False
    dx, dy = points[:, 0] - grid.origin_x, points[:, 1] - grid.origin_y
    cosine, sine = math.cos(grid.origin_yaw), math.sin(grid.origin_yaw)
    coordinates = np.column_stack((
        (cosine * dx + sine * dy) / grid.resolution,
        (-sine * dx + cosine * dy) / grid.resolution,
    ))
    if (not np.all(np.isfinite(coordinates))
            or np.any(coordinates < 0)
            or np.any(coordinates[:, 0] >= grid.width)
            or np.any(coordinates[:, 1] >= grid.height)):
        return False
    free = (grid.cells >= 0) & (grid.cells <= 19)
    first = np.floor(coordinates[0]).astype(int)
    if not free[first[1], first[0]]:
        return False
    for start, end in zip(coordinates, coordinates[1:]):
        # Half-cell steps cannot skip a complete cell on either axis. Also
        # inspect both side cells at a diagonal transition between samples.
        steps = max(1, int(math.ceil(float(np.max(np.abs(end - start))) * 2.0)))
        cells = np.floor(np.linspace(start, end, steps + 1)).astype(int)
        columns, rows = cells[:, 0], cells[:, 1]
        if not np.all(free[rows, columns]):
            return False
        diagonal = (np.diff(rows) != 0) & (np.diff(columns) != 0)
        if (not np.all(free[rows[:-1][diagonal], columns[1:][diagonal]])
                or not np.all(free[rows[1:][diagonal], columns[:-1][diagonal]])):
            return False
    return True


def blocked_approach(points_xy, robot_xy, clearance_m):
    """Remember a suspected blocked approach ahead, not an obstacle at the robot."""
    points = np.asarray(points_xy, dtype=float)
    robot = np.asarray(robot_xy, dtype=float)
    if len(points) < 2:
        return None
    starts, vectors = points[:-1], np.diff(points, axis=0)
    lengths_squared = np.sum(vectors * vectors, axis=1)
    fractions = np.clip(np.sum((robot - starts) * vectors, axis=1)
                        / np.maximum(lengths_squared, 1e-12), 0.0, 1.0)
    projections = starts + fractions[:, None] * vectors
    nearest = int(np.argmin(np.sum((projections - robot) ** 2, axis=1)))
    center = projections[nearest]
    remaining = 2.0 * clearance_m
    for end in points[nearest + 1:]:
        vector = end - center
        length = float(np.linalg.norm(vector))
        if length >= remaining:
            center = center + vector * remaining / length
            break
        remaining -= length
        center = end
    distance = float(np.linalg.norm(center - robot))
    if distance < 0.05:
        return None  # Already at the endpoint: no defensible forward region.
    return (float(center[0]), float(center[1]), min(clearance_m, distance / 2.0))


def path_avoids_blocks(points_xy, blocks):
    """Check entire segments against run-local exclusion disks, even on sparse paths."""
    if not blocks:
        return True
    points = np.asarray(points_xy, dtype=float)
    starts = points[:-1] if len(points) > 1 else points
    vectors = np.diff(points, axis=0) if len(points) > 1 else np.zeros_like(points)
    lengths_squared = np.sum(vectors * vectors, axis=1)
    for x, y, radius in blocks:
        center = np.asarray((x, y))
        fractions = np.clip(np.sum((center - starts) * vectors, axis=1)
                            / np.maximum(lengths_squared, 1e-12), 0.0, 1.0)
        closest = starts + fractions[:, None] * vectors
        if np.any(np.sum((closest - center) ** 2, axis=1) <= radius * radius):
            return False
    return True
