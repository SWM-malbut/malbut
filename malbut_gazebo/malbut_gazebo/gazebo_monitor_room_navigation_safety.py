"""
Pure deterministic path-safety checks for monitor-room navigation.

This module deliberately has no ROS, I/O, clock, image-processing, or
execution dependency.  It validates one already-computed planner path.  It
does not authorize motion and does not claim room coverage, physical motion,
stream readiness, or viewer playback.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Optional, Tuple, Union


MAP_FRAME = 'map'
ROBOT_RADIUS_M = 0.238
PATH_CLEARANCE_MARGIN_M = 0.05
REQUIRED_PATH_CLEARANCE_M = 0.288
TRAVERSABLE_COST_EXCLUSIVE = 253
MAX_ENDPOINT_GAP_M = 0.05
MAX_GRID_DIMENSION = 4096
MAX_GRID_CELLS = 4_194_304
MAX_PATH_POINTS = 4096
MAX_PATH_SAMPLES = 131_072
MAX_ZONE_GEOMETRIES = 64
MAX_ZONE_POLYGONS = 128
MAX_ZONE_RINGS = 256
MAX_ZONE_RING_POINTS = 512
MAX_ZONE_POINTS = 4096
MAX_ZONE_GEOMETRY_CANDIDATES = 1_000_000
MAX_PATH_ZONE_CANDIDATES = 2_000_000
MAX_ABS_COORDINATE_M = 1_000_000.0
MIN_RESOLUTION_M = 0.000001
MAX_RESOLUTION_M = 1_000.0
SAMPLE_SPACING_RESOLUTION_FRACTION = 0.5
ZONE_BOUNDARY_EPSILON_M = 0.000000001
ZONE_MINIMUM_DOUBLE_AREA_M2 = 0.000000000001

_HEX_DIGITS = frozenset('0123456789abcdef')


class NavigationSafetyInputError(ValueError):
    """Report a bounded, content-free DTO construction failure."""

    _CODES = frozenset({
        'invalid_clearance_grid',
        'invalid_cost_grid',
        'invalid_grid_cell',
        'invalid_path',
        'invalid_path_point',
        'invalid_restricted_zones',
    })

    def __init__(self, code: str) -> None:
        """Create an error without retaining rejected input values."""
        normalized = (
            code if type(code) is str and code in self._CODES
            else 'invalid_path'
        )
        super().__init__(normalized)
        self.code = normalized

    def __getattribute__(self, name):
        """Hide collaborator exception chains at the public boundary."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


class PathSafetyFailureCode(str, Enum):
    """Enumerate coordinate-free path validation failures."""

    INVALID_INPUT = 'invalid_input'
    INVALID_BINDING_DIGEST = 'invalid_binding_digest'
    COSTMAP_TAMPERED = 'costmap_tampered'
    CLEARANCE_GRID_TAMPERED = 'clearance_grid_tampered'
    PATH_TAMPERED = 'path_tampered'
    START_POINT_TAMPERED = 'start_point_tampered'
    TARGET_POINT_TAMPERED = 'target_point_tampered'
    RESTRICTED_ZONES_TAMPERED = 'restricted_zones_tampered'
    ZONES_DIGEST_MISMATCH = 'zones_digest_mismatch'
    GRID_ALIGNMENT_MISMATCH = 'grid_alignment_mismatch'
    PATH_SAMPLE_BUDGET_EXCEEDED = 'path_sample_budget_exceeded'
    PATH_OFF_MAP = 'path_off_map'
    PATH_COST_BLOCKED = 'path_cost_blocked'
    PATH_CLEARANCE_INSUFFICIENT = 'path_clearance_insufficient'
    PATH_START_GAP_TOO_LARGE = 'path_start_gap_too_large'
    PATH_TARGET_GAP_TOO_LARGE = 'path_target_gap_too_large'
    PATH_RESTRICTED_ZONE = 'path_restricted_zone'
    PATH_ZONE_BOUNDARY_CONTACT = 'path_zone_boundary_contact'
    PATH_ZONE_VALIDATION_BUDGET_EXCEEDED = (
        'path_zone_validation_budget_exceeded'
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(',', ':'),
    ).encode('ascii')


def _digest(tag: str, value: object) -> str:
    return hashlib.sha256(
        _canonical_bytes(('malbut-navigation-safety-v1', tag, value))
    ).hexdigest()


def _float_token(value: float) -> str:
    return value.hex()


def _require_float(
    value: object,
    *,
    code: str,
    nonnegative: bool = False,
    positive: bool = False,
    coordinate: bool = False,
) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise NavigationSafetyInputError(code)
    if nonnegative and value < 0.0:
        raise NavigationSafetyInputError(code)
    if positive and value <= 0.0:
        raise NavigationSafetyInputError(code)
    if coordinate and abs(value) > MAX_ABS_COORDINATE_M:
        raise NavigationSafetyInputError(code)
    return 0.0 if value == 0.0 else value


def _require_int(
    value: object,
    *,
    code: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise NavigationSafetyInputError(code)
    return value


def _valid_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value != '0' * 64
        and all(character in _HEX_DIGITS for character in value)
    )


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class PathPoint:
    """Hold one immutable, redacted point in the map frame."""

    _x_m: float
    _y_m: float
    _digest: str

    def __init__(self, x_m: float, y_m: float) -> None:
        """Snapshot one exact pair of finite built-in floats."""
        x_value = _require_float(
            x_m, code='invalid_path_point', coordinate=True
        )
        y_value = _require_float(
            y_m, code='invalid_path_point', coordinate=True
        )
        digest = _digest(
            'path-point', (_float_token(x_value), _float_token(y_value))
        )
        object.__setattr__(self, '_x_m', x_value)
        object.__setattr__(self, '_y_m', y_value)
        object.__setattr__(self, '_digest', digest)

    @property
    def digest(self) -> str:
        """Return the coordinate-free content digest."""
        return self._digest

    def __repr__(self) -> str:
        """Return a representation that never exposes coordinates."""
        return 'PathPoint(<redacted>)'


_ZONE_OUTSIDE = 0
_ZONE_INSIDE = 1
_ZONE_BOUNDARY = 2


