"""Extract compact foreground obstacles from a map-aligned 2-D LiDAR scan."""

from dataclasses import dataclass
import math
from typing import Sequence

import cv2
import numpy as np

from .costmap_tracking import CostmapGrid, ObstacleCluster
from .geometry import Point2D, distance


@dataclass(frozen=True)
class ScanTransform2D:
    """Planar scan transform, optionally spanning one moving acquisition."""

    translation: Point2D
    yaw: float
    end_translation: Point2D | None = None
    end_yaw: float | None = None

    def point(
        self,
        range_m: float,
        angle_rad: float,
        scan_fraction: float = 0.0,
    ) -> Point2D:
        """Transform one ray, interpolating ego motion across the scan."""
        fraction = min(1.0, max(0.0, scan_fraction))
        end_translation = self.end_translation or self.translation
        translation = Point2D(
            self.translation.x
            + (end_translation.x - self.translation.x) * fraction,
            self.translation.y
            + (end_translation.y - self.translation.y) * fraction,
        )
        end_yaw = self.yaw if self.end_yaw is None else self.end_yaw
        yaw_delta = math.atan2(
            math.sin(end_yaw - self.yaw),
            math.cos(end_yaw - self.yaw),
        )
        yaw = self.yaw + yaw_delta * fraction
        local_x = range_m * math.cos(angle_rad)
        local_y = range_m * math.sin(angle_rad)
        cosine = math.cos(yaw)
        sine = math.sin(yaw)
        return Point2D(
            translation.x + cosine * local_x - sine * local_y,
            translation.y + sine * local_x + cosine * local_y,
        )


@dataclass(frozen=True)
class StaticDistanceField:
    """Cached distance from each known map cell to saved static geometry."""

    grid: CostmapGrid
    known: np.ndarray
    distances_m: np.ndarray

    @classmethod
    def build(
        cls,
        grid: CostmapGrid,
        occupied_threshold: int,
    ) -> 'StaticDistanceField':
        """Build the invariant distance field once for one static map."""
        grid.validate()
        if not 0 <= occupied_threshold <= 100:
            raise ValueError('static occupied threshold must be in [0, 100]')
        values = np.asarray(grid.costs, dtype=np.int16).reshape(
            grid.height,
            grid.width,
        )
        known = values >= 0
        occupied = known & (values >= occupied_threshold)
        if np.any(occupied):
            distances = cv2.distanceTransform(
                (~occupied).astype(np.uint8),
                cv2.DIST_L2,
                5,
            ).astype(np.float32)
            distances *= grid.resolution
        else:
            distances = np.full(values.shape, math.inf, dtype=np.float32)
        return cls(grid, known, distances)

    def distance_at(self, point: Point2D) -> float | None:
        """Return cached static clearance, rejecting unknown map space."""
        cell = self.grid.world_to_cell(point)
        if cell is None:
            return None
        cell_x, cell_y = cell
        if not bool(self.known[cell_y, cell_x]):
            return None
        return float(self.distances_m[cell_y, cell_x])


