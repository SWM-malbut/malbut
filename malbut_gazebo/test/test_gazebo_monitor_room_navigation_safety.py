"""Adversarial tests for the pure monitor-room path-safety core."""

from dataclasses import FrozenInstanceError
import inspect
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from malbut_gazebo.gazebo_monitor_room_navigation_safety import (
    MAX_GRID_CELLS,
    MapCostGrid,
    NavigationSafetyInputError,
    PathPoint,
    PathSafetyFailure,
    PathSafetyFailureCode,
    PathSafetyProof,
    REQUIRED_PATH_CLEARANCE_M,
    RestrictedZones,
    SERVER_SAFETY_PROFILE,
    SamplePath,
    ServerSafetyProfile,
    StaticClearanceGrid,
    validate_sample_path,
)


_DIGESTS = {
    'target_binding_digest': '1' * 64,
    'operation_binding_digest': '2' * 64,
    'map_content_digest': '3' * 64,
    'semantic_content_digest': '4' * 64,
}


def _inputs(
    *,
    costs=None,
    clearances=None,
    resolution=0.5,
    width=5,
    height=3,
    origin_x=0.0,
    origin_y=0.0,
    points=None,
    start=None,
    target=None,
    restricted_zones=None,
    zones_digest=None,
):
    cell_count = width * height
    cost_values = [0] * cell_count if costs is None else costs
    clearance_values = (
        [0.5] * cell_count if clearances is None else clearances
    )
    path_points = points or [PathPoint(0.25, 0.75), PathPoint(2.25, 0.75)]
    zone_value = restricted_zones or RestrictedZones('map', [])
    return {
        'start_point': start or PathPoint(0.25, 0.75),
        'target_point': target or PathPoint(2.25, 0.75),
        **_DIGESTS,
        'zones_digest': (
            zone_value.digest if zones_digest is None else zones_digest
        ),
        'restricted_zones': zone_value,
        'costmap': MapCostGrid(
            'map',
            width,
            height,
            resolution,
            origin_x,
            origin_y,
            0.0,
            cost_values,
        ),
        'static_clearance': StaticClearanceGrid(
            'map',
            width,
            height,
            resolution,
            origin_x,
            origin_y,
            0.0,
            clearance_values,
        ),
        'path': SamplePath('map', path_points),
    }


def _polygon(outer, *holes):
    return {
        'type': 'Polygon',
        'coordinates': [outer, *holes],
    }


def _rectangle(min_x, min_y, max_x, max_y):
    return [
        [min_x, min_y],
        [max_x, min_y],
        [max_x, max_y],
        [min_x, max_y],
        [min_x, min_y],
    ]


def test_safe_path_returns_bound_coordinate_free_non_authority_proof():
    """A valid path yields only a narrowly scoped non-authority proof."""
    inputs = _inputs()
    result = validate_sample_path(**inputs)

    assert type(result) is PathSafetyProof
    assert result.scope == 'single_planner_path_preflight'
    assert result.profile_digest == SERVER_SAFETY_PROFILE.digest
    assert result.operation_binding_digest == '2' * 64
    assert result.target_binding_digest == '1' * 64
    assert result.map_content_digest == '3' * 64
    assert result.semantic_content_digest == '4' * 64
    assert result.zones_digest == inputs['restricted_zones'].digest
    assert result.costmap_digest == inputs['costmap'].digest
    assert result.path_digest == inputs['path'].digest
    assert result.maximum_cost == 0
    assert result.minimum_clearance_m == 0.5
    assert result.sampled_point_count == 9
    assert result.restricted_zone_validation_performed is True
    assert result.authority_claimed is False
    assert result.coverage_claimed is False
    assert result.physical_execution_observed is False
    assert result.viewer_observed is False
    assert '0.25' not in repr(result)
    assert '2.25' not in repr(result)
    with pytest.raises(TypeError):
        vars(result)


