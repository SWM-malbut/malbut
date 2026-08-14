"""Tests for dynamic-grid extraction and statistically gated tracking."""

import math

import pytest

from malbut_tracking.costmap_tracking import (
    CostmapGrid,
    CostmapTargetTracker,
    find_obstacle_clusters,
)
from malbut_tracking.geometry import Point2D


def _grid(lethal_cells, other_costs=None, stamp=1.0):
    width = 40
    height = 30
    costs = [0] * (width * height)
    for cell_x, cell_y in lethal_cells:
        costs[cell_y * width + cell_x] = 254
    for cell_x, cell_y, value in other_costs or []:
        costs[cell_y * width + cell_x] = value
    return CostmapGrid(
        frame_id='map',
        stamp_seconds=stamp,
        resolution=0.1,
        width=width,
        height=height,
        origin=Point2D(0.0, 0.0),
        origin_yaw=0.0,
        costs=tuple(costs),
    )


def _static_grid(occupied_cells):
    grid = _grid(set())
    costs = list(grid.costs)
    for cell_x, cell_y in occupied_cells:
        costs[cell_y * grid.width + cell_x] = 100
    return CostmapGrid(
        frame_id=grid.frame_id,
        stamp_seconds=grid.stamp_seconds,
        resolution=grid.resolution,
        width=grid.width,
        height=grid.height,
        origin=grid.origin,
        origin_yaw=grid.origin_yaw,
        costs=tuple(costs),
    )


def _tracker(**overrides):
    parameters = {
        'cluster_radius_m': 0.10,
        'obstacle_cost_threshold': 254,
        'minimum_cluster_cells': 1,
        'maximum_cluster_cells': 40,
        'maximum_cluster_extent_m': 0.8,
        'static_exclusion_radius_m': 0.0,
        'process_variance': 0.5,
        'measurement_variance': 0.01,
        'mahalanobis_gate': 9.21,
        'confirmation_hits': 3,
        'maximum_missed_updates': 3,
        'maximum_coast_time_s': 3.0,
        'camera_label_gate_m': 0.5,
    }
    parameters.update(overrides)
    return CostmapTargetTracker(**parameters)


def _step(tracker, cells, stamp, static_map=None):
    return tracker.update(
        _grid(cells, stamp=stamp),
        static_map or _static_grid(set()),
    )


def test_dynamic_extraction_excludes_inflation_and_saved_geometry():
    """Only lethal cells absent from the saved map become measurements."""
    clusters = find_obstacle_clusters(
        _grid({(5, 5), (5, 6), (6, 5), (6, 6), (20, 20)},
              other_costs=[(7, 5, 253)]),
        obstacle_cost_threshold=254,
        cluster_radius_m=0.15,
        minimum_cluster_cells=1,
        maximum_cluster_cells=40,
        maximum_cluster_extent_m=0.8,
        static_map=_static_grid({(20, 20)}),
        static_exclusion_radius_m=0.0,
    )
    assert len(clusters) == 1
    assert clusters[0].cell_count == 4
    assert clusters[0].position.x == pytest.approx(0.6)
    assert clusters[0].position.y == pytest.approx(0.6)


def test_dynamic_extraction_never_treats_unknown_space_as_obstacle():
    """Nav2 value 255 is NO_INFORMATION, not a lethal measurement."""
    clusters = find_obstacle_clusters(
        _grid({(5, 5)}, other_costs=[(10, 10, 255), (11, 10, 255)]),
        obstacle_cost_threshold=254,
        cluster_radius_m=0.1,
        minimum_cluster_cells=1,
        maximum_cluster_cells=40,
        maximum_cluster_extent_m=0.8,
    )
    assert len(clusters) == 1
    assert clusters[0].cell_count == 1
    assert clusters[0].position == Point2D(0.55, 0.55)


def test_static_exclusion_margin_rejects_localization_edge_noise():
    """A lethal cell beside a mapped wall is not a dynamic object."""
    clusters = find_obstacle_clusters(
        _grid({(11, 10)}),
        obstacle_cost_threshold=254,
        cluster_radius_m=0.1,
        minimum_cluster_cells=1,
        maximum_cluster_cells=40,
        maximum_cluster_extent_m=0.8,
        static_map=_static_grid({(10, 10)}),
        static_exclusion_radius_m=0.11,
    )
    assert clusters == []