def extract_foreground_clusters(
    ranges: Sequence[float],
    angle_min: float,
    angle_increment: float,
    range_min: float,
    range_max: float,
    transform: ScanTransform2D,
    static_field: StaticDistanceField,
    static_exclusion_radius_m: float,
    cluster_gap_m: float,
    minimum_cluster_points: int,
    minimum_cluster_density_points_per_m: float,
    maximum_cluster_points: int,
    maximum_cluster_extent_m: float,
) -> list[ObstacleCluster]:
    """Subtract static geometry and cluster adjacent foreground scan returns."""
    _validate_parameters(
        angle_increment,
        range_min,
        range_max,
        static_exclusion_radius_m,
        cluster_gap_m,
        minimum_cluster_points,
        minimum_cluster_density_points_per_m,
        maximum_cluster_points,
        maximum_cluster_extent_m,
    )
    groups: list[list[Point2D]] = []
    current: list[Point2D] = []

    def finish_group() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for index, raw_range in enumerate(ranges):
        range_m = float(raw_range)
        if (
            not math.isfinite(range_m)
            or range_m < range_min
            or range_m > range_max
        ):
            finish_group()
            continue
        point = transform.point(
            range_m,
            angle_min + index * angle_increment,
            index / max(1, len(ranges) - 1),
        )
        clearance = static_field.distance_at(point)
        if clearance is None or clearance <= static_exclusion_radius_m:
            finish_group()
            continue
        if current and distance(current[-1], point) > cluster_gap_m:
            finish_group()
        current.append(point)
    finish_group()

    covered_angle = abs(angle_increment) * max(0, len(ranges) - 1)
    if (
        len(groups) > 1
        and covered_angle >= math.tau - 2.0 * abs(angle_increment)
        and distance(groups[-1][-1], groups[0][0]) <= cluster_gap_m
    ):
        groups[0] = groups[-1] + groups[0]
        groups.pop()

    clusters = [
        cluster
        for group in groups
        if (
            cluster := _make_cluster(
                group,
                minimum_cluster_points,
                minimum_cluster_density_points_per_m,
                maximum_cluster_points,
                maximum_cluster_extent_m,
            )
        ) is not None
    ]
    clusters.sort(key=lambda cluster: (cluster.position.x, cluster.position.y))
    return clusters


def camera_consistent_clusters(
    clusters: Sequence[ObstacleCluster],
    camera_position: Point2D,
    camera_gate_m: float,
    maximum_extent_padding_m: float,
) -> list[ObstacleCluster]:
    """
    Keep LiDAR clusters compatible with one fresh RGB-D observation.

    Half of a cluster's measured extent is admitted as centroid uncertainty,
    but the padding is bounded so nearby furniture cannot remain a person
    candidate merely because it belongs to a wide LiDAR segment.
    """
    if camera_gate_m <= 0.0:
        raise ValueError('camera gate must be positive')
    if maximum_extent_padding_m < 0.0:
        raise ValueError('camera extent padding must be non-negative')
    return [
        cluster
        for cluster in clusters
        if distance(cluster.position, camera_position)
        <= camera_gate_m
        + min(maximum_extent_padding_m, 0.5 * cluster.extent_m)
    ]


def _make_cluster(
    points: list[Point2D],
    minimum_points: int,
    minimum_density_points_per_m: float,
    maximum_points: int,
    maximum_extent_m: float,
) -> ObstacleCluster | None:
    count = len(points)
    if not minimum_points <= count <= maximum_points:
        return None
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    extent = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    if extent > maximum_extent_m:
        return None
    density = count / max(extent, 0.05)
    if density < minimum_density_points_per_m:
        return None
    return ObstacleCluster(
        Point2D(sum(xs) / count, sum(ys) / count),
        count,
        extent,
    )


def _validate_parameters(
    angle_increment: float,
    range_min: float,
    range_max: float,
    static_exclusion_radius_m: float,
    cluster_gap_m: float,
    minimum_cluster_points: int,
    minimum_cluster_density_points_per_m: float,
    maximum_cluster_points: int,
    maximum_cluster_extent_m: float,
) -> None:
    if angle_increment == 0.0 or not math.isfinite(angle_increment):
        raise ValueError('scan angle increment must be finite and non-zero')
    if range_min < 0.0 or range_max <= range_min:
        raise ValueError('scan range bounds are invalid')
    if static_exclusion_radius_m < 0.0:
        raise ValueError('static exclusion radius must be non-negative')
    if cluster_gap_m <= 0.0:
        raise ValueError('cluster gap must be positive')
    if minimum_cluster_points <= 0:
        raise ValueError('minimum cluster points must be positive')
    if minimum_cluster_density_points_per_m <= 0.0:
        raise ValueError('minimum cluster density must be positive')
    if maximum_cluster_points < minimum_cluster_points:
        raise ValueError('maximum cluster points must cover the minimum')
    if maximum_cluster_extent_m <= 0.0:
        raise ValueError('maximum cluster extent must be positive')