def test_restricted_zones_are_strict_detached_and_redacted():
    """Zone DTOs detach canonical geometry and expose only safe metadata."""
    outer = _rectangle(3.0, 0.5, 3.5, 1.0)
    source = [_polygon(outer)]
    zones = RestrictedZones('map', source)
    digest = zones.digest

    outer[0][0] = 999.0
    source.clear()
    assert zones.digest == digest
    assert zones.geometry_count == 1
    assert zones.polygon_count == 1
    assert zones.boundary_segment_count == 4
    assert repr(zones) == 'RestrictedZones(<redacted>)'
    assert '3.0' not in repr(zones)
    with pytest.raises(TypeError):
        vars(zones)
    with pytest.raises(FrozenInstanceError):
        zones._digest = 'f' * 64


def test_empty_zones_are_actually_validated_before_proof():
    """An empty canonical set is a real validation, not an omitted check."""
    inputs = _inputs()
    result = validate_sample_path(**inputs)
    assert type(result) is PathSafetyProof
    assert result.restricted_zone_validation_performed is True
    assert result.zones_digest == inputs['restricted_zones'].digest
    assert result.zone_boundary_segment_count == 0
    assert result.zone_validation_candidate_count == 0
    assert len(result.zone_validation_digest) == 64


def test_sparse_path_cannot_tunnel_through_a_narrow_restricted_zone():
    """Exact segment intersection catches zones between sparse poses."""
    zones = RestrictedZones(
        'map', [_polygon(_rectangle(1.10, 0.60, 1.15, 0.90))]
    )
    result = validate_sample_path(
        **_inputs(restricted_zones=zones)
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT
    )


def test_endpoint_gap_connectors_cannot_tunnel_through_zones():
    """The allowed endpoint gap does not exempt its connector segments."""
    start_zone = RestrictedZones(
        'map', [_polygon(_rectangle(0.22, 0.70, 0.23, 0.80))]
    )
    result = validate_sample_path(
        **_inputs(
            restricted_zones=start_zone,
            start=PathPoint(0.20, 0.75),
        )
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT
    )

    target_zone = RestrictedZones(
        'map', [_polygon(_rectangle(2.27, 0.70, 2.28, 0.80))]
    )
    result = validate_sample_path(
        **_inputs(
            restricted_zones=target_zone,
            target=PathPoint(2.30, 0.75),
        )
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT
    )


def test_path_boundary_crossing_touching_and_vertex_contact_fail_closed():
    """Crossing, tangency, overlap, and vertex contact are all forbidden."""
    crossing = RestrictedZones(
        'map', [_polygon(_rectangle(1.0, 0.5, 1.5, 1.0))]
    )
    result = validate_sample_path(
        **_inputs(
            restricted_zones=crossing,
            points=[PathPoint(0.25, 0.74), PathPoint(2.25, 0.74)],
            start=PathPoint(0.25, 0.74),
            target=PathPoint(2.25, 0.74),
        )
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT
    )

    touching = RestrictedZones(
        'map', [_polygon(_rectangle(1.10, 0.75, 1.15, 1.0))]
    )
    result = validate_sample_path(
        **_inputs(restricted_zones=touching)
    )
    assert result.code in {
        PathSafetyFailureCode.PATH_RESTRICTED_ZONE,
        PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT,
    }

    vertex = RestrictedZones(
        'map', [
            _polygon([
                [1.0, 0.75],
                [1.25, 1.0],
                [0.75, 1.0],
                [1.0, 0.75],
            ])
        ]
    )
    result = validate_sample_path(
        **_inputs(
            restricted_zones=vertex,
            points=[PathPoint(0.25, 0.50), PathPoint(1.75, 1.50)],
            start=PathPoint(0.25, 0.50),
            target=PathPoint(1.75, 1.50),
        )
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT
    )