def _zone_sequence(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> tuple:
    if type(value) not in (list, tuple):
        raise NavigationSafetyInputError('invalid_restricted_zones')
    if not minimum <= len(value) <= maximum:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    return tuple(value)


def _zone_coordinate(value: object) -> float:
    if type(value) is int:
        if abs(value) > MAX_ABS_COORDINATE_M:
            raise NavigationSafetyInputError('invalid_restricted_zones')
        normalized = float(value)
    elif type(value) is float:
        if (
            not math.isfinite(value)
            or abs(value) > MAX_ABS_COORDINATE_M
        ):
            raise NavigationSafetyInputError('invalid_restricted_zones')
        normalized = value
    else:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    return 0.0 if normalized == 0.0 else normalized


def _cross(
    first: Tuple[float, float],
    second: Tuple[float, float],
    point: Tuple[float, float],
) -> float:
    return (
        (second[0] - first[0]) * (point[1] - first[1])
        - (second[1] - first[1]) * (point[0] - first[0])
    )


def _cross_tolerance(
    first: Tuple[float, float], second: Tuple[float, float]
) -> float:
    return ZONE_BOUNDARY_EPSILON_M * max(
        1.0,
        math.hypot(second[0] - first[0], second[1] - first[1]),
    )


def _point_on_segment(
    point: Tuple[float, float],
    first: Tuple[float, float],
    second: Tuple[float, float],
) -> bool:
    tolerance = _cross_tolerance(first, second)
    if abs(_cross(first, second, point)) > tolerance:
        return False
    epsilon = ZONE_BOUNDARY_EPSILON_M
    return (
        min(first[0], second[0]) - epsilon
        <= point[0]
        <= max(first[0], second[0]) + epsilon
        and min(first[1], second[1]) - epsilon
        <= point[1]
        <= max(first[1], second[1]) + epsilon
    )


def _orientation_sign(
    first: Tuple[float, float],
    second: Tuple[float, float],
    point: Tuple[float, float],
) -> int:
    value = _cross(first, second, point)
    tolerance = _cross_tolerance(first, second)
    if value > tolerance:
        return 1
    if value < -tolerance:
        return -1
    return 0


def _segments_touch(
    first_start: Tuple[float, float],
    first_end: Tuple[float, float],
    second_start: Tuple[float, float],
    second_end: Tuple[float, float],
) -> bool:
    epsilon = ZONE_BOUNDARY_EPSILON_M
    if (
        max(first_start[0], first_end[0]) + epsilon
        < min(second_start[0], second_end[0])
        or max(second_start[0], second_end[0]) + epsilon
        < min(first_start[0], first_end[0])
        or max(first_start[1], first_end[1]) + epsilon
        < min(second_start[1], second_end[1])
        or max(second_start[1], second_end[1]) + epsilon
        < min(first_start[1], first_end[1])
    ):
        return False
    signs = (
        _orientation_sign(first_start, first_end, second_start),
        _orientation_sign(first_start, first_end, second_end),
        _orientation_sign(second_start, second_end, first_start),
        _orientation_sign(second_start, second_end, first_end),
    )
    if signs[0] == 0 and _point_on_segment(
        second_start, first_start, first_end
    ):
        return True
    if signs[1] == 0 and _point_on_segment(
        second_end, first_start, first_end
    ):
        return True
    if signs[2] == 0 and _point_on_segment(
        first_start, second_start, second_end
    ):
        return True
    if signs[3] == 0 and _point_on_segment(
        first_end, second_start, second_end
    ):
        return True
    return signs[0] * signs[1] < 0 and signs[2] * signs[3] < 0


def _classify_ring(
    point: Tuple[float, float], ring: tuple
) -> int:
    inside = False
    for first, second in zip(ring, ring[1:]):
        if _point_on_segment(point, first, second):
            return _ZONE_BOUNDARY
        if (first[1] > point[1]) != (second[1] > point[1]):
            crossing_x = (
                (second[0] - first[0])
                * (point[1] - first[1])
                / (second[1] - first[1])
                + first[0]
            )
            if point[0] < crossing_x:
                inside = not inside
    return _ZONE_INSIDE if inside else _ZONE_OUTSIDE


def _classify_polygon(
    point: Tuple[float, float], polygon: tuple
) -> int:
    outer = _classify_ring(point, polygon[0])
    if outer != _ZONE_INSIDE:
        return outer
    for hole in polygon[1:]:
        hole_result = _classify_ring(point, hole)
        if hole_result == _ZONE_BOUNDARY:
            return _ZONE_BOUNDARY
        if hole_result == _ZONE_INSIDE:
            return _ZONE_OUTSIDE
    return _ZONE_INSIDE


def _consume_zone_candidates(budget: list, amount: int) -> None:
    budget[0] += amount
    if budget[0] > MAX_ZONE_GEOMETRY_CANDIDATES:
        raise NavigationSafetyInputError('invalid_restricted_zones')


def _parse_zone_point(value: object) -> Tuple[float, float]:
    pair = _zone_sequence(value, minimum=2, maximum=2)
    return _zone_coordinate(pair[0]), _zone_coordinate(pair[1])


def _parse_zone_ring(value: object, counts: list, budget: list) -> tuple:
    raw_points = _zone_sequence(
        value, minimum=4, maximum=MAX_ZONE_RING_POINTS
    )
    counts[2] += len(raw_points)
    if counts[2] > MAX_ZONE_POINTS:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    points = tuple(_parse_zone_point(point) for point in raw_points)
    if points[0] != points[-1]:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    if any(first == second for first, second in zip(points, points[1:])):
        raise NavigationSafetyInputError('invalid_restricted_zones')
    double_area = sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:])
    )
    if abs(double_area) <= ZONE_MINIMUM_DOUBLE_AREA_M2:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    edge_count = len(points) - 1
    _consume_zone_candidates(
        budget, edge_count * max(0, edge_count - 3) // 2
    )
    for first_index in range(edge_count):
        for second_index in range(first_index + 1, edge_count):
            if second_index == first_index + 1:
                continue
            if first_index == 0 and second_index == edge_count - 1:
                continue
            if _segments_touch(
                points[first_index],
                points[first_index + 1],
                points[second_index],
                points[second_index + 1],
            ):
                raise NavigationSafetyInputError('invalid_restricted_zones')
    return points


def _rings_touch(first: tuple, second: tuple, budget: list) -> bool:
    first_edges = len(first) - 1
    second_edges = len(second) - 1
    _consume_zone_candidates(budget, first_edges * second_edges)
    for first_start, first_end in zip(first, first[1:]):
        for second_start, second_end in zip(second, second[1:]):
            if _segments_touch(
                first_start, first_end, second_start, second_end
            ):
                return True
    return False


