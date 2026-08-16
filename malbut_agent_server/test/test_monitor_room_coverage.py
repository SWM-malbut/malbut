"""Tests for deterministic non-physical room coverage sample planning."""

import builtins
import dataclasses
import hashlib
import json
import math
import os
import random
import socket
import subprocess
import sys
import time

import pytest

from malbut_agent_server import monitor_room_coverage as coverage
from malbut_agent_server.monitor_room_coverage import (
    CoveragePlannerProfile,
    CoveragePlanningError,
    CoverageSample,
    build_monitor_room_coverage_plan,
)
from malbut_agent_server.monitor_room_target import Effects, TargetBinding


def _ring_area(ring):
    return abs(sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(ring, ring[1:])
    )) / 2.0


def _geometry_area(geometry):
    raw_polygons = (
        [geometry['coordinates']]
        if geometry['type'] == 'Polygon'
        else geometry['coordinates']
    )
    return sum(
        _ring_area(polygon[0])
        - sum(_ring_area(hole) for hole in polygon[1:])
        for polygon in raw_polygons
    )


def _effects(**updates):
    values = {
        'physical_navigation': True,
        'camera_capture': True,
        'external_video_stream': True,
        'video_recording': False,
        'audio_capture': False,
        'max_duration_seconds': 300,
        'coverage_mode': 'whole_room',
        'viewer_scope': 'requesting_user',
        'talkback_allowed': False,
    }
    values.update(updates)
    return Effects(**values)


def _target(
    geometry,
    representative_point,
    *,
    effects=None,
    room_name='SECRET-ROOM-NAME',
    device_id='secret-device-id',
):
    geometry_json = json.dumps(
        geometry,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )
    return TargetBinding(
        device_id=device_id,
        device_binding_revision='device-binding-revision',
        source_revision='source-revision',
        map_id='secret-map-id',
        map_revision='map-revision',
        semantic_revision='1' * 64,
        frame_id='map',
        room_id='secret-room-id',
        room_name=room_name,
        room_category='living_room',
        source_arguments_digest='2' * 64,
        geometry_json=geometry_json,
        geometry_digest=hashlib.sha256(
            geometry_json.encode('utf-8')
        ).hexdigest(),
        representative_point=representative_point,
        clearance_m=0.25,
        area_m2=round(_geometry_area(geometry), 2),
        effects=_effects() if effects is None else effects,
    )


def _rectangle(minimum_x, minimum_y, maximum_x, maximum_y):
    return {
        'type': 'Polygon',
        'coordinates': [[
            [float(minimum_x), float(minimum_y)],
            [float(maximum_x), float(minimum_y)],
            [float(maximum_x), float(maximum_y)],
            [float(minimum_x), float(maximum_y)],
            [float(minimum_x), float(minimum_y)],
        ]],
    }


def _circle(radius, edge_count):
    ring = [
        [
            radius * math.cos(2.0 * math.pi * index / edge_count),
            radius * math.sin(2.0 * math.pi * index / edge_count),
        ]
        for index in range(edge_count)
    ]
    ring.append(list(ring[0]))
    return {'type': 'Polygon', 'coordinates': [ring]}


def _coordinates(result):
    return tuple(
        (
            sample.polygon_ordinal,
            sample.row_ordinal,
            sample.x_mm,
            sample.y_mm,
        )
        for sample in result.plan.samples
    )


def test_rectangle_uses_global_mm_lattice_and_boustrophedon() -> None:
    """Interior rows alternate x direction on one global map lattice."""
    result = build_monitor_room_coverage_plan(
        _target(_rectangle(0, 0, 2, 2), (1.0, 1.0))
    )

    assert _coordinates(result) == (
        (0, 0, 500, 500),
        (0, 0, 1000, 500),
        (0, 0, 1500, 500),
        (0, 1, 1500, 1000),
        (0, 1, 1000, 1000),
        (0, 1, 500, 1000),
        (0, 2, 500, 1500),
        (0, 2, 1000, 1500),
        (0, 2, 1500, 1500),
    )
    assert all(
        sample.x_mm % 500 == 0 and sample.y_mm % 500 == 0
        for sample in result.plan.samples
    )
    assert result.plan.candidate_upper_bound == 25