def test_concave_polygon_classification_does_not_fill_its_notch():
    """A concave notch remains safe while polygon body and edge reject."""
    concave = RestrictedZones(
        'map', [
            _polygon([
                [1.0, 1.0],
                [3.0, 1.0],
                [3.0, 2.0],
                [2.0, 2.0],
                [2.0, 3.0],
                [1.0, 3.0],
                [1.0, 1.0],
            ])
        ]
    )
    safe = validate_sample_path(
        **_inputs(
            width=8,
            height=8,
            restricted_zones=concave,
            points=[PathPoint(2.5, 2.5)],
            start=PathPoint(2.5, 2.5),
            target=PathPoint(2.5, 2.5),
        )
    )
    assert type(safe) is PathSafetyProof

    for point in (PathPoint(1.5, 2.5), PathPoint(2.0, 2.5)):
        result = validate_sample_path(
            **_inputs(
                width=8,
                height=8,
                restricted_zones=concave,
                points=[point],
                start=point,
                target=point,
            )
        )
        assert result == PathSafetyFailure(
            PathSafetyFailureCode.PATH_RESTRICTED_ZONE
        )


def test_polygon_hole_interior_is_safe_but_every_hole_edge_is_forbidden():
    """Hole interiors remain free while entering or touching them rejects."""
    zones = RestrictedZones(
        'map', [
            _polygon(
                _rectangle(0.5, 0.5, 3.5, 3.5),
                _rectangle(1.0, 1.0, 3.0, 3.0),
            )
        ]
    )
    safe = validate_sample_path(
        **_inputs(
            width=8,
            height=8,
            restricted_zones=zones,
            points=[PathPoint(1.5, 2.0), PathPoint(2.5, 2.0)],
            start=PathPoint(1.5, 2.0),
            target=PathPoint(2.5, 2.0),
        )
    )
    assert type(safe) is PathSafetyProof

    for point in (PathPoint(0.75, 2.0), PathPoint(1.0, 2.0)):
        result = validate_sample_path(
            **_inputs(
                width=8,
                height=8,
                restricted_zones=zones,
                points=[point],
                start=point,
                target=point,
            )
        )
        assert result == PathSafetyFailure(
            PathSafetyFailureCode.PATH_RESTRICTED_ZONE
        )

    crossing = validate_sample_path(
        **_inputs(
            width=8,
            height=8,
            restricted_zones=zones,
            points=[PathPoint(0.25, 2.0), PathPoint(1.5, 2.0)],
            start=PathPoint(0.25, 2.0),
            target=PathPoint(1.5, 2.0),
        )
    )
    assert crossing == PathSafetyFailure(
        PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT
    )


def test_multipolygon_checks_every_component():
    """A path cannot cross a later MultiPolygon component."""
    zones = RestrictedZones(
        'map', [
            {
                'type': 'MultiPolygon',
                'coordinates': [
                    [_rectangle(3.0, 2.0, 3.5, 2.5)],
                    [_rectangle(1.10, 0.60, 1.15, 0.90)],
                ],
            }
        ]
    )
    assert zones.polygon_count == 2
    result = validate_sample_path(
        **_inputs(restricted_zones=zones)
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT
    )


def test_world_cell_contract_is_lower_inclusive_upper_exclusive():
    """World and cell conversions preserve the existing map convention."""
    grid = MapCostGrid(
        'map', 3, 2, 0.5, -1.0, 2.0, 0.0, [0] * 6
    )
    assert grid.world_to_cell(-1.0, 2.0) == (0, 0)
    assert grid.world_to_cell(-0.75, 2.25) == (0, 0)
    assert grid.world_to_cell(0.499999999, 2.999999999) == (1, 2)
    assert grid.world_to_cell(0.5, 2.5) is None
    assert grid.world_to_cell(-1.000000001, 2.5) is None
    assert grid.cell_to_world(1, 2) == (0.25, 2.75)

    for row, column in ((True, 0), (0, False), (-1, 0), (0, 3)):
        with pytest.raises(NavigationSafetyInputError):
            grid.cell_to_world(row, column)
    with pytest.raises(NavigationSafetyInputError):
        grid.world_to_cell(True, 2.0)


