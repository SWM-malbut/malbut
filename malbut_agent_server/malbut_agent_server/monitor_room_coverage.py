"""
Deterministic semantic sample planning for one monitor-room target.

The planner is deliberately weaker than a navigation or camera-coverage
planner.  It converts the immutable room geometry in :class:`TargetBinding`
into bounded, ordered points on a global integer-millimetre map lattice.  A
later trusted adapter must validate every point and path against the current
map, zones, costmaps, robot footprint, and camera model.

This module performs no clock reads, randomness, I/O, ROS, Nav2, image
processing, or external calls.  Its result always states that no physical
effect, navigation validation, camera validation, viewer evidence, or room
coverage has occurred.
"""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, Tuple

from malbut_agent_server.monitor_room_target import (
    Effects,
    TargetBinding,
)
from malbut_agent_server.schemas import ValidationError


COVERAGE_PROFILE_SCHEMA_VERSION = 1
COVERAGE_SAMPLE_SCHEMA_VERSION = 1
COVERAGE_PLAN_SCHEMA_VERSION = 1
COVERAGE_RESULT_SCHEMA_VERSION = 1

MAP_FRAME = 'map'
MILLIMETRES_PER_METRE = 1000

PROFILE_ID = 'semantic-global-mm-lattice-v1'
PLANNER_REVISION = 'monitor-room-coverage-planner-v1'
LATTICE_SPACING_MM = 500
MAX_CANDIDATES = 65536
MAX_GEOMETRY_TESTS = 250_000
MAX_SAMPLES = 4096

_FAILURE_MESSAGE = 'room coverage planning failed'
_SHA256_LENGTH = 64
_FAILURE_CODES = frozenset(
    {
        'candidate_budget_exceeded',
        'geometry_test_budget_exceeded',
        'invalid_failure_code',
        'invalid_plan',
        'invalid_profile',
        'invalid_result',
        'invalid_sample',
        'invalid_target',
        'no_semantic_samples',
        'sample_budget_exceeded',
    }
)


class CoveragePlanningError(ValidationError):
    """Content-free typed failure from semantic coverage planning."""

    def __init__(self, code: str) -> None:
        """Expose only a stable code and a target-independent message."""
        super().__init__(_FAILURE_MESSAGE)
        self.code = (
            code if code in _FAILURE_CODES else 'invalid_failure_code'
        )


def _fail(code: str) -> None:
    raise CoveragePlanningError(code)


def _exact_int(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _digest(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in '0123456789abcdef' for character in value)
    )