def test_person_label_requires_confirmed_repeated_costmap_track():
    """A one-frame costmap artifact may be labeled but never drive motion."""
    tracker = _tracker()
    _step(tracker, {(5, 5)}, 1.0)
    target = tracker.bind('person', Point2D(0.55, 0.55), 'detector-4')
    assert target is not None
    assert not target.track.confirmed
    _step(tracker, {(6, 5)}, 2.0)
    observed = _step(tracker, {(7, 5)}, 3.0)
    assert observed is not None
    assert observed.track.confirmed
    assert observed.track.track_id == target.track.track_id


def test_label_coasts_through_short_occlusion_then_reacquires():
    """Prediction preserves identity while measurements briefly disappear."""
    tracker = _tracker(confirmation_hits=2)
    _step(tracker, {(5, 5)}, 1.0)
    _step(tracker, {(7, 5)}, 2.0)
    target = tracker.bind('person', Point2D(0.75, 0.55), 'detector-2')
    assert target is not None and target.track.confirmed
    assert _step(tracker, set(), 3.0) is None
    coasting = tracker.target
    assert coasting is not None
    assert coasting.track.misses == 1
    reacquired = _step(tracker, {(11, 5)}, 4.0)
    assert reacquired is not None
    assert reacquired.track.track_id == target.track.track_id
    assert reacquired.track.misses == 0


def test_two_crossing_obstacles_keep_motion_consistent_identities():
    """Hungarian assignment and velocity prediction prevent nearest swaps."""
    tracker = _tracker(
        confirmation_hits=2,
        measurement_variance=0.0025,
        process_variance=0.2,
    )
    _step(tracker, {(5, 5), (25, 5)}, 1.0)
    _step(tracker, {(9, 5), (21, 5)}, 2.0)
    target = tracker.bind('person', Point2D(0.95, 0.55), 'detector-8')
    assert target is not None
    target_id = target.track.track_id
    _step(tracker, {(13, 5), (17, 5)}, 3.0)
    observed = _step(tracker, {(17, 5), (13, 5)}, 4.0)
    assert observed is not None
    assert observed.track.track_id == target_id
    assert observed.track.position.x > 1.5
    assert observed.track.velocity.x > 0.0


def test_tracker_never_jumps_to_distant_unrelated_obstacle():
    """A gated track coasts instead of transferring its person label."""
    tracker = _tracker(confirmation_hits=1)
    _step(tracker, {(5, 5)}, 1.0)
    target = tracker.bind('person', Point2D(0.55, 0.55), 'detector-1')
    assert target is not None
    assert _step(tracker, {(35, 25)}, 2.0) is None
    assert tracker.target is not None
    assert tracker.target.track.track_id == target.track.track_id
    assert tracker.target.track.misses == 1


def test_camera_rebinds_label_when_person_enters_a_new_costmap_track():
    """A far camera target may gain precise costmap tracking when nearer."""
    tracker = _tracker(confirmation_hits=1)
    _step(tracker, {(5, 5)}, 1.0)
    first = tracker.bind('person', Point2D(0.55, 0.55), 'detector-1')
    assert first is not None
    _step(tracker, {(5, 5), (25, 5)}, 2.0)
    rebound = tracker.bind('person', Point2D(2.55, 0.55), 'detector-1')
    assert rebound is not None
    assert rebound.track.track_id != first.track.track_id
    assert rebound.track.position.x == pytest.approx(2.55)


def test_large_unmapped_wall_component_is_not_an_object_track():
    """Long geometry absent from the saved map is still rejected by shape."""
    wall = {(10, cell_y) for cell_y in range(4, 17)}
    clusters = find_obstacle_clusters(
        _grid(wall),
        obstacle_cost_threshold=254,
        cluster_radius_m=0.1,
        minimum_cluster_cells=1,
        maximum_cluster_cells=40,
        maximum_cluster_extent_m=0.8,
    )
    assert clusters == []


def test_rotated_costmap_coordinates_preserve_world_position():
    """Grid origins with yaw must convert cell centers in the map frame."""
    costs = [0] * 4
    costs[0] = 254
    grid = CostmapGrid(
        frame_id='map',
        stamp_seconds=1.0,
        resolution=1.0,
        width=2,
        height=2,
        origin=Point2D(2.0, 3.0),
        origin_yaw=math.pi / 2.0,
        costs=tuple(costs),
    )
    center = grid.cell_center(0, 0)
    assert center.x == pytest.approx(1.5)
    assert center.y == pytest.approx(3.5)
    assert grid.world_to_cell(center) == (0, 0)