def test_concave_polygon_excludes_missing_interior() -> None:
    """A concave bounding-box region is not mistaken for room interior."""
    geometry = {
        'type': 'Polygon',
        'coordinates': [[
            [0.0, 0.0],
            [3.0, 0.0],
            [3.0, 1.0],
            [1.0, 1.0],
            [1.0, 3.0],
            [0.0, 3.0],
            [0.0, 0.0],
        ]],
    }
    result = build_monitor_room_coverage_plan(
        _target(geometry, (0.5, 0.5))
    )
    points = {(sample.x_mm, sample.y_mm) for sample in result.plan.samples}

    assert (500, 2500) in points
    assert (2500, 500) in points
    assert (1500, 1500) not in points
    assert (1000, 1000) not in points


def test_hole_and_every_boundary_are_excluded() -> None:
    """Outer, hole, and exact lattice-aligned boundaries produce no sample."""
    geometry = {
        'type': 'Polygon',
        'coordinates': [
            [
                [0.0, 0.0],
                [3.0, 0.0],
                [3.0, 3.0],
                [0.0, 3.0],
                [0.0, 0.0],
            ],
            [
                [1.0, 1.0],
                [2.0, 1.0],
                [2.0, 2.0],
                [1.0, 2.0],
                [1.0, 1.0],
            ],
        ],
    }
    result = build_monitor_room_coverage_plan(
        _target(geometry, (0.5, 0.5))
    )
    points = {(sample.x_mm, sample.y_mm) for sample in result.plan.samples}

    assert points
    assert all(0 < x < 3000 and 0 < y < 3000 for x, y in points)
    assert not any(
        1000 <= x <= 2000 and 1000 <= y <= 2000
        for x, y in points
    )


def test_multipolygon_parts_are_sorted_by_geometry_not_input_order() -> None:
    """Canonical components use deterministic spatial polygon ordering."""
    left = _rectangle(0, 0, 1.5, 1.5)['coordinates']
    right = _rectangle(5, 2, 6.5, 3.5)['coordinates']
    geometry = {
        'type': 'MultiPolygon',
        'coordinates': [right, left],
    }
    result = build_monitor_room_coverage_plan(
        _target(geometry, (0.5, 0.5))
    )

    assert result.plan.component_count == 2
    assert result.plan.samples[0].polygon_ordinal == 0
    assert result.plan.samples[0].x_mm == 500
    assert result.plan.samples[-1].polygon_ordinal == 1
    assert result.plan.samples[-1].x_mm >= 5500


def test_multipolygon_rejects_one_component_without_lattice_sample() -> None:
    """A sampled component cannot hide another unsampled component."""
    normal = _rectangle(0, 0, 2, 2)['coordinates']
    narrow = _rectangle(5.1, 0.1, 5.4, 0.4)['coordinates']
    binding = _target(
        {
            'type': 'MultiPolygon',
            'coordinates': [normal, narrow],
        },
        (1.0, 1.0),
    )

    with pytest.raises(CoveragePlanningError) as captured:
        build_monitor_room_coverage_plan(binding)

    assert captured.value.code == 'no_semantic_samples'


def test_plan_requires_every_declared_component_to_be_represented() -> None:
    """The immutable plan validates its component/sample relationship."""
    result = build_monitor_room_coverage_plan(
        _target(_rectangle(0, 0, 2, 2), (1.0, 1.0))
    )

    with pytest.raises(CoveragePlanningError) as captured:
        dataclasses.replace(result.plan, component_count=2)

    assert captured.value.code == 'invalid_plan'


def test_result_is_deterministic_and_digests_bind_all_evidence() -> None:
    """Repeated input is exact and target/profile/sample mutations rebind."""
    target = _target(_rectangle(-1, -1, 2, 2), (0.5, 0.5))
    first = build_monitor_room_coverage_plan(target)
    second = build_monitor_room_coverage_plan(target)
    changed_effects = build_monitor_room_coverage_plan(
        _target(
            _rectangle(-1, -1, 2, 2),
            (0.5, 0.5),
            effects=_effects(max_duration_seconds=301),
        )
    )
    changed_geometry = build_monitor_room_coverage_plan(
        _target(_rectangle(-1, -1, 2.5, 2), (0.5, 0.5))
    )

    assert first.to_private_dict() == second.to_private_dict()
    assert first.plan.digest == second.plan.digest
    assert first.result_digest == second.result_digest == first.digest
    assert first.plan.planner_revision == coverage.PLANNER_REVISION
    assert changed_effects.plan.digest != first.plan.digest
    assert changed_effects.result_digest != first.result_digest
    assert changed_geometry.plan.digest != first.plan.digest