def _ring_strictly_inside(
    candidate: tuple, container: tuple, budget: list
) -> bool:
    _consume_zone_candidates(
        budget, (len(candidate) - 1) * (len(container) - 1)
    )
    return all(
        _classify_ring(point, container) == _ZONE_INSIDE
        for point in candidate[:-1]
    )


def _parse_zone_polygon(value: object, counts: list, budget: list) -> tuple:
    raw_rings = _zone_sequence(value, minimum=1, maximum=MAX_ZONE_RINGS)
    counts[0] += 1
    counts[1] += len(raw_rings)
    if counts[0] > MAX_ZONE_POLYGONS or counts[1] > MAX_ZONE_RINGS:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    rings = tuple(
        _parse_zone_ring(ring, counts, budget) for ring in raw_rings
    )
    outer = rings[0]
    for hole in rings[1:]:
        if _rings_touch(outer, hole, budget):
            raise NavigationSafetyInputError('invalid_restricted_zones')
        if not _ring_strictly_inside(hole, outer, budget):
            raise NavigationSafetyInputError('invalid_restricted_zones')
    for first_index, first in enumerate(rings[1:]):
        for second in rings[first_index + 2:]:
            if _rings_touch(first, second, budget):
                raise NavigationSafetyInputError(
                    'invalid_restricted_zones'
                )
            _consume_zone_candidates(
                budget, (len(first) - 1) + (len(second) - 1)
            )
            if (
                _classify_ring(first[0], second) != _ZONE_OUTSIDE
                or _classify_ring(second[0], first) != _ZONE_OUTSIDE
            ):
                raise NavigationSafetyInputError(
                    'invalid_restricted_zones'
                )
    return rings


def _polygon_boundaries_touch(
    first: tuple, second: tuple, budget: list
) -> bool:
    return any(
        _rings_touch(first_ring, second_ring, budget)
        for first_ring in first
        for second_ring in second
    )


def _polygons_are_disjoint(
    first: tuple, second: tuple, budget: list
) -> bool:
    if _polygon_boundaries_touch(first, second, budget):
        return False
    first_edges = sum(len(ring) - 1 for ring in first)
    second_edges = sum(len(ring) - 1 for ring in second)
    _consume_zone_candidates(budget, first_edges + second_edges)
    return (
        _classify_polygon(first[0][0], second) == _ZONE_OUTSIDE
        and _classify_polygon(second[0][0], first) == _ZONE_OUTSIDE
    )


def _zone_geometry_items(value: object) -> Tuple[str, object]:
    if type(value) is not dict or len(value) != 2:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    items = tuple(value.items())
    if any(type(key) is not str for key, _item in items):
        raise NavigationSafetyInputError('invalid_restricted_zones')
    if frozenset(key for key, _item in items) != {
        'type', 'coordinates'
    }:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    geometry_type = None
    coordinates = None
    for key, item in items:
        if key == 'type':
            geometry_type = item
        else:
            coordinates = item
    if type(geometry_type) is not str:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    return geometry_type, coordinates


def _parse_zone_geometry(
    value: object, counts: list, budget: list
) -> tuple:
    geometry_type, coordinates = _zone_geometry_items(value)
    if geometry_type == 'Polygon':
        polygons = (_parse_zone_polygon(coordinates, counts, budget),)
    elif geometry_type == 'MultiPolygon':
        raw_polygons = _zone_sequence(
            coordinates, minimum=1, maximum=MAX_ZONE_POLYGONS
        )
        polygons = tuple(
            _parse_zone_polygon(polygon, counts, budget)
            for polygon in raw_polygons
        )
        for first_index, first in enumerate(polygons):
            for second in polygons[first_index + 1:]:
                if not _polygons_are_disjoint(first, second, budget):
                    raise NavigationSafetyInputError(
                        'invalid_restricted_zones'
                    )
    else:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    return geometry_type, polygons