def test_sparse_path_sampling_catches_cost_253_tunneling():
    """Half-cell segment sampling catches an obstacle between poses."""
    costs = [0] * 15
    costs[1 * 5 + 2] = 253
    result = validate_sample_path(**_inputs(costs=costs))
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_COST_BLOCKED
    )

    costs[1 * 5 + 2] = 252
    result = validate_sample_path(**_inputs(costs=costs))
    assert type(result) is PathSafetyProof
    assert result.maximum_cost == 252


def test_clearance_point_288_is_inclusive_and_below_is_rejected():
    """The fixed .238 m radius plus .05 m margin has an inclusive edge."""
    assert REQUIRED_PATH_CLEARANCE_M == 0.288
    result = validate_sample_path(
        **_inputs(clearances=[0.288] * 15)
    )
    assert type(result) is PathSafetyProof
    assert result.minimum_clearance_m == 0.288

    clearances = [0.288] * 15
    clearances[7] = 0.287999999
    result = validate_sample_path(**_inputs(clearances=clearances))
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_CLEARANCE_INSUFFICIENT
    )


@pytest.mark.parametrize(
    'points,start,target,code',
    [
        (
            [PathPoint(0.25, 0.75), PathPoint(2.25, 0.75)],
            PathPoint(0.199999, 0.75),
            PathPoint(2.25, 0.75),
            PathSafetyFailureCode.PATH_START_GAP_TOO_LARGE,
        ),
        (
            [PathPoint(0.25, 0.75), PathPoint(2.25, 0.75)],
            PathPoint(0.25, 0.75),
            PathPoint(2.300001, 0.75),
            PathSafetyFailureCode.PATH_TARGET_GAP_TOO_LARGE,
        ),
    ],
)
def test_endpoint_gap_is_checked_at_both_ends(points, start, target, code):
    """Both current-pose and target endpoints use the .05 m limit."""
    result = validate_sample_path(
        **_inputs(points=points, start=start, target=target)
    )
    assert result == PathSafetyFailure(code)


def test_endpoint_gap_equal_to_limit_is_accepted():
    """Exactly .05 m is allowed by the fixed server profile."""
    result = validate_sample_path(
        **_inputs(
            start=PathPoint(0.20, 0.75),
            target=PathPoint(2.30, 0.75),
        )
    )
    assert type(result) is PathSafetyProof
    assert result.start_gap_m == pytest.approx(0.05)
    assert result.target_gap_m == pytest.approx(0.05)


def test_any_interpolated_sample_off_map_fails_closed():
    """A segment that exits the grid is rejected without coordinate output."""
    result = validate_sample_path(
        **_inputs(
            points=[PathPoint(0.25, 0.75), PathPoint(2.75, 0.75)],
            target=PathPoint(2.75, 0.75),
        )
    )
    assert result == PathSafetyFailure(PathSafetyFailureCode.PATH_OFF_MAP)
    assert '2.75' not in repr(result)


def test_exact_start_and_target_references_are_also_safety_checked():
    """A nearby safe endpoint cannot launder an unsafe reference point."""
    result = validate_sample_path(
        **_inputs(
            points=[PathPoint(0.25, 0.75), PathPoint(2.49, 0.75)],
            target=PathPoint(2.51, 0.75),
        )
    )
    assert result == PathSafetyFailure(PathSafetyFailureCode.PATH_OFF_MAP)

    costs = [0] * 15
    costs[1 * 5 + 2] = 253
    result = validate_sample_path(
        **_inputs(
            costs=costs,
            points=[PathPoint(0.25, 0.75), PathPoint(0.99, 0.75)],
            target=PathPoint(1.01, 0.75),
        )
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_COST_BLOCKED
    )


