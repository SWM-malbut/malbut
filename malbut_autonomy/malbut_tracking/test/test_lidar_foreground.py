"""Tests for cached static subtraction and ordered LaserScan clustering."""

import math

import pytest

from malbut_tracking.costmap_tracking import CostmapGrid, ObstacleCluster
from malbut_tracking.geometry import Point2D
from malbut_tracking.lidar_foreground import (
    ScanTransform2D,
    StaticDistanceField,
    camera_consistent_clusters,
    extract_foreground_clusters,
)


def _static_grid(*, wall_x=10, unknown_cells=()):
    width = 20
    height = 20
    costs = [0] * (width * height)
    for cell_y in range(height):
        costs[cell_y * width + wall_x] = 100
    for cell_x, cell_y in unknown_cells:
        costs[cell_y * width + cell_x] = -1
    return CostmapGrid(
        frame_id='map',
        stamp_seconds=1.0,
        resolution=0.1,
        width=width,
        height=height,
        origin=Point2D(0.0, 0.0),
        origin_yaw=0.0,
        costs=tuple(costs),
    )


def _clusters(ranges, field, **overrides):
    parameters = {
        'angle_min': 0.0,
        'angle_increment': 0.02,
        'range_min': 0.05,
        'range_max': 5.0,
        'transform': ScanTransform2D(Point2D(0.05, 1.05), 0.0),
        'static_field': field,
        'static_exclusion_radius_m': 0.20,
        'cluster_gap_m': 0.20,
        'minimum_cluster_points': 1,
        'minimum_cluster_density_points_per_m': 1.0,
        'maximum_cluster_points': 20,
        'maximum_cluster_extent_m': 0.80,
    }
    parameters.update(overrides)
    return extract_foreground_clusters(ranges=ranges, **parameters)


def test_static_distance_field_is_precomputed_and_constant_time_to_query():
    """One cached lookup returns clearance from saved map geometry."""
    field = StaticDistanceField.build(_static_grid(), 65)

    assert field.distance_at(Point2D(1.05, 1.05)) == pytest.approx(0.0)
    assert field.distance_at(Point2D(0.55, 1.05)) == pytest.approx(
        0.5,
        abs=0.02,
    )


def test_scan_subtraction_keeps_foreground_and_removes_saved_wall_returns():
    """A scan endpoint on the wall is removed without reading a costmap."""
    field = StaticDistanceField.build(_static_grid(), 65)

    foreground = _clusters([0.50], field)
    wall = _clusters([1.00], field)

    assert len(foreground) == 1
    assert foreground[0].position.x == pytest.approx(0.55)
    assert wall == []


def test_scan_order_clusters_adjacent_returns_and_splits_invalid_gaps():
    """Laser angular order replaces full-grid flood-fill clustering."""
    field = StaticDistanceField.build(_static_grid(wall_x=19), 65)

    clusters = _clusters([0.50, 0.52, math.inf, 1.20], field)

    assert len(clusters) == 2
    assert sorted(cluster.point_count for cluster in clusters) == [1, 2]


def test_scan_endpoint_in_unknown_map_space_is_not_a_dynamic_candidate():
    """Unmapped space cannot create a LiDAR continuation target."""
    field = StaticDistanceField.build(
        _static_grid(wall_x=19, unknown_cells={(5, 10)}),
        65,
    )

    assert _clusters([0.50], field) == []


def test_single_scan_return_is_not_promoted_to_an_object_cluster():
    """One map residual cannot become a moving object by itself."""
    field = StaticDistanceField.build(_static_grid(wall_x=19), 65)

    assert _clusters(
        [0.50],
        field,
        minimum_cluster_points=3,
    ) == []


def test_sparse_wide_group_fails_the_physical_density_check():
    """Several isolated residuals spread over a contour are not one object."""
    field = StaticDistanceField.build(_static_grid(wall_x=19), 65)

    assert _clusters(
        [0.50, 0.70, 0.90],
        field,
        cluster_gap_m=0.25,
        minimum_cluster_points=3,
        minimum_cluster_density_points_per_m=10.0,
    ) == []


def test_scan_pose_is_interpolated_across_a_moving_acquisition():
    """Each ray uses its acquisition pose instead of one latest TF."""
    field = StaticDistanceField.build(_static_grid(wall_x=19), 65)
    clusters = _clusters(
        [0.20, 0.20],
        field,
        transform=ScanTransform2D(
            Point2D(0.0, 1.0),
            0.0,
            Point2D(0.40, 1.0),
            0.0,
        ),
        cluster_gap_m=1.0,
    )

    assert len(clusters) == 1
    assert clusters[0].position.x == pytest.approx(0.40, abs=1e-3)


def test_fresh_camera_rejects_neighboring_lidar_geometry():
    """Fresh RGB-D keeps the person cluster and excludes nearby furniture."""
    person = ObstacleCluster(Point2D(1.48, 1.0), 8, 0.20)
    furniture = ObstacleCluster(Point2D(1.70, 1.0), 20, 0.50)

    selected = camera_consistent_clusters(
        [person, furniture],
        Point2D(1.0, 1.0),
        camera_gate_m=0.40,
        maximum_extent_padding_m=0.15,
    )

    assert selected == [person]


def test_camera_extent_allowance_is_bounded_for_wide_clusters():
    """A broad segment cannot win association through unlimited padding."""
    broad_furniture = ObstacleCluster(Point2D(1.56, 1.0), 40, 0.80)

    assert camera_consistent_clusters(
        [broad_furniture],
        Point2D(1.0, 1.0),
        camera_gate_m=0.40,
        maximum_extent_padding_m=0.15,
    ) == []