def _zones_canonical_value(frame_id: str, geometries: tuple) -> tuple:
    return (
        frame_id,
        tuple(
            (
                geometry_type,
                tuple(
                    tuple(
                        tuple(
                            (_float_token(x_m), _float_token(y_m))
                            for x_m, y_m in ring
                        )
                        for ring in polygon
                    )
                    for polygon in polygons
                ),
            )
            for geometry_type, polygons in geometries
        ),
    )


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class RestrictedZones:
    """Hold bounded canonical restricted Polygon/MultiPolygon geometry."""

    _frame_id: str
    _geometries: tuple
    _polygon_count: int
    _boundary_segment_count: int
    _digest: str

    def __init__(self, frame_id: str, geometries: object) -> None:
        """Validate and detach strict GeoJSON geometry objects."""
        if type(frame_id) is not str or frame_id != MAP_FRAME:
            raise NavigationSafetyInputError('invalid_restricted_zones')
        raw_geometries = _zone_sequence(
            geometries, minimum=0, maximum=MAX_ZONE_GEOMETRIES
        )
        counts = [0, 0, 0]
        budget = [0]
        parsed = tuple(
            _parse_zone_geometry(geometry, counts, budget)
            for geometry in raw_geometries
        )
        boundary_count = sum(
            len(ring) - 1
            for _geometry_type, polygons in parsed
            for polygon in polygons
            for ring in polygon
        )
        digest = _digest(
            'restricted-zones', _zones_canonical_value(frame_id, parsed)
        )
        object.__setattr__(self, '_frame_id', frame_id)
        object.__setattr__(self, '_geometries', parsed)
        object.__setattr__(self, '_polygon_count', counts[0])
        object.__setattr__(
            self, '_boundary_segment_count', boundary_count
        )
        object.__setattr__(self, '_digest', digest)

    @property
    def digest(self) -> str:
        """Return the coordinate-free canonical geometry digest."""
        return self._digest

    @property
    def geometry_count(self) -> int:
        """Return the number of strict GeoJSON geometry objects."""
        return len(self._geometries)

    @property
    def polygon_count(self) -> int:
        """Return the total number of validated polygon components."""
        return self._polygon_count

    @property
    def boundary_segment_count(self) -> int:
        """Return the bounded number of validated boundary segments."""
        return self._boundary_segment_count

    def __repr__(self) -> str:
        """Return a representation that never exposes zone coordinates."""
        return 'RestrictedZones(<redacted>)'


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class MapCostGrid:
    """Hold a bounded axis-aligned Nav2 cost grid in the map frame."""

    _frame_id: str
    _width: int
    _height: int
    _resolution_m: float
    _origin_x_m: float
    _origin_y_m: float
    _origin_yaw_rad: float
    _costs: Tuple[int, ...]
    _digest: str

    def __init__(
        self,
        frame_id: str,
        width: int,
        height: int,
        resolution_m: float,
        origin_x_m: float,
        origin_y_m: float,
        origin_yaw_rad: float,
        costs: object,
    ) -> None:
        """Validate and detach one complete cost-grid snapshot."""
        if type(frame_id) is not str or frame_id != MAP_FRAME:
            raise NavigationSafetyInputError('invalid_cost_grid')
        width_value = _require_int(
            width,
            code='invalid_cost_grid',
            minimum=1,
            maximum=MAX_GRID_DIMENSION,
        )
        height_value = _require_int(
            height,
            code='invalid_cost_grid',
            minimum=1,
            maximum=MAX_GRID_DIMENSION,
        )
        cell_count = width_value * height_value
        if cell_count > MAX_GRID_CELLS:
            raise NavigationSafetyInputError('invalid_cost_grid')
        resolution_value = _require_float(
            resolution_m, code='invalid_cost_grid', positive=True
        )
        if not MIN_RESOLUTION_M <= resolution_value <= MAX_RESOLUTION_M:
            raise NavigationSafetyInputError('invalid_cost_grid')
        origin_x_value = _require_float(
            origin_x_m, code='invalid_cost_grid', coordinate=True
        )
        origin_y_value = _require_float(
            origin_y_m, code='invalid_cost_grid', coordinate=True
        )
        yaw_value = _require_float(
            origin_yaw_rad, code='invalid_cost_grid'
        )
        if yaw_value != 0.0:
            raise NavigationSafetyInputError('invalid_cost_grid')
        if type(costs) not in (list, tuple) or len(costs) != cell_count:
            raise NavigationSafetyInputError('invalid_cost_grid')
        values = tuple(costs)
        if any(
            type(value) is not int or not 0 <= value <= 255
            for value in values
        ):
            raise NavigationSafetyInputError('invalid_cost_grid')
        canonical = (
            frame_id,
            width_value,
            height_value,
            _float_token(resolution_value),
            _float_token(origin_x_value),
            _float_token(origin_y_value),
            _float_token(yaw_value),
            values,
        )
        object.__setattr__(self, '_frame_id', frame_id)
        object.__setattr__(self, '_width', width_value)
        object.__setattr__(self, '_height', height_value)
        object.__setattr__(self, '_resolution_m', resolution_value)
        object.__setattr__(self, '_origin_x_m', origin_x_value)
        object.__setattr__(self, '_origin_y_m', origin_y_value)
        object.__setattr__(self, '_origin_yaw_rad', yaw_value)
        object.__setattr__(self, '_costs', values)
        object.__setattr__(
            self, '_digest', _digest('map-cost-grid', canonical)
        )

    @property
    def digest(self) -> str:
        """Return the coordinate-free grid content digest."""
        return self._digest

    @property
    def width(self) -> int:
        """Return the validated grid width."""
        return self._width

    @property
    def height(self) -> int:
        """Return the validated grid height."""
        return self._height

    def world_to_cell(
        self, x_m: float, y_m: float
    ) -> Optional[Tuple[int, int]]:
        """Map a finite world point to an inclusive-lower grid cell."""
        x_value = _require_float(
            x_m, code='invalid_path_point', coordinate=True
        )
        y_value = _require_float(
            y_m, code='invalid_path_point', coordinate=True
        )
        column = math.floor(
            (x_value - self._origin_x_m) / self._resolution_m
        )
        row = math.floor(
            (y_value - self._origin_y_m) / self._resolution_m
        )
        if not (0 <= column < self._width and 0 <= row < self._height):
            return None
        return row, column

    def cell_to_world(self, row: int, column: int) -> Tuple[float, float]:
        """Return the exact center of one validated cell."""
        row_value = _require_int(
            row,
            code='invalid_grid_cell',
            minimum=0,
            maximum=self._height - 1,
        )
        column_value = _require_int(
            column,
            code='invalid_grid_cell',
            minimum=0,
            maximum=self._width - 1,
        )
        return (
            self._origin_x_m
            + (column_value + 0.5) * self._resolution_m,
            self._origin_y_m + (row_value + 0.5) * self._resolution_m,
        )

    def __repr__(self) -> str:
        """Return a representation that omits origin and cost content."""
        return 'MapCostGrid(<redacted>)'


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class StaticClearanceGrid:
    """Hold static-map obstacle clearance aligned to a map cost grid."""

    _frame_id: str
    _width: int
    _height: int
    _resolution_m: float
    _origin_x_m: float
    _origin_y_m: float
    _origin_yaw_rad: float
    _clearances_m: Tuple[float, ...]
    _digest: str

    def __init__(
        self,
        frame_id: str,
        width: int,
        height: int,
        resolution_m: float,
        origin_x_m: float,
        origin_y_m: float,
        origin_yaw_rad: float,
        clearances_m: object,
    ) -> None:
        """Validate and detach one complete clearance-grid snapshot."""
        if type(frame_id) is not str or frame_id != MAP_FRAME:
            raise NavigationSafetyInputError('invalid_clearance_grid')
        width_value = _require_int(
            width,
            code='invalid_clearance_grid',
            minimum=1,
            maximum=MAX_GRID_DIMENSION,
        )
        height_value = _require_int(
            height,
            code='invalid_clearance_grid',
            minimum=1,
            maximum=MAX_GRID_DIMENSION,
        )
        cell_count = width_value * height_value
        if cell_count > MAX_GRID_CELLS:
            raise NavigationSafetyInputError('invalid_clearance_grid')
        resolution_value = _require_float(
            resolution_m,
            code='invalid_clearance_grid',
            positive=True,
        )
        if not MIN_RESOLUTION_M <= resolution_value <= MAX_RESOLUTION_M:
            raise NavigationSafetyInputError('invalid_clearance_grid')
        origin_x_value = _require_float(
            origin_x_m,
            code='invalid_clearance_grid',
            coordinate=True,
        )
        origin_y_value = _require_float(
            origin_y_m,
            code='invalid_clearance_grid',
            coordinate=True,
        )
        yaw_value = _require_float(
            origin_yaw_rad, code='invalid_clearance_grid'
        )
        if yaw_value != 0.0:
            raise NavigationSafetyInputError('invalid_clearance_grid')
        if (
            type(clearances_m) not in (list, tuple)
            or len(clearances_m) != cell_count
        ):
            raise NavigationSafetyInputError('invalid_clearance_grid')
        raw_values = tuple(clearances_m)
        values = tuple(
            _require_float(
                value,
                code='invalid_clearance_grid',
                nonnegative=True,
            )
            for value in raw_values
        )
        canonical = (
            frame_id,
            width_value,
            height_value,
            _float_token(resolution_value),
            _float_token(origin_x_value),
            _float_token(origin_y_value),
            _float_token(yaw_value),
            tuple(_float_token(value) for value in values),
        )
        object.__setattr__(self, '_frame_id', frame_id)
        object.__setattr__(self, '_width', width_value)
        object.__setattr__(self, '_height', height_value)
        object.__setattr__(self, '_resolution_m', resolution_value)
        object.__setattr__(self, '_origin_x_m', origin_x_value)
        object.__setattr__(self, '_origin_y_m', origin_y_value)
        object.__setattr__(self, '_origin_yaw_rad', yaw_value)
        object.__setattr__(self, '_clearances_m', values)
        object.__setattr__(
            self, '_digest', _digest('static-clearance-grid', canonical)
        )

    @property
    def digest(self) -> str:
        """Return the coordinate-free grid content digest."""
        return self._digest

    def __repr__(self) -> str:
        """Return a representation that omits origin and clearance data."""
        return 'StaticClearanceGrid(<redacted>)'


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class SamplePath:
    """Hold one bounded planner path without exposing its coordinates."""

    _frame_id: str
    _points: Tuple[Tuple[float, float], ...]
    _digest: str

    def __init__(self, frame_id: str, points: object) -> None:
        """Validate and detach a nonempty map-frame point sequence."""
        if type(frame_id) is not str or frame_id != MAP_FRAME:
            raise NavigationSafetyInputError('invalid_path')
        if type(points) not in (list, tuple):
            raise NavigationSafetyInputError('invalid_path')
        if not 1 <= len(points) <= MAX_PATH_POINTS:
            raise NavigationSafetyInputError('invalid_path')
        snapshot = tuple(points)
        values = []
        for point in snapshot:
            rebuilt = _rebuild_point_input(point)
            values.append((rebuilt._x_m, rebuilt._y_m))
        immutable_values = tuple(values)
        canonical = (
            frame_id,
            tuple(
                (_float_token(x_m), _float_token(y_m))
                for x_m, y_m in immutable_values
            ),
        )
        object.__setattr__(self, '_frame_id', frame_id)
        object.__setattr__(self, '_points', immutable_values)
        object.__setattr__(self, '_digest', _digest('sample-path', canonical))

    @property
    def digest(self) -> str:
        """Return the coordinate-free path content digest."""
        return self._digest

    @property
    def point_count(self) -> int:
        """Return the bounded number of planner points."""
        return len(self._points)

    def __repr__(self) -> str:
        """Return a representation that never exposes path coordinates."""
        return 'SamplePath(<redacted>)'


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ServerSafetyProfile:
    """Describe the single non-configurable server safety profile."""

    frame_id: str
    robot_radius_m: float
    clearance_margin_m: float
    required_clearance_m: float
    traversable_cost_exclusive: int
    max_endpoint_gap_m: float
    sample_spacing_resolution_fraction: float
    snapping_allowed: bool
    restricted_zone_validation_required: bool

    def __init__(self) -> None:
        """Construct only the fixed server-owned v2 profile."""
        object.__setattr__(self, 'frame_id', MAP_FRAME)
        object.__setattr__(self, 'robot_radius_m', ROBOT_RADIUS_M)
        object.__setattr__(
            self, 'clearance_margin_m', PATH_CLEARANCE_MARGIN_M
        )
        object.__setattr__(
            self, 'required_clearance_m', REQUIRED_PATH_CLEARANCE_M
        )
        object.__setattr__(
            self,
            'traversable_cost_exclusive',
            TRAVERSABLE_COST_EXCLUSIVE,
        )
        object.__setattr__(
            self, 'max_endpoint_gap_m', MAX_ENDPOINT_GAP_M
        )
        object.__setattr__(
            self,
            'sample_spacing_resolution_fraction',
            SAMPLE_SPACING_RESOLUTION_FRACTION,
        )
        object.__setattr__(self, 'snapping_allowed', False)
        object.__setattr__(
            self, 'restricted_zone_validation_required', True
        )

    @property
    def digest(self) -> str:
        """Return the fixed profile digest."""
        return _profile_digest(self)

    def __repr__(self) -> str:
        """Return a stable non-coordinate profile representation."""
        return 'ServerSafetyProfile(fixed-v2)'