def test_result_digest_explicitly_binds_persisted_summary_counts() -> None:
    """Ledger summary count tampering cannot preserve the result digest."""
    result = build_monitor_room_coverage_plan(
        _target(_rectangle(0, 0, 2, 2), (1.0, 1.0))
    )
    body = result._digest_value()
    expected_fields = {
        'schema_version',
        'code',
        'planner_revision',
        'profile_digest',
        'plan_digest',
        'sample_count',
        'component_count',
        'simulation',
        'physical_effects',
        'viewer_live',
        'nav2_validated',
        'camera_coverage_validated',
        'coverage_achieved',
        'execution_authorized',
    }

    assert set(body) == expected_fields
    assert body['profile_digest'] == result.plan.profile.digest
    assert body['sample_count'] == result.plan.sample_count
    assert body['component_count'] == result.plan.component_count
    assert coverage._digest(body) == result.result_digest
    for field_name in ('sample_count', 'component_count'):
        tampered = dict(body)
        tampered[field_name] += 1
        assert coverage._digest(tampered) != result.result_digest


def test_digest_is_identical_across_hash_seeded_subprocesses() -> None:
    """Hash randomization and process boundaries cannot alter plan output."""
    target = _target(_rectangle(-1, -1, 2, 2), (0.5, 0.5))
    private_json = json.dumps(
        target.to_private_dict(),
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    )
    script = (
        'import json,sys;'
        'from malbut_agent_server.monitor_room_target import TargetBinding;'
        'from malbut_agent_server.monitor_room_coverage import '
        'build_monitor_room_coverage_plan;'
        't=TargetBinding.from_private_dict(json.loads(sys.argv[1]));'
        'r=build_monitor_room_coverage_plan(t);'
        'print(json.dumps(r.to_private_dict(),sort_keys=True,'
        "separators=(',',':')))"
    )
    package_root = os.path.dirname(os.path.dirname(__file__))
    outputs = []
    for seed in ('1', '777'):
        environment = dict(os.environ)
        environment['PYTHONHASHSEED'] = seed
        environment['PYTHONPATH'] = package_root
        completed = subprocess.run(
            [sys.executable, '-c', script, private_json],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            cwd=package_root,
            timeout=10,
        )
        outputs.append(completed.stdout)

    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    ('target', 'code'),
    [
        (
            lambda: _target(
                _rectangle(0, 0, 200, 200),
                (100.0, 100.0),
            ),
            'candidate_budget_exceeded',
        ),
        (
            lambda: _target(_circle(13.0, 128), (0.0, 0.0)),
            'geometry_test_budget_exceeded',
        ),
        (
            lambda: _target(
                _rectangle(0, 0, 36, 36),
                (18.0, 18.0),
            ),
            'sample_budget_exceeded',
        ),
    ],
)
def test_budgets_fail_before_candidate_geometry_loop(
    monkeypatch,
    target,
    code,
) -> None:
    """All upper bounds are checked before any membership-test loop."""
    binding = target()

    def unexpected_geometry_test(*_args, **_kwargs):
        raise AssertionError('geometry loop ran before budget rejection')

    monkeypatch.setattr(
        coverage,
        '_strictly_inside',
        unexpected_geometry_test,
    )
    with pytest.raises(CoveragePlanningError) as captured:
        build_monitor_room_coverage_plan(binding)

    assert captured.value.code == code


def test_near_geometry_budget_is_bounded_and_fast() -> None:
    """A near-cap Decimal workload stays bounded for a short DB operation."""
    binding = _target(_circle(12.0, 96), (0.0, 0.0))
    started = time.perf_counter()
    result = build_monitor_room_coverage_plan(binding)
    elapsed = time.perf_counter() - started

    assert 200_000 <= result.plan.geometry_test_upper_bound <= 250_000
    assert result.plan.candidate_upper_bound <= 4096
    assert result.plan.sample_count <= 4096
    assert elapsed < 2.0


def test_tiny_geometry_without_global_lattice_point_fails_typed() -> None:
    """The planner does not invent an off-lattice fallback point."""
    binding = _target(
        _rectangle(0.1, 0.1, 0.4, 0.4),
        (0.25, 0.25),
    )
    with pytest.raises(CoveragePlanningError) as captured:
        build_monitor_room_coverage_plan(binding)

    assert captured.value.code == 'no_semantic_samples'


@pytest.mark.parametrize('value', [None, {}, object(), True, 1])
def test_only_exact_target_binding_is_accepted(value) -> None:
    """Untrusted lookalikes and implicit values fail at the sole input seam."""
    with pytest.raises(CoveragePlanningError) as captured:
        build_monitor_room_coverage_plan(value)

    assert captured.value.code == 'invalid_target'