def test_grid_alignment_requires_exact_axis_aligned_metadata():
    """A separately valid but shifted clearance grid is not aligned."""
    inputs = _inputs()
    inputs['static_clearance'] = StaticClearanceGrid(
        'map', 5, 3, 0.5, 0.5, 0.0, 0.0, [0.5] * 15
    )
    result = validate_sample_path(**inputs)
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.GRID_ALIGNMENT_MISMATCH
    )

    for grid_type, values in (
        (MapCostGrid, [0]),
        (StaticClearanceGrid, [0.5]),
    ):
        with pytest.raises(NavigationSafetyInputError):
            grid_type('odom', 1, 1, 0.5, 0.0, 0.0, 0.0, values)
        with pytest.raises(NavigationSafetyInputError):
            grid_type('map', 1, 1, 0.5, 0.0, 0.0, 0.01, values)


def test_constructors_reject_bool_subclasses_nan_and_bad_data():
    """Numeric coercion, subclasses, NaN, infinity, and bad arrays fail."""

    class FloatSubclass(float):
        pass

    class IntSubclass(int):
        pass

    invalid_points = (
        (True, 0.0),
        (0, 0.0),
        (FloatSubclass(0.0), 0.0),
        (float('nan'), 0.0),
        (float('inf'), 0.0),
        (1_000_000.1, 0.0),
    )
    for x_m, y_m in invalid_points:
        with pytest.raises(NavigationSafetyInputError) as captured:
            PathPoint(x_m, y_m)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    invalid_grids = (
        ('map', True, 1, 0.5, 0.0, 0.0, 0.0, [0]),
        ('map', IntSubclass(1), 1, 0.5, 0.0, 0.0, 0.0, [0]),
        ('map', 1, 1, True, 0.0, 0.0, 0.0, [0]),
        ('map', 1, 1, 0.0, 0.0, 0.0, 0.0, [0]),
        ('map', 1, 1, 0.5, 0.0, 0.0, 0.0, [256]),
        ('map', 1, 1, 0.5, 0.0, 0.0, 0.0, [False]),
    )
    for arguments in invalid_grids:
        with pytest.raises(NavigationSafetyInputError):
            MapCostGrid(*arguments)

    for value in (True, 0, FloatSubclass(0.5), -0.1, float('nan')):
        with pytest.raises(NavigationSafetyInputError):
            StaticClearanceGrid(
                'map', 1, 1, 0.5, 0.0, 0.0, 0.0, [value]
            )


@pytest.mark.parametrize(
    'geometry',
    [
        {'type': 'LineString', 'coordinates': []},
        {'type': 'Polygon', 'coordinates': []},
        {'type': 'MultiPolygon', 'coordinates': []},
        {
            'type': 'Polygon',
            'coordinates': [[[0.0, 0.0], [1.0, 0.0], [0.0, 0.0]]],
        },
        {
            'type': 'Polygon',
            'coordinates': [[
                [0.0, 0.0],
                [1.0, 0.0],
                [1.0, 1.0],
                [0.0, 1.0],
            ]],
        },
        {
            'type': 'Polygon',
            'coordinates': [[
                [0.0, 0.0],
                [1.0, 1.0],
                [0.0, 1.0],
                [1.0, 0.0],
                [0.0, 0.0],
            ]],
        },
        {
            'type': 'Polygon',
            'coordinates': [[
                [0.0, 0.0],
                [1.0, 0.0],
                [2.0, 0.0],
                [0.0, 0.0],
            ]],
        },
        {
            'type': 'Polygon',
            'coordinates': [_rectangle(0.0, 0.0, 2.0, 2.0)],
            'bbox': [0.0, 0.0, 2.0, 2.0],
        },
    ],
)
def test_restricted_zones_reject_malformed_geometry(geometry):
    """Only strict closed simple Polygon/MultiPolygon values are valid."""
    with pytest.raises(NavigationSafetyInputError) as captured:
        RestrictedZones('map', [geometry])
    assert captured.value.code == 'invalid_restricted_zones'
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_restricted_zones_reject_invalid_holes_and_components():
    """Holes and MultiPolygon components must be strictly well formed."""
    invalid = [
        _polygon(
            _rectangle(0.0, 0.0, 2.0, 2.0),
            _rectangle(2.5, 2.5, 3.0, 3.0),
        ),
        _polygon(
            _rectangle(0.0, 0.0, 2.0, 2.0),
            _rectangle(0.0, 0.5, 1.0, 1.5),
        ),
        _polygon(
            _rectangle(0.0, 0.0, 4.0, 4.0),
            _rectangle(0.5, 0.5, 3.5, 3.5),
            _rectangle(1.0, 1.0, 2.0, 2.0),
        ),
        {
            'type': 'MultiPolygon',
            'coordinates': [
                [_rectangle(0.0, 0.0, 2.0, 2.0)],
                [_rectangle(1.0, 1.0, 3.0, 3.0)],
            ],
        },
        {
            'type': 'MultiPolygon',
            'coordinates': [
                [_rectangle(0.0, 0.0, 1.0, 1.0)],
                [_rectangle(1.0, 0.0, 2.0, 1.0)],
            ],
        },
    ]
    for geometry in invalid:
        with pytest.raises(NavigationSafetyInputError):
            RestrictedZones('map', [geometry])