def _profile_digest(profile: ServerSafetyProfile) -> str:
    return _digest(
        'server-safety-profile',
        (
            profile.frame_id,
            _float_token(profile.robot_radius_m),
            _float_token(profile.clearance_margin_m),
            _float_token(profile.required_clearance_m),
            profile.traversable_cost_exclusive,
            _float_token(profile.max_endpoint_gap_m),
            _float_token(profile.sample_spacing_resolution_fraction),
            profile.snapping_allowed,
            profile.restricted_zone_validation_required,
        ),
    )


SERVER_SAFETY_PROFILE = ServerSafetyProfile()


@dataclass(frozen=True, slots=True, repr=False)
class PathSafetyFailure:
    """Return one typed failure without coordinates or input content."""

    code: PathSafetyFailureCode

    def __repr__(self) -> str:
        """Return only the stable failure code."""
        return 'PathSafetyFailure(code={})'.format(self.code.value)


@dataclass(frozen=True, slots=True, repr=False)
class PathSafetyProof:
    """Bind successful checks without claiming execution or coverage."""

    schema_version: str
    scope: str
    profile_digest: str
    operation_binding_digest: str
    target_binding_digest: str
    map_content_digest: str
    semantic_content_digest: str
    zones_digest: str
    start_point_digest: str
    target_point_digest: str
    costmap_digest: str
    static_clearance_digest: str
    path_digest: str
    sample_trace_digest: str
    zone_validation_digest: str
    input_bundle_digest: str
    proof_digest: str
    path_point_count: int
    sampled_point_count: int
    zone_boundary_segment_count: int
    zone_validation_candidate_count: int
    maximum_cost: int
    minimum_clearance_m: float
    start_gap_m: float
    target_gap_m: float
    restricted_zone_validation_performed: bool
    authority_claimed: bool
    coverage_claimed: bool
    physical_execution_observed: bool
    viewer_observed: bool

    def __repr__(self) -> str:
        """Return a coordinate-free proof representation."""
        return 'PathSafetyProof(single-path-preflight-v2)'