@dataclass(frozen=True)
class CoveragePlannerProfile:
    """Fixed, server-owned semantic lattice planner profile."""

    profile_id: str = PROFILE_ID
    planner_revision: str = PLANNER_REVISION
    frame_id: str = MAP_FRAME
    lattice_spacing_mm: int = LATTICE_SPACING_MM
    max_candidates: int = MAX_CANDIDATES
    max_geometry_tests: int = MAX_GEOMETRY_TESTS
    max_samples: int = MAX_SAMPLES
    boundary_rule: str = 'strict_interior'
    ordering: str = 'polygon_y_boustrophedon_x'
    schema_version: int = COVERAGE_PROFILE_SCHEMA_VERSION
    _profile_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Reject caller-selected profiles and implicit numeric values."""
        expected = (
            PROFILE_ID,
            PLANNER_REVISION,
            MAP_FRAME,
            LATTICE_SPACING_MM,
            MAX_CANDIDATES,
            MAX_GEOMETRY_TESTS,
            MAX_SAMPLES,
            'strict_interior',
            'polygon_y_boustrophedon_x',
            COVERAGE_PROFILE_SCHEMA_VERSION,
        )
        actual = (
            self.profile_id,
            self.planner_revision,
            self.frame_id,
            self.lattice_spacing_mm,
            self.max_candidates,
            self.max_geometry_tests,
            self.max_samples,
            self.boundary_rule,
            self.ordering,
            self.schema_version,
        )
        if actual != expected or any(
            type(value) is not int
            for value in (
                self.lattice_spacing_mm,
                self.max_candidates,
                self.max_geometry_tests,
                self.max_samples,
                self.schema_version,
            )
        ):
            _fail('invalid_profile')
        object.__setattr__(
            self,
            '_profile_digest',
            _digest(self.to_dict()),
        )

    @property
    def digest(self) -> str:
        """Return the canonical fixed-profile digest."""
        return self._profile_digest

    def to_dict(self) -> Dict[str, Any]:
        """Return a detached, public profile description."""
        return {
            'schema_version': self.schema_version,
            'profile_id': self.profile_id,
            'planner_revision': self.planner_revision,
            'frame_id': self.frame_id,
            'lattice_spacing_mm': self.lattice_spacing_mm,
            'max_candidates': self.max_candidates,
            'max_geometry_tests': self.max_geometry_tests,
            'max_samples': self.max_samples,
            'boundary_rule': self.boundary_rule,
            'ordering': self.ordering,
        }


DEFAULT_COVERAGE_PROFILE = CoveragePlannerProfile()


@dataclass(frozen=True)
class CoverageSample:
    """One strict-interior semantic candidate in fixed-point map units."""

    index: int
    polygon_ordinal: int
    row_ordinal: int
    x_mm: int
    y_mm: int
    frame_id: str = MAP_FRAME
    schema_version: int = COVERAGE_SAMPLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Keep sample coordinates exact, bounded, and map-frame only."""
        coordinate_bound = 10_000 * MILLIMETRES_PER_METRE
        if (
            not _exact_int(self.index, 0, MAX_SAMPLES - 1)
            or not _exact_int(
                self.polygon_ordinal,
                0,
                MAX_CANDIDATES - 1,
            )
            or not _exact_int(self.row_ordinal, 0, MAX_CANDIDATES - 1)
            or not _exact_int(
                self.x_mm,
                -coordinate_bound,
                coordinate_bound,
            )
            or not _exact_int(
                self.y_mm,
                -coordinate_bound,
                coordinate_bound,
            )
            or self.x_mm % LATTICE_SPACING_MM != 0
            or self.y_mm % LATTICE_SPACING_MM != 0
            or self.frame_id != MAP_FRAME
            or type(self.schema_version) is not int
            or self.schema_version != COVERAGE_SAMPLE_SCHEMA_VERSION
        ):
            _fail('invalid_sample')

    def to_private_dict(self) -> Dict[str, Any]:
        """Return fixed-point coordinates for a trusted later preflight."""
        return {
            'schema_version': self.schema_version,
            'index': self.index,
            'polygon_ordinal': self.polygon_ordinal,
            'row_ordinal': self.row_ordinal,
            'frame_id': self.frame_id,
            'x_mm': self.x_mm,
            'y_mm': self.y_mm,
        }