def test_restricted_zones_reject_bool_subclass_nan_and_bounds():
    """Zone containers, keys, and coordinates use exact bounded built-ins."""

    class FloatSubclass(float):
        pass

    class IntSubclass(int):
        pass

    class ListSubclass(list):
        pass

    class StringSubclass(str):
        pass

    invalid_coordinates = (
        True,
        FloatSubclass(0.0),
        IntSubclass(0),
        float('nan'),
        float('inf'),
        1_000_001,
    )
    for value in invalid_coordinates:
        ring = _rectangle(0.0, 0.0, 1.0, 1.0)
        ring[0][0] = value
        ring[-1][0] = value
        with pytest.raises(NavigationSafetyInputError):
            RestrictedZones('map', [_polygon(ring)])

    with pytest.raises(NavigationSafetyInputError):
        RestrictedZones(
            'map',
            [{StringSubclass('type'): 'Polygon', 'coordinates': []}],
        )
    with pytest.raises(NavigationSafetyInputError):
        RestrictedZones(
            'map',
            [{'type': StringSubclass('Polygon'), 'coordinates': []}],
        )
    with pytest.raises(NavigationSafetyInputError):
        RestrictedZones(StringSubclass('map'), [])
    with pytest.raises(NavigationSafetyInputError):
        RestrictedZones('map', ListSubclass())
    with pytest.raises(NavigationSafetyInputError):
        RestrictedZones('map', (geometry for geometry in ()))
    with pytest.raises(NavigationSafetyInputError):
        RestrictedZones('odom', [])


def test_zone_digest_mismatch_wrong_type_and_object_mutation_never_prove():
    """Only a fresh exact zone snapshot matching its digest can prove."""
    zones = RestrictedZones(
        'map', [_polygon(_rectangle(3.0, 0.5, 3.5, 1.0))]
    )
    inputs = _inputs(restricted_zones=zones)
    inputs['zones_digest'] = 'f' * 64
    assert validate_sample_path(**inputs) == PathSafetyFailure(
        PathSafetyFailureCode.ZONES_DIGEST_MISMATCH
    )

    inputs = _inputs()
    inputs['restricted_zones'] = None
    assert validate_sample_path(**inputs) == PathSafetyFailure(
        PathSafetyFailureCode.RESTRICTED_ZONES_TAMPERED
    )

    inputs = _inputs()
    del inputs['restricted_zones']
    with pytest.raises(TypeError):
        validate_sample_path(**inputs)

    mutations = (
        ('_boundary_segment_count', 0),
        ('_geometries', ()),
        ('_digest', 'e' * 64),
    )
    for slot, replacement in mutations:
        current = RestrictedZones(
            'map', [_polygon(_rectangle(3.0, 0.5, 3.5, 1.0))]
        )
        inputs = _inputs(restricted_zones=current)
        object.__setattr__(current, slot, replacement)
        result = validate_sample_path(**inputs)
        assert result == PathSafetyFailure(
            PathSafetyFailureCode.RESTRICTED_ZONES_TAMPERED
        )
        assert not hasattr(
            result, 'restricted_zone_validation_performed'
        )