PathSafetyResult = Union[PathSafetyProof, PathSafetyFailure]


def _rebuild_point_input(value: object) -> PathPoint:
    if type(value) is not PathPoint:
        raise NavigationSafetyInputError('invalid_path_point')
    try:
        x_m = object.__getattribute__(value, '_x_m')
        y_m = object.__getattribute__(value, '_y_m')
        cached_digest = object.__getattribute__(value, '_digest')
    except AttributeError:
        raise NavigationSafetyInputError('invalid_path_point') from None
    rebuilt = PathPoint(x_m, y_m)
    if type(cached_digest) is not str or cached_digest != rebuilt.digest:
        raise NavigationSafetyInputError('invalid_path_point')
    return rebuilt


def _zones_as_strict_input(geometries: object) -> list:
    if type(geometries) is not tuple:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    result = []
    for geometry in geometries:
        if type(geometry) is not tuple or len(geometry) != 2:
            raise NavigationSafetyInputError('invalid_restricted_zones')
        geometry_type, polygons = geometry
        if type(geometry_type) is not str or type(polygons) is not tuple:
            raise NavigationSafetyInputError('invalid_restricted_zones')
        polygon_values = []
        for polygon in polygons:
            if type(polygon) is not tuple:
                raise NavigationSafetyInputError(
                    'invalid_restricted_zones'
                )
            ring_values = []
            for ring in polygon:
                if type(ring) is not tuple:
                    raise NavigationSafetyInputError(
                        'invalid_restricted_zones'
                    )
                point_values = []
                for point in ring:
                    if type(point) is not tuple or len(point) != 2:
                        raise NavigationSafetyInputError(
                            'invalid_restricted_zones'
                        )
                    if (
                        type(point[0]) is not float
                        or type(point[1]) is not float
                    ):
                        raise NavigationSafetyInputError(
                            'invalid_restricted_zones'
                        )
                    point_values.append([point[0], point[1]])
                ring_values.append(point_values)
            polygon_values.append(ring_values)
        if geometry_type == 'Polygon' and len(polygon_values) == 1:
            coordinates = polygon_values[0]
        elif geometry_type == 'MultiPolygon':
            coordinates = polygon_values
        else:
            raise NavigationSafetyInputError('invalid_restricted_zones')
        result.append({
            'type': geometry_type,
            'coordinates': coordinates,
        })
    return result


def _rebuild_zones(value: object) -> RestrictedZones:
    if type(value) is not RestrictedZones:
        raise NavigationSafetyInputError('invalid_restricted_zones')
    try:
        frame_id = object.__getattribute__(value, '_frame_id')
        geometries = object.__getattribute__(value, '_geometries')
        polygon_count = object.__getattribute__(value, '_polygon_count')
        boundary_count = object.__getattribute__(
            value, '_boundary_segment_count'
        )
        cached_digest = object.__getattribute__(value, '_digest')
    except AttributeError:
        raise NavigationSafetyInputError(
            'invalid_restricted_zones'
        ) from None
    strict_input = _zones_as_strict_input(geometries)
    rebuilt = RestrictedZones(frame_id, strict_input)
    if (
        type(cached_digest) is not str
        or cached_digest != rebuilt.digest
        or type(polygon_count) is not int
        or polygon_count != rebuilt.polygon_count
        or type(boundary_count) is not int
        or boundary_count != rebuilt.boundary_segment_count
    ):
        raise NavigationSafetyInputError('invalid_restricted_zones')
    return rebuilt


def _rebuild_costmap(value: object) -> MapCostGrid:
    if type(value) is not MapCostGrid:
        raise NavigationSafetyInputError('invalid_cost_grid')
    try:
        snapshot = (
            object.__getattribute__(value, '_frame_id'),
            object.__getattribute__(value, '_width'),
            object.__getattribute__(value, '_height'),
            object.__getattribute__(value, '_resolution_m'),
            object.__getattribute__(value, '_origin_x_m'),
            object.__getattribute__(value, '_origin_y_m'),
            object.__getattribute__(value, '_origin_yaw_rad'),
            object.__getattribute__(value, '_costs'),
        )
        cached_digest = object.__getattribute__(value, '_digest')
    except AttributeError:
        raise NavigationSafetyInputError('invalid_cost_grid') from None
    rebuilt = MapCostGrid(*snapshot)
    if type(cached_digest) is not str or cached_digest != rebuilt.digest:
        raise NavigationSafetyInputError('invalid_cost_grid')
    return rebuilt


def _rebuild_clearance(value: object) -> StaticClearanceGrid:
    if type(value) is not StaticClearanceGrid:
        raise NavigationSafetyInputError('invalid_clearance_grid')
    try:
        snapshot = (
            object.__getattribute__(value, '_frame_id'),
            object.__getattribute__(value, '_width'),
            object.__getattribute__(value, '_height'),
            object.__getattribute__(value, '_resolution_m'),
            object.__getattribute__(value, '_origin_x_m'),
            object.__getattribute__(value, '_origin_y_m'),
            object.__getattribute__(value, '_origin_yaw_rad'),
            object.__getattribute__(value, '_clearances_m'),
        )
        cached_digest = object.__getattribute__(value, '_digest')
    except AttributeError:
        raise NavigationSafetyInputError(
            'invalid_clearance_grid'
        ) from None
    rebuilt = StaticClearanceGrid(*snapshot)
    if type(cached_digest) is not str or cached_digest != rebuilt.digest:
        raise NavigationSafetyInputError('invalid_clearance_grid')
    return rebuilt