@dataclass(frozen=True)
class CoveragePlan:
    """Immutable ordered semantic candidates bound to one target."""

    profile: CoveragePlannerProfile
    target_binding_digest: str
    source_arguments_digest: str
    geometry_digest: str
    effects_digest: str
    samples: Tuple[CoverageSample, ...] = field(repr=False)
    component_count: int
    candidate_upper_bound: int
    geometry_test_upper_bound: int
    frame_id: str = MAP_FRAME
    schema_version: int = COVERAGE_PLAN_SCHEMA_VERSION
    semantic_samples_only: bool = field(default=True, init=False)
    _plan_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate ordering and calculate the complete evidence digest."""
        if (
            type(self.profile) is not CoveragePlannerProfile
            or self.profile != DEFAULT_COVERAGE_PROFILE
            or not _valid_digest(self.target_binding_digest)
            or not _valid_digest(self.source_arguments_digest)
            or not _valid_digest(self.geometry_digest)
            or not _valid_digest(self.effects_digest)
            or type(self.samples) is not tuple
            or not self.samples
            or len(self.samples) > self.profile.max_samples
            or not _exact_int(self.component_count, 1, 128)
            or not _exact_int(
                self.candidate_upper_bound,
                len(self.samples),
                self.profile.max_candidates,
            )
            or not _exact_int(
                self.geometry_test_upper_bound,
                1,
                self.profile.max_geometry_tests,
            )
            or self.frame_id != MAP_FRAME
            or type(self.schema_version) is not int
            or self.schema_version != COVERAGE_PLAN_SCHEMA_VERSION
            or self.semantic_samples_only is not True
        ):
            _fail('invalid_plan')
        if any(
            type(sample) is not CoverageSample or sample.index != index
            for index, sample in enumerate(self.samples)
        ):
            _fail('invalid_plan')
        previous_key = None
        previous_row = None
        previous_x = None
        represented_components = set()
        for sample in self.samples:
            if sample.polygon_ordinal >= self.component_count:
                _fail('invalid_plan')
            represented_components.add(sample.polygon_ordinal)
            key = (sample.polygon_ordinal, sample.row_ordinal)
            if previous_key is not None and key < previous_key:
                _fail('invalid_plan')
            if key == previous_row:
                if sample.row_ordinal % 2 == 0:
                    if sample.x_mm <= previous_x:
                        _fail('invalid_plan')
                elif sample.x_mm >= previous_x:
                    _fail('invalid_plan')
            previous_key = key
            previous_row = key
            previous_x = sample.x_mm
        if represented_components != set(range(self.component_count)):
            _fail('invalid_plan')
        object.__setattr__(self, '_plan_digest', _digest(self._digest_value()))

    @property
    def digest(self) -> str:
        """Return a digest over profile, target evidence, and sample order."""
        return self._plan_digest

    def _digest_value(self) -> Dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'profile': self.profile.to_dict(),
            'profile_digest': self.profile.digest,
            'target_evidence': {
                'binding_digest': self.target_binding_digest,
                'source_arguments_digest': self.source_arguments_digest,
                'geometry_digest': self.geometry_digest,
                'effects_digest': self.effects_digest,
                'frame_id': self.frame_id,
            },
            'candidate_upper_bound': self.candidate_upper_bound,
            'geometry_test_upper_bound': self.geometry_test_upper_bound,
            'component_count': self.component_count,
            'ordered_fixed_point_samples': [
                [
                    sample.polygon_ordinal,
                    sample.row_ordinal,
                    sample.x_mm,
                    sample.y_mm,
                ]
                for sample in self.samples
            ],
            'semantic_samples_only': True,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return a coordinate-free summary safe for public responses."""
        return {
            'schema_version': self.schema_version,
            'planner_revision': self.profile.planner_revision,
            'profile_digest': self.profile.digest,
            'target_binding_digest': self.target_binding_digest,
            'geometry_digest': self.geometry_digest,
            'effects_digest': self.effects_digest,
            'plan_digest': self.digest,
            'sample_count': self.sample_count,
            'component_count': self.component_count,
            'semantic_samples_only': True,
        }

    @property
    def planner_revision(self) -> str:
        """Return the implementation revision bound by the profile."""
        return self.profile.planner_revision

    @property
    def sample_count(self) -> int:
        """Return the immutable ordered sample count."""
        return len(self.samples)

    def to_private_dict(self) -> Dict[str, Any]:
        """Return detached semantic samples for trusted Nav2 preflight."""
        result = self.to_dict()
        result.update(
            {
                'profile': self.profile.to_dict(),
                'source_arguments_digest': self.source_arguments_digest,
                'frame_id': self.frame_id,
                'candidate_upper_bound': self.candidate_upper_bound,
                'component_count': self.component_count,
                'geometry_test_upper_bound': (
                    self.geometry_test_upper_bound
                ),
                'samples': [
                    sample.to_private_dict() for sample in self.samples
                ],
            }
        )
        return result