def test_zone_path_candidate_budget_fails_before_quadratic_validation():
    """Path-to-boundary work has a fixed fail-closed candidate budget."""
    vertices = [
        [
            10.0 + math.cos(index * 2.0 * math.pi / 500.0),
            10.0 + math.sin(index * 2.0 * math.pi / 500.0),
        ]
        for index in range(500)
    ]
    vertices.append(list(vertices[0]))
    zones = RestrictedZones('map', [_polygon(vertices)])
    repeated = [PathPoint(0.25, 0.75)] * 4096
    result = validate_sample_path(
        **_inputs(
            restricted_zones=zones,
            points=repeated,
            start=PathPoint(0.25, 0.75),
            target=PathPoint(0.25, 0.75),
        )
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_ZONE_VALIDATION_BUDGET_EXCEEDED
    )


def test_grid_and_path_budgets_fail_before_unbounded_work():
    """Dimensions, cell count, point count, and sampling are bounded."""
    with pytest.raises(NavigationSafetyInputError):
        MapCostGrid(
            'map', MAX_GRID_CELLS + 1, 1, 0.5, 0.0, 0.0, 0.0, []
        )
    with pytest.raises(NavigationSafetyInputError):
        MapCostGrid('map', 4096, 4096, 0.5, 0.0, 0.0, 0.0, [])
    with pytest.raises(NavigationSafetyInputError):
        SamplePath('map', [PathPoint(0.0, 0.0)] * 4097)
    with pytest.raises(NavigationSafetyInputError):
        MapCostGrid(
            'map', 1, 1, 0.0000001, 0.0, 0.0, 0.0, [0]
        )

    result = validate_sample_path(
        **_inputs(
            width=1,
            height=1,
            resolution=0.000001,
            points=[PathPoint(0.0, 0.0), PathPoint(0.1, 0.0)],
            start=PathPoint(0.0, 0.0),
            target=PathPoint(0.1, 0.0),
        )
    )
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.PATH_SAMPLE_BUDGET_EXCEEDED
    )


def test_server_profile_cannot_be_configured_by_a_caller():
    """Only the fixed v2 profile can be constructed or used."""
    assert ServerSafetyProfile().digest == SERVER_SAFETY_PROFILE.digest
    assert SERVER_SAFETY_PROFILE.restricted_zone_validation_required is True
    assert not hasattr(
        SERVER_SAFETY_PROFILE,
        'restricted_zone_validation_performed',
    )
    with pytest.raises(TypeError):
        ServerSafetyProfile(required_clearance_m=0.0)


def test_source_container_mutation_cannot_change_snapshots_or_digests():
    """Data objects detach lists and never expose coordinate dictionaries."""
    costs = [0] * 15
    clearances = [0.5] * 15
    points = [PathPoint(0.25, 0.75), PathPoint(2.25, 0.75)]
    inputs = _inputs(
        costs=costs, clearances=clearances, points=points
    )
    digests = (
        inputs['costmap'].digest,
        inputs['static_clearance'].digest,
        inputs['path'].digest,
    )
    costs[7] = 255
    clearances[7] = 0.0
    points[:] = [PathPoint(999.0, 999.0)]

    assert (
        inputs['costmap'].digest,
        inputs['static_clearance'].digest,
        inputs['path'].digest,
    ) == digests
    assert type(validate_sample_path(**inputs)) is PathSafetyProof
    for value in (
        inputs['start_point'],
        inputs['costmap'],
        inputs['static_clearance'],
        inputs['path'],
    ):
        assert '<redacted>' in repr(value)
        with pytest.raises(TypeError):
            vars(value)