def _rebuild_path(value: object) -> SamplePath:
    if type(value) is not SamplePath:
        raise NavigationSafetyInputError('invalid_path')
    try:
        frame_id = object.__getattribute__(value, '_frame_id')
        points = object.__getattribute__(value, '_points')
        cached_digest = object.__getattribute__(value, '_digest')
    except AttributeError:
        raise NavigationSafetyInputError('invalid_path') from None
    if type(points) is not tuple:
        raise NavigationSafetyInputError('invalid_path')
    rebuilt_points = []
    for pair in points:
        if type(pair) is not tuple or len(pair) != 2:
            raise NavigationSafetyInputError('invalid_path')
        rebuilt_points.append(PathPoint(pair[0], pair[1]))
    rebuilt = SamplePath(frame_id, rebuilt_points)
    if type(cached_digest) is not str or cached_digest != rebuilt.digest:
        raise NavigationSafetyInputError('invalid_path')
    return rebuilt


def _aligned(costmap: MapCostGrid, clearance: StaticClearanceGrid) -> bool:
    return (
        costmap._frame_id == clearance._frame_id
        and costmap._width == clearance._width
        and costmap._height == clearance._height
        and _float_token(costmap._resolution_m)
        == _float_token(clearance._resolution_m)
        and _float_token(costmap._origin_x_m)
        == _float_token(clearance._origin_x_m)
        and _float_token(costmap._origin_y_m)
        == _float_token(clearance._origin_y_m)
        and _float_token(costmap._origin_yaw_rad)
        == _float_token(clearance._origin_yaw_rad)
    )


def _sample_count(path: SamplePath, resolution_m: float) -> Optional[int]:
    points = path._points
    count = 1
    spacing = resolution_m * SAMPLE_SPACING_RESOLUTION_FRACTION
    for first, second in zip(points, points[1:]):
        distance = math.hypot(
            second[0] - first[0], second[1] - first[1]
        )
        steps = max(1, math.ceil(distance / spacing))
        count += steps
        if count > MAX_PATH_SAMPLES:
            return None
    return count


def _zone_polygons(zones: RestrictedZones):
    for _geometry_type, polygons in zones._geometries:
        for polygon in polygons:
            yield polygon


def _point_is_restricted(
    point: Tuple[float, float], zones: RestrictedZones
) -> bool:
    return any(
        _classify_polygon(point, polygon) != _ZONE_OUTSIDE
        for polygon in _zone_polygons(zones)
    )


def _segment_touches_zone_boundary(
    first: Tuple[float, float],
    second: Tuple[float, float],
    zones: RestrictedZones,
) -> bool:
    return any(
        _segments_touch(first, second, edge_start, edge_end)
        for polygon in _zone_polygons(zones)
        for ring in polygon
        for edge_start, edge_end in zip(ring, ring[1:])
    )


def _validate_path_zones(
    start: PathPoint,
    target: PathPoint,
    path: SamplePath,
    zones: RestrictedZones,
) -> Tuple[Optional[PathSafetyFailure], int, Optional[str]]:
    points = (
        (start._x_m, start._y_m),
        (target._x_m, target._y_m),
    ) + path._points
    segments = (
        ((start._x_m, start._y_m), path._points[0]),
        *tuple(zip(path._points, path._points[1:])),
        (path._points[-1], (target._x_m, target._y_m)),
    )
    candidate_count = zones.boundary_segment_count * (
        len(points) + len(segments)
    )
    if candidate_count > MAX_PATH_ZONE_CANDIDATES:
        return (
            _failure(
                PathSafetyFailureCode.PATH_ZONE_VALIDATION_BUDGET_EXCEEDED
            ),
            candidate_count,
            None,
        )
    if any(_point_is_restricted(point, zones) for point in points):
        return (
            _failure(PathSafetyFailureCode.PATH_RESTRICTED_ZONE),
            candidate_count,
            None,
        )
    for first, second in segments:
        if _segment_touches_zone_boundary(first, second, zones):
            return (
                _failure(PathSafetyFailureCode.PATH_ZONE_BOUNDARY_CONTACT),
                candidate_count,
                None,
            )
    trace_digest = _digest(
        'restricted-zone-validation',
        (
            zones.digest,
            start.digest,
            target.digest,
            path.digest,
            zones.geometry_count,
            zones.polygon_count,
            zones.boundary_segment_count,
            candidate_count,
            'clear',
        ),
    )
    return None, candidate_count, trace_digest


def _failure(code: PathSafetyFailureCode) -> PathSafetyFailure:
    return PathSafetyFailure(code=code)