@dataclass(frozen=True)
class CoveragePlanningResult:
    """Honest non-physical result of semantic sample-plan construction."""

    plan: CoveragePlan = field(repr=False)
    code: str = 'semantic_sample_plan_created'
    schema_version: int = COVERAGE_RESULT_SCHEMA_VERSION
    simulation: bool = field(default=True, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    nav2_validated: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    execution_authorized: bool = field(default=False, init=False)
    _result_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Prevent semantic planning from claiming stronger evidence."""
        if (
            type(self.plan) is not CoveragePlan
            or self.code != 'semantic_sample_plan_created'
            or type(self.schema_version) is not int
            or self.schema_version != COVERAGE_RESULT_SCHEMA_VERSION
            or self.simulation is not True
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.nav2_validated is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
            or self.execution_authorized is not False
        ):
            _fail('invalid_result')
        object.__setattr__(
            self,
            '_result_digest',
            _digest(self._digest_value()),
        )

    def _digest_value(self) -> Dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'code': self.code,
            'planner_revision': self.plan.planner_revision,
            'profile_digest': self.plan.profile.digest,
            'plan_digest': self.plan.digest,
            'sample_count': self.plan.sample_count,
            'component_count': self.plan.component_count,
            'simulation': True,
            'physical_effects': False,
            'viewer_live': False,
            'nav2_validated': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
            'execution_authorized': False,
        }

    @property
    def digest(self) -> str:
        """Return the canonical content-minimized result digest."""
        return self._result_digest

    @property
    def result_digest(self) -> str:
        """Return the digest under its persistence-facing name."""
        return self._result_digest

    def to_dict(self) -> Dict[str, Any]:
        """Return a content-minimized public result with honest flags."""
        return {
            'schema_version': self.schema_version,
            'code': self.code,
            'result_digest': self.result_digest,
            'plan': self.plan.to_dict(),
            'simulation': True,
            'physical_effects': False,
            'viewer_live': False,
            'nav2_validated': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
            'execution_authorized': False,
        }

    def to_private_dict(self) -> Dict[str, Any]:
        """Return detached samples plus the same non-authorizing flags."""
        result = self.to_dict()
        result['plan'] = self.plan.to_private_dict()
        return result


Point = Tuple[Decimal, Decimal]
Ring = Tuple[Point, ...]
Polygon = Tuple[Ring, ...]


@dataclass(frozen=True)
class _PreparedPolygon:
    polygon: Polygon = field(repr=False)
    sort_key: Tuple[Any, ...] = field(repr=False)
    first_x_mm: int
    last_x_mm: int
    first_y_mm: int
    last_y_mm: int
    x_count: int
    y_count: int
    edge_count: int


def _snapshot_target(target: Any) -> TargetBinding:
    if (
        type(target) is not TargetBinding
        or type(target.effects) is not Effects
    ):
        _fail('invalid_target')
    try:
        effects = Effects(
            physical_navigation=target.effects.physical_navigation,
            camera_capture=target.effects.camera_capture,
            external_video_stream=target.effects.external_video_stream,
            video_recording=target.effects.video_recording,
            audio_capture=target.effects.audio_capture,
            max_duration_seconds=target.effects.max_duration_seconds,
            coverage_mode=target.effects.coverage_mode,
            viewer_scope=target.effects.viewer_scope,
            talkback_allowed=target.effects.talkback_allowed,
            schema_version=target.effects.schema_version,
        )
        snapshot = TargetBinding(
            device_id=target.device_id,
            device_binding_revision=target.device_binding_revision,
            source_revision=target.source_revision,
            map_id=target.map_id,
            map_revision=target.map_revision,
            semantic_revision=target.semantic_revision,
            frame_id=target.frame_id,
            room_id=target.room_id,
            room_name=target.room_name,
            room_category=target.room_category,
            source_arguments_digest=target.source_arguments_digest,
            geometry_json=target.geometry_json,
            geometry_digest=target.geometry_digest,
            representative_point=target.representative_point,
            clearance_m=target.clearance_m,
            area_m2=target.area_m2,
            effects=effects,
            schema_version=target.schema_version,
        )
    except (AttributeError, TypeError, ValidationError, ValueError):
        _fail('invalid_target')
    if (
        snapshot.binding_digest != target.binding_digest
        or snapshot.geometry_digest != target.geometry_digest
        or snapshot.effects_digest != target.effects_digest
    ):
        _fail('invalid_target')
    return snapshot


def _decimal_geometry(target: TargetBinding) -> Tuple[Polygon, ...]:
    try:
        value = json.loads(
            target.geometry_json,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError('invalid number')
            ),
        )
        geometry_type = value['type']
        coordinates = value['coordinates']
        raw_polygons = (
            [coordinates]
            if geometry_type == 'Polygon'
            else coordinates
        )
        polygons = tuple(
            tuple(
                tuple((point[0], point[1]) for point in ring)
                for ring in polygon
            )
            for polygon in raw_polygons
        )
    except (KeyError, TypeError, ValueError, IndexError):
        _fail('invalid_target')
    return polygons


def _lattice_ceiling(value_m: Decimal) -> int:
    value_mm = value_m * MILLIMETRES_PER_METRE
    quotient = value_mm / LATTICE_SPACING_MM
    return int(quotient.to_integral_value(rounding=ROUND_CEILING)) * (
        LATTICE_SPACING_MM
    )


def _lattice_floor(value_m: Decimal) -> int:
    value_mm = value_m * MILLIMETRES_PER_METRE
    quotient = value_mm / LATTICE_SPACING_MM
    return int(quotient.to_integral_value(rounding=ROUND_FLOOR)) * (
        LATTICE_SPACING_MM
    )


def _prepare_polygon(polygon: Polygon) -> _PreparedPolygon:
    outer = polygon[0]
    xs = tuple(point[0] for point in outer[:-1])
    ys = tuple(point[1] for point in outer[:-1])
    minimum_x = min(xs)
    maximum_x = max(xs)
    minimum_y = min(ys)
    maximum_y = max(ys)
    first_x = _lattice_ceiling(minimum_x)
    last_x = _lattice_floor(maximum_x)
    first_y = _lattice_ceiling(minimum_y)
    last_y = _lattice_floor(maximum_y)
    x_count = (
        0
        if first_x > last_x
        else ((last_x - first_x) // LATTICE_SPACING_MM) + 1
    )
    y_count = (
        0
        if first_y > last_y
        else ((last_y - first_y) // LATTICE_SPACING_MM) + 1
    )
    edge_count = sum(len(ring) - 1 for ring in polygon)
    sort_key = (
        minimum_y,
        minimum_x,
        maximum_y,
        maximum_x,
        polygon,
    )
    return _PreparedPolygon(
        polygon=polygon,
        sort_key=sort_key,
        first_x_mm=first_x,
        last_x_mm=last_x,
        first_y_mm=first_y,
        last_y_mm=last_y,
        x_count=x_count,
        y_count=y_count,
        edge_count=edge_count,
    )


def _point_on_segment(point: Point, first: Point, second: Point) -> bool:
    cross = (
        (second[0] - first[0]) * (point[1] - first[1])
        - (second[1] - first[1]) * (point[0] - first[0])
    )
    return (
        cross == 0
        and min(first[0], second[0]) <= point[0] <= max(
            first[0], second[0]
        )
        and min(first[1], second[1]) <= point[1] <= max(
            first[1], second[1]
        )
    )


def _ring_membership(point: Point, ring: Ring) -> int:
    """Return -1 on boundary, 1 inside, and 0 outside one ring."""
    inside = False
    x, y = point
    for first, second in zip(ring, ring[1:]):
        if _point_on_segment(point, first, second):
            return -1
        if (first[1] > y) == (second[1] > y):
            continue
        crossing_x = (
            first[0]
            + (y - first[1])
            * (second[0] - first[0])
            / (second[1] - first[1])
        )
        if x < crossing_x:
            inside = not inside
    return 1 if inside else 0


def _strictly_inside(point: Point, polygon: Polygon) -> bool:
    if _ring_membership(point, polygon[0]) != 1:
        return False
    return all(_ring_membership(point, hole) == 0 for hole in polygon[1:])


def _budget(prepared: Tuple[_PreparedPolygon, ...]) -> Tuple[int, int]:
    candidates = sum(item.x_count * item.y_count for item in prepared)
    geometry_tests = sum(
        item.x_count * item.y_count * item.edge_count
        for item in prepared
    )
    if candidates > DEFAULT_COVERAGE_PROFILE.max_candidates:
        _fail('candidate_budget_exceeded')
    if geometry_tests > DEFAULT_COVERAGE_PROFILE.max_geometry_tests:
        _fail('geometry_test_budget_exceeded')
    if candidates > DEFAULT_COVERAGE_PROFILE.max_samples:
        _fail('sample_budget_exceeded')
    return candidates, geometry_tests


def _sample(
    prepared: Tuple[_PreparedPolygon, ...],
) -> Tuple[CoverageSample, ...]:
    samples = []
    for polygon_ordinal, item in enumerate(prepared):
        component_start = len(samples)
        sample_row_ordinal = 0
        for lattice_row in range(item.y_count):
            y_mm = item.first_y_mm + lattice_row * LATTICE_SPACING_MM
            accepted_x = []
            for column in range(item.x_count):
                x_mm = item.first_x_mm + column * LATTICE_SPACING_MM
                point = (
                    Decimal(x_mm) / MILLIMETRES_PER_METRE,
                    Decimal(y_mm) / MILLIMETRES_PER_METRE,
                )
                if _strictly_inside(point, item.polygon):
                    accepted_x.append(x_mm)
            if not accepted_x:
                continue
            if sample_row_ordinal % 2 == 1:
                accepted_x.reverse()
            for x_mm in accepted_x:
                samples.append(
                    CoverageSample(
                        index=len(samples),
                        polygon_ordinal=polygon_ordinal,
                        row_ordinal=sample_row_ordinal,
                        x_mm=x_mm,
                        y_mm=y_mm,
                    )
                )
            sample_row_ordinal += 1
        if len(samples) == component_start:
            _fail('no_semantic_samples')
    if not samples:
        _fail('no_semantic_samples')
    return tuple(samples)


def _build_plan(target: Any) -> CoveragePlanningResult:
    snapshot = _snapshot_target(target)
    polygons = _decimal_geometry(snapshot)
    prepared = tuple(sorted(
        (_prepare_polygon(polygon) for polygon in polygons),
        key=lambda item: item.sort_key,
    ))
    candidate_bound, geometry_test_bound = _budget(prepared)
    samples = _sample(prepared)
    plan = CoveragePlan(
        profile=DEFAULT_COVERAGE_PROFILE,
        target_binding_digest=snapshot.binding_digest,
        source_arguments_digest=snapshot.source_arguments_digest,
        geometry_digest=snapshot.geometry_digest,
        effects_digest=snapshot.effects_digest,
        samples=samples,
        component_count=len(prepared),
        candidate_upper_bound=candidate_bound,
        geometry_test_upper_bound=geometry_test_bound,
    )
    return CoveragePlanningResult(plan=plan)


def build_monitor_room_coverage_plan(target: Any) -> CoveragePlanningResult:
    """
    Build bounded semantic samples from exactly one TargetBinding.

    The returned order is deterministic but is not a route.  No sample has
    been checked for reachability, collision clearance, visibility, camera
    coverage, or current-map validity.  Failures are normalized after the
    raw exception scope is gone so no target content remains in an exception
    chain or retained traceback frame.
    """
    failure_code = None
    try:
        result = _build_plan(target)
    except CoveragePlanningError as error:
        failure_code = error.code
    except Exception:
        failure_code = 'invalid_target'
    if failure_code is not None:
        del target
        raise CoveragePlanningError(failure_code)
    return result


__all__ = [
    'CoveragePlan',
    'CoveragePlannerProfile',
    'CoveragePlanningError',
    'CoveragePlanningResult',
    'CoverageSample',
    'DEFAULT_COVERAGE_PROFILE',
    'PLANNER_REVISION',
    'build_monitor_room_coverage_plan',
]