@pytest.mark.parametrize(
    'name,slot,replacement,code',
    [
        (
            'costmap',
            '_costs',
            (255,) * 15,
            PathSafetyFailureCode.COSTMAP_TAMPERED,
        ),
        (
            'static_clearance',
            '_clearances_m',
            (0.0,) * 15,
            PathSafetyFailureCode.CLEARANCE_GRID_TAMPERED,
        ),
        (
            'path',
            '_points',
            ((0.25, 0.75), (1.25, 0.75)),
            PathSafetyFailureCode.PATH_TAMPERED,
        ),
        (
            'start_point',
            '_x_m',
            1.25,
            PathSafetyFailureCode.START_POINT_TAMPERED,
        ),
        (
            'target_point',
            '_digest',
            'f' * 64,
            PathSafetyFailureCode.TARGET_POINT_TAMPERED,
        ),
    ],
)
def test_object_setattr_and_stale_cached_digest_fail_closed(
    name, slot, replacement, code
):
    """Fresh reconstruction rejects frozen-object bypass and stale hashes."""
    inputs = _inputs()
    value = inputs[name]
    with pytest.raises(FrozenInstanceError):
        setattr(value, slot, replacement)
    object.__setattr__(value, slot, replacement)
    result = validate_sample_path(**inputs)
    assert result == PathSafetyFailure(code)


@pytest.mark.parametrize(
    'name,value',
    [
        ('target_binding_digest', True),
        ('operation_binding_digest', '0' * 64),
        ('map_content_digest', 'A' * 64),
        ('semantic_content_digest', 'short'),
        ('zones_digest', str('5' * 63)),
    ],
)
def test_external_digest_bindings_are_strict(name, value):
    """Every external binding is an exact nonzero lowercase SHA-256."""
    inputs = _inputs()
    inputs[name] = value
    result = validate_sample_path(**inputs)
    assert result == PathSafetyFailure(
        PathSafetyFailureCode.INVALID_BINDING_DIGEST
    )


def test_proof_is_deterministic_across_hash_seeds_and_processes():
    """Canonical hashes do not depend on process hash randomization."""
    script = r'''
from malbut_gazebo.gazebo_monitor_room_navigation_safety import *
cost = MapCostGrid('map', 2, 1, 0.5, 0.0, 0.0, 0.0, [0, 7])
clearance = StaticClearanceGrid(
    'map', 2, 1, 0.5, 0.0, 0.0, 0.0, [0.288, 0.5]
)
path = SamplePath('map', [PathPoint(0.25, 0.25), PathPoint(0.75, 0.25)])
zones = RestrictedZones('map', [{
    'type': 'Polygon',
    'coordinates': [[
        [2.0, 2.0], [3.0, 2.0], [3.0, 3.0],
        [2.0, 3.0], [2.0, 2.0],
    ]],
}])
proof = validate_sample_path(
    start_point=PathPoint(0.25, 0.25),
    target_point=PathPoint(0.75, 0.25),
    target_binding_digest='1' * 64,
    operation_binding_digest='2' * 64,
    map_content_digest='3' * 64,
    semantic_content_digest='4' * 64,
    zones_digest=zones.digest,
    restricted_zones=zones,
    costmap=cost,
    static_clearance=clearance,
    path=path,
)
print(proof.input_bundle_digest)
print(proof.proof_digest)
'''
    package_root = Path(__file__).resolve().parents[1]
    isolated_script = (
        "import sys\n"
        f"sys.path.insert(0, {str(package_root)!r})\n"
        + script
    )
    outputs = []
    for seed in ('1', '8675309'):
        environment = dict(os.environ)
        environment['PYTHONHASHSEED'] = seed
        completed = subprocess.run(
            [sys.executable, '-I', '-c', isolated_script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def test_module_has_no_ros_io_clock_or_numeric_runtime_dependency():
    """The pure core cannot acquire state, perform effects, or use NumPy."""
    import malbut_gazebo.gazebo_monitor_room_navigation_safety as module

    source = inspect.getsource(module)
    prohibited = (
        'import rclpy',
        'import rospy',
        'import cv2',
        'import numpy',
        'import random',
        'import time',
        'import os',
        'open(',
        'subprocess',
        'socket',
    )
    for fragment in prohibited:
        assert fragment not in source