def validate_sample_path(
    *,
    start_point: PathPoint,
    target_point: PathPoint,
    target_binding_digest: str,
    operation_binding_digest: str,
    map_content_digest: str,
    semantic_content_digest: str,
    zones_digest: str,
    restricted_zones: RestrictedZones,
    costmap: MapCostGrid,
    static_clearance: StaticClearanceGrid,
    path: SamplePath,
) -> PathSafetyResult:
    """
    Validate one sampled path and return proof or a content-free code.

    ``zones_digest`` must identify the exact freshly reconstructed zone DTO.
    Every reference point, path point, and complete path segment is checked.
    """
    digests = (
        target_binding_digest,
        operation_binding_digest,
        map_content_digest,
        semantic_content_digest,
        zones_digest,
    )
    if not all(_valid_digest(value) for value in digests):
        return _failure(PathSafetyFailureCode.INVALID_BINDING_DIGEST)

    try:
        current_start = _rebuild_point_input(start_point)
    except NavigationSafetyInputError:
        return _failure(PathSafetyFailureCode.START_POINT_TAMPERED)
    try:
        current_target = _rebuild_point_input(target_point)
    except NavigationSafetyInputError:
        return _failure(PathSafetyFailureCode.TARGET_POINT_TAMPERED)
    try:
        current_zones = _rebuild_zones(restricted_zones)
    except NavigationSafetyInputError:
        return _failure(PathSafetyFailureCode.RESTRICTED_ZONES_TAMPERED)
    if zones_digest != current_zones.digest:
        return _failure(PathSafetyFailureCode.ZONES_DIGEST_MISMATCH)
    try:
        current_costmap = _rebuild_costmap(costmap)
    except NavigationSafetyInputError:
        return _failure(PathSafetyFailureCode.COSTMAP_TAMPERED)
    try:
        current_clearance = _rebuild_clearance(static_clearance)
    except NavigationSafetyInputError:
        return _failure(PathSafetyFailureCode.CLEARANCE_GRID_TAMPERED)
    try:
        current_path = _rebuild_path(path)
    except NavigationSafetyInputError:
        return _failure(PathSafetyFailureCode.PATH_TAMPERED)

    if not _aligned(current_costmap, current_clearance):
        return _failure(PathSafetyFailureCode.GRID_ALIGNMENT_MISMATCH)

    first = current_path._points[0]
    last = current_path._points[-1]
    start_gap = math.hypot(
        first[0] - current_start._x_m,
        first[1] - current_start._y_m,
    )
    target_gap = math.hypot(
        last[0] - current_target._x_m,
        last[1] - current_target._y_m,
    )
    if start_gap > MAX_ENDPOINT_GAP_M:
        return _failure(PathSafetyFailureCode.PATH_START_GAP_TOO_LARGE)
    if target_gap > MAX_ENDPOINT_GAP_M:
        return _failure(PathSafetyFailureCode.PATH_TARGET_GAP_TOO_LARGE)

    zone_failure, zone_candidate_count, zone_validation_digest = (
        _validate_path_zones(
            current_start,
            current_target,
            current_path,
            current_zones,
        )
    )
    if zone_failure is not None or zone_validation_digest is None:
        return zone_failure or _failure(PathSafetyFailureCode.INVALID_INPUT)

    sampled_count = _sample_count(
        current_path, current_costmap._resolution_m
    )
    if sampled_count is None:
        return _failure(
            PathSafetyFailureCode.PATH_SAMPLE_BUDGET_EXCEEDED
        )

    trace = hashlib.sha256()
    maximum_cost = 0
    minimum_clearance = math.inf
    emitted = 0

    def inspect_point(
        x_m: float,
        y_m: float,
        *,
        trace_sample: bool,
    ) -> Optional[PathSafetyFailure]:
        nonlocal emitted, maximum_cost, minimum_clearance
        cell = current_costmap.world_to_cell(x_m, y_m)
        if cell is None:
            return _failure(PathSafetyFailureCode.PATH_OFF_MAP)
        row, column = cell
        offset = row * current_costmap._width + column
        cost = current_costmap._costs[offset]
        clearance_m = current_clearance._clearances_m[offset]
        if trace_sample:
            encoded = _canonical_bytes((
                emitted,
                _float_token(x_m),
                _float_token(y_m),
                row,
                column,
                cost,
                _float_token(clearance_m),
            ))
            trace.update(len(encoded).to_bytes(4, 'big'))
            trace.update(encoded)
            emitted += 1
        maximum_cost = max(maximum_cost, cost)
        minimum_clearance = min(minimum_clearance, clearance_m)
        if cost >= TRAVERSABLE_COST_EXCLUSIVE:
            return _failure(PathSafetyFailureCode.PATH_COST_BLOCKED)
        if clearance_m < REQUIRED_PATH_CLEARANCE_M:
            return _failure(
                PathSafetyFailureCode.PATH_CLEARANCE_INSUFFICIENT
            )
        return None

    for reference in (current_start, current_target):
        failure = inspect_point(
            reference._x_m,
            reference._y_m,
            trace_sample=False,
        )
        if failure is not None:
            return failure

    first_point = current_path._points[0]
    failure = inspect_point(
        first_point[0], first_point[1], trace_sample=True
    )
    if failure is not None:
        return failure
    spacing = (
        current_costmap._resolution_m
        * SAMPLE_SPACING_RESOLUTION_FRACTION
    )
    for first, second in zip(
        current_path._points, current_path._points[1:]
    ):
        distance = math.hypot(
            second[0] - first[0], second[1] - first[1]
        )
        steps = max(1, math.ceil(distance / spacing))
        for index in range(1, steps + 1):
            ratio = index / steps
            failure = inspect_point(
                first[0] + (second[0] - first[0]) * ratio,
                first[1] + (second[1] - first[1]) * ratio,
                trace_sample=True,
            )
            if failure is not None:
                return failure

    profile = ServerSafetyProfile()
    profile_digest = profile.digest
    sample_trace_digest = trace.hexdigest()
    input_bundle_digest = _digest(
        'path-safety-input-bundle',
        (
            profile_digest,
            operation_binding_digest,
            target_binding_digest,
            map_content_digest,
            semantic_content_digest,
            zones_digest,
            current_start.digest,
            current_target.digest,
            current_costmap.digest,
            current_clearance.digest,
            current_path.digest,
            sample_trace_digest,
            zone_validation_digest,
        ),
    )
    proof_values = (
        'gazebo-monitor-room-path-safety-proof-v2',
        'single_planner_path_preflight',
        profile_digest,
        operation_binding_digest,
        target_binding_digest,
        map_content_digest,
        semantic_content_digest,
        zones_digest,
        current_start.digest,
        current_target.digest,
        current_costmap.digest,
        current_clearance.digest,
        current_path.digest,
        sample_trace_digest,
        zone_validation_digest,
        input_bundle_digest,
        current_path.point_count,
        emitted,
        current_zones.boundary_segment_count,
        zone_candidate_count,
        maximum_cost,
        _float_token(minimum_clearance),
        _float_token(start_gap),
        _float_token(target_gap),
        True,
        False,
        False,
        False,
        False,
    )
    return PathSafetyProof(
        schema_version='gazebo-monitor-room-path-safety-proof-v2',
        scope='single_planner_path_preflight',
        profile_digest=profile_digest,
        operation_binding_digest=operation_binding_digest,
        target_binding_digest=target_binding_digest,
        map_content_digest=map_content_digest,
        semantic_content_digest=semantic_content_digest,
        zones_digest=zones_digest,
        start_point_digest=current_start.digest,
        target_point_digest=current_target.digest,
        costmap_digest=current_costmap.digest,
        static_clearance_digest=current_clearance.digest,
        path_digest=current_path.digest,
        sample_trace_digest=sample_trace_digest,
        zone_validation_digest=zone_validation_digest,
        input_bundle_digest=input_bundle_digest,
        proof_digest=_digest('path-safety-proof', proof_values),
        path_point_count=current_path.point_count,
        sampled_point_count=emitted,
        zone_boundary_segment_count=(
            current_zones.boundary_segment_count
        ),
        zone_validation_candidate_count=zone_candidate_count,
        maximum_cost=maximum_cost,
        minimum_clearance_m=minimum_clearance,
        start_gap_m=start_gap,
        target_gap_m=target_gap,
        restricted_zone_validation_performed=True,
        authority_claimed=False,
        coverage_claimed=False,
        physical_execution_observed=False,
        viewer_observed=False,
    )