def test_profile_and_sample_types_are_strict() -> None:
    """Booleans, caller profiles, and non-lattice samples are rejected."""
    with pytest.raises(CoveragePlanningError) as profile_error:
        CoveragePlannerProfile(lattice_spacing_mm=True)
    with pytest.raises(CoveragePlanningError) as sample_error:
        CoverageSample(
            index=True,
            polygon_ordinal=0,
            row_ordinal=0,
            x_mm=500,
            y_mm=500,
        )
    with pytest.raises(CoveragePlanningError) as lattice_error:
        CoverageSample(
            index=0,
            polygon_ordinal=0,
            row_ordinal=0,
            x_mm=501,
            y_mm=500,
        )

    assert profile_error.value.code == 'invalid_profile'
    assert sample_error.value.code == 'invalid_sample'
    assert lattice_error.value.code == 'invalid_sample'


def test_objects_are_frozen_and_private_dto_is_detached() -> None:
    """Callers cannot mutate nested plan evidence through returned DTOs."""
    result = build_monitor_room_coverage_plan(
        _target(_rectangle(0, 0, 2, 2), (1.0, 1.0))
    )
    private = result.to_private_dict()
    original_x = result.plan.samples[0].x_mm
    private['plan']['samples'][0]['x_mm'] = 9_999_500

    assert result.plan.samples[0].x_mm == original_x
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.coverage_achieved = True
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.plan.samples[0].x_mm = 1000
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.plan.profile.max_samples = 9999


def test_mutated_target_fails_without_reusing_stale_digest() -> None:
    """Post-construction target mutation cannot retain old binding evidence."""
    target = _target(_rectangle(0, 0, 2, 2), (1.0, 1.0))
    object.__setattr__(target, 'geometry_digest', '0' * 64)

    with pytest.raises(CoveragePlanningError) as captured:
        build_monitor_room_coverage_plan(target)

    assert captured.value.code == 'invalid_target'


def test_public_dto_is_secret_free_and_never_claims_execution() -> None:
    """Public results contain digests/counts, not target content or success."""
    result = build_monitor_room_coverage_plan(
        _target(_rectangle(0, 0, 2, 2), (1.0, 1.0))
    )
    public = result.to_dict()
    encoded = json.dumps(public, sort_keys=True)

    assert 'SECRET-ROOM-NAME' not in encoded
    assert 'secret-device-id' not in encoded
    assert 'secret-map-id' not in encoded
    assert 'secret-room-id' not in encoded
    assert 'x_mm' not in encoded
    assert 'y_mm' not in encoded
    assert 'path' not in encoded
    assert 'waypoint' not in encoded
    assert public['simulation'] is True
    assert public['physical_effects'] is False
    assert public['viewer_live'] is False
    assert public['nav2_validated'] is False
    assert public['camera_coverage_validated'] is False
    assert public['coverage_achieved'] is False
    assert public['execution_authorized'] is False
    assert public['result_digest'] == result.result_digest
    assert public['plan']['sample_count'] == result.plan.sample_count
    assert public['plan']['component_count'] == 1


def test_public_failures_are_allowlisted_content_free_and_chain_free() -> None:
    """Raw target data cannot survive in code, message, chain, or traceback."""
    secret = 'SUPER-SECRET-GEOMETRY-VALUE'
    with pytest.raises(CoveragePlanningError) as captured:
        build_monitor_room_coverage_plan({'geometry': secret})
    error = captured.value

    assert error.code == 'invalid_target'
    assert str(error) == 'room coverage planning failed'
    assert secret not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get('__name__') == (
            'malbut_agent_server.monitor_room_coverage'
        ):
            local_dump = repr(traceback.tb_frame.f_locals)
            assert secret not in local_dump
        traceback = traceback.tb_next

    arbitrary = CoveragePlanningError(secret)
    assert arbitrary.code == 'invalid_failure_code'
    assert secret not in repr(arbitrary)


def test_planning_uses_no_external_side_effect_or_nondeterminism(
    monkeypatch,
) -> None:
    """Planning remains pure even when common external seams are poisoned."""
    binding = _target(_rectangle(0, 0, 2, 2), (1.0, 1.0))

    def forbidden(*_args, **_kwargs):
        raise AssertionError('external side effect was attempted')

    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(os, 'urandom', forbidden)
    monkeypatch.setattr(random, 'random', forbidden)
    monkeypatch.setattr(time, 'time', forbidden)

    result = build_monitor_room_coverage_plan(binding)

    assert result.plan.sample_count == 9
