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


def find_frontiers(
    grid: MapGrid,
    robot_xy: tuple[float, float] | None,
    *,
    minimum_cells: int = 8,
    minimum_clearance_m: float = 0.30,
    minimum_goal_distance_m: float = 0.45,
    blacklisted: tuple[tuple[float, float], ...] = (),
) -> list[Frontier]:
    """Find safe, connected frontier clusters sorted by utility."""
    free = ((grid.cells >= 0) & (grid.cells <= 19)).astype(np.uint8)
    unknown = (grid.cells < 0).astype(np.uint8)
    neighborhood = np.ones((3, 3), dtype=np.uint8)
    frontier_mask = free & cv2.dilate(unknown, neighborhood, iterations=1)
    count, labels, statistics, _centroids = cv2.connectedComponentsWithStats(
        frontier_mask, connectivity=8
    )
    clearance = (
        cv2.distanceTransform(free, cv2.DIST_L2, 5)
        * grid.resolution
    )
    candidates = []
    for label in range(1, count):
        cell_count = int(statistics[label, cv2.CC_STAT_AREA])
        if cell_count < minimum_cells:
            continue
        cluster = (labels == label).astype(np.uint8)
        approach_cells = max(
            1, int(math.ceil(0.60 / grid.resolution))
        )
        approach = cv2.dilate(
            cluster,
            cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE,
                (approach_cells * 2 + 1, approach_cells * 2 + 1),
            ),
            iterations=1,
        )
        safe_approach = (
            (approach > 0)
            & (free > 0)
            & (clearance >= minimum_clearance_m)
        )
        rows, columns = np.where(safe_approach)
        if rows.size == 0:
            continue
        if robot_xy is None:
            distances = np.zeros(rows.shape, dtype=np.float64)
        else:
            points = [grid.world(int(row), int(column)) for row, column in zip(
                rows, columns
            )]
            distances = np.asarray([
                math.hypot(x - robot_xy[0], y - robot_xy[1])
                for x, y in points
            ])
        eligible = distances >= minimum_goal_distance_m
        if not np.any(eligible):
            continue
        eligible_indices = np.flatnonzero(eligible)
        cluster_rows, cluster_columns = np.where(cluster)
        center_row = float(np.mean(cluster_rows))
        center_column = float(np.mean(cluster_columns))
        frontier_distance_cells = np.hypot(
            rows[eligible] - center_row,
            columns[eligible] - center_column,
        )
        # Stay inside known space while remaining close enough to observe it.
        utility = (
            clearance[rows[eligible], columns[eligible]]
            - 0.05 * distances[eligible]
            - 0.02 * frontier_distance_cells
        )
        selected = eligible_indices[int(np.argmax(utility))]
        row = int(rows[selected])
        column = int(columns[selected])
        x, y = grid.world(row, column)
        if any(math.hypot(x - bx, y - by) < 0.75 for bx, by in blacklisted):
            continue
        unknown_rows, unknown_columns = np.where(
            unknown
            & cv2.dilate(
                (labels == label).astype(np.uint8),
                neighborhood,
                iterations=1,
            )
        )
        if unknown_rows.size:
            unknown_x, unknown_y = grid.world(
                int(round(float(np.mean(unknown_rows)))),
                int(round(float(np.mean(unknown_columns)))),
            )
            yaw = math.atan2(unknown_y - y, unknown_x - x)
        elif robot_xy is not None:
            yaw = math.atan2(y - robot_xy[1], x - robot_xy[0])
        else:
            yaw = 0.0
        distance = float(distances[selected]) if robot_xy is not None else 0.0
        candidates.append(Frontier(
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
    return sorted(
        candidates,
        key=lambda item: (
            -(
                min(item.cell_count, FRONTIER_CELL_CAP)
                - FRONTIER_DISTANCE_PENALTY_CELLS_PER_M * item.distance_m
            ),
            item.distance_m,
        ),
    )
