"""
Resolve one trusted semantic room name to one neutral Nav2 target.

This module is intentionally limited to deterministic validation and target
binding.  It performs no file, network, ROS, planning, or execution I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import unicodedata
from typing import Any


USER_MAP_FORMAT = 'malbut-user-map-v1'
NAMED_NAVIGATION_FIXTURE_FORMAT = 'malbut-named-navigation-fixture/v1'
MAP_FRAME = 'map'
TARGET_SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_FEATURES = 1024
MAX_ROOMS = 512
MAX_NAME_LENGTH = 80
MAX_CATEGORY_LENGTH = 40
MAX_RING_POINTS = 2048
MAX_TOTAL_ROOM_POINTS = 20000
MAX_POLYGONS = 128
MAX_RINGS_PER_POLYGON = 128
MAX_ABS_COORDINATE = 10000.0

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_LOWER_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_USER_MAP_FIELDS = frozenset({
    'type',
    'format',
    'map_id',
    'map_revision',
    'legacy_map_ids',
    'frame_id',
    'generated_at',
    'source',
    'room_segmentation',
    'features',
    'fixture',
})

Point = tuple[float, float]
Ring = tuple[Point, ...]
Polygon = tuple[Ring, ...]
Polygons = tuple[Polygon, ...]


class NamedNavigationError(ValueError):
    """Fail-closed named-target validation or resolution error."""

    def __init__(self, code: str, message: str) -> None:
        """Create an error with a stable machine-readable code."""
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise NamedNavigationError(code, message)


def _identifier(value: Any, field_name: str, code: str) -> str:
    if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
        _fail(code, f'{field_name} is invalid')
    return value


def _normalized_text(
    value: Any,
    field_name: str,
    maximum_length: int,
    code: str,
) -> str:
    if type(value) is not str:
        _fail(code, f'{field_name} must be a string')
    normalized = ' '.join(unicodedata.normalize('NFKC', value).split())
    if not normalized or len(normalized) > maximum_length:
        _fail(code, f'{field_name} is invalid')
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in normalized
    ):
        _fail(code, f'{field_name} contains control characters')
    return normalized


def _lookup_key(value: Any, code: str) -> str:
    return _normalized_text(
        value,
        'location',
        MAX_NAME_LENGTH,
        code,
    ).casefold()


def _validate_json_value(value: Any, depth: int = 0) -> None:
    if depth > 32:
        _fail('invalid_user_map', 'User Map is nested too deeply')
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail('invalid_user_map', 'User Map contains a non-finite number')
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            _fail('invalid_user_map', 'User Map keys must be strings')
        for item in value.values():
            _validate_json_value(item, depth + 1)
        return
    _fail('invalid_user_map', 'User Map contains a non-JSON value')


def _canonical_json(value: Any) -> str:
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
        encoded.encode('utf-8')
        return encoded
    except (OverflowError, TypeError, UnicodeEncodeError, ValueError):
        _fail('invalid_user_map', 'User Map cannot be canonicalized')
    raise AssertionError('unreachable')


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _finite_number(
    value: Any,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    if type(value) not in {int, float}:
        _fail('invalid_user_map', f'{field_name} must be a number')
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        _fail('invalid_user_map', f'{field_name} is outside the valid range')
    return 0.0 if result == 0.0 else result


def _point(value: Any, field_name: str) -> Point:
    if type(value) is not list or len(value) != 2:
        _fail(
            'invalid_user_map',
            f'{field_name} must be an exact [x, y] point',
        )
    return (
        _finite_number(
            value[0],
            f'{field_name}[0]',
            -MAX_ABS_COORDINATE,
            MAX_ABS_COORDINATE,
        ),
        _finite_number(
            value[1],
            f'{field_name}[1]',
            -MAX_ABS_COORDINATE,
            MAX_ABS_COORDINATE,
        ),
    )


def _orientation(first: Point, second: Point, third: Point) -> float:
    return (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )


def _point_on_segment(point: Point, first: Point, second: Point) -> bool:
    epsilon = 1e-9
    return (
        abs(_orientation(first, second, point)) <= epsilon
        and min(first[0], second[0]) - epsilon
        <= point[0]
        <= max(first[0], second[0]) + epsilon
        and min(first[1], second[1]) - epsilon
        <= point[1]
        <= max(first[1], second[1]) + epsilon
    )


def _properly_intersects(
    first: Point,
    second: Point,
    third: Point,
    fourth: Point,
) -> bool:
    one = _orientation(first, second, third)
    two = _orientation(first, second, fourth)
    three = _orientation(third, fourth, first)
    four = _orientation(third, fourth, second)
    return (
        ((one > 0 and two < 0) or (one < 0 and two > 0))
        and ((three > 0 and four < 0) or (three < 0 and four > 0))
    )


def _rings_intersect(first: Ring, second: Ring) -> bool:
    return any(
        _properly_intersects(one, two, three, four)
        for one, two in zip(first, first[1:])
        for three, four in zip(second, second[1:])
    )


def _ring_self_intersects(ring: Ring) -> bool:
    edge_count = len(ring) - 1
    for first in range(edge_count):
        for second in range(first + 1, edge_count):
            adjacent = (
                abs(first - second) == 1
                or (first == 0 and second == edge_count - 1)
            )
            if adjacent:
                continue
            if _properly_intersects(
                ring[first],
                ring[first + 1],
                ring[second],
                ring[second + 1],
            ):
                return True
    return False


def _ring_area(ring: Ring) -> float:
    return abs(sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(ring, ring[1:])
    )) / 2.0


def _point_in_ring(point: Point, ring: Ring) -> bool:
    if any(
        _point_on_segment(point, first, second)
        for first, second in zip(ring, ring[1:])
    ):
        return False
    x, y = point
    inside = False
    for first, second in zip(ring, ring[1:]):
        if (first[1] > y) == (second[1] > y):
            continue
        crossing_x = (
            (second[0] - first[0])
            * (y - first[1])
            / (second[1] - first[1])
            + first[0]
        )
        if x < crossing_x:
            inside = not inside
    return inside


def _point_in_polygon(point: Point, polygon: Polygon) -> bool:
    if not _point_in_ring(point, polygon[0]):
        return False
    return not any(
        _point_in_ring(point, hole)
        or any(
            _point_on_segment(point, first, second)
            for first, second in zip(hole, hole[1:])
        )
        for hole in polygon[1:]
    )


def _parse_ring(value: Any, field_name: str, point_budget: list[int]) -> Ring:
    if (
        type(value) is not list
        or len(value) < 4
        or len(value) > MAX_RING_POINTS
    ):
        _fail('invalid_user_map', f'{field_name} has an invalid point count')
    ring = tuple(
        _point(item, f'{field_name}[{index}]')
        for index, item in enumerate(value)
    )
    point_budget[0] += len(ring)
    if point_budget[0] > MAX_TOTAL_ROOM_POINTS:
        _fail('invalid_user_map', 'User Map contains too many room points')
    if ring[0] != ring[-1]:
        _fail('invalid_user_map', f'{field_name} must be closed')
    if any(first == second for first, second in zip(ring, ring[1:])):
        _fail('invalid_user_map', f'{field_name} has a zero-length edge')
    if _ring_self_intersects(ring):
        _fail('invalid_user_map', f'{field_name} self-intersects')
    if len(set(ring[:-1])) < 3 or _ring_area(ring) <= 1e-8:
        _fail('invalid_user_map', f'{field_name} has no usable area')
    return ring


def _parse_polygon(
    value: Any,
    field_name: str,
    point_budget: list[int],
) -> Polygon:
    if (
        type(value) is not list
        or not value
        or len(value) > MAX_RINGS_PER_POLYGON
    ):
        _fail('invalid_user_map', f'{field_name} has invalid rings')
    rings = tuple(
        _parse_ring(item, f'{field_name}[{index}]', point_budget)
        for index, item in enumerate(value)
    )
    outer = rings[0]
    for index, hole in enumerate(rings[1:], start=1):
        if not _point_in_ring(hole[0], outer):
            _fail(
                'invalid_user_map',
                f'{field_name}[{index}] is outside its outer ring',
            )
        if _rings_intersect(outer, hole):
            _fail(
                'invalid_user_map',
                f'{field_name}[{index}] intersects its outer ring',
            )
    holes = rings[1:]
    for first_index, first in enumerate(holes):
        for second in holes[first_index + 1:]:
            if (
                _rings_intersect(first, second)
                or _point_in_ring(first[0], second)
                or _point_in_ring(second[0], first)
            ):
                _fail('invalid_user_map', f'{field_name} holes overlap')
    return rings


def _parse_geometry(value: Any, point_budget: list[int]) -> tuple[
    str,
    str,
    float,
    Polygons,
]:
    if type(value) is not dict or set(value) != {'type', 'coordinates'}:
        _fail(
            'invalid_user_map',
            'room.geometry must contain only type and coordinates',
        )
    geometry_type = value.get('type')
    coordinates = value.get('coordinates')
    if geometry_type == 'Polygon':
        raw_polygons = [coordinates]
    elif geometry_type == 'MultiPolygon':
        if (
            type(coordinates) is not list
            or not coordinates
            or len(coordinates) > MAX_POLYGONS
        ):
            _fail(
                'invalid_user_map',
                'room MultiPolygon has an invalid polygon count',
            )
        raw_polygons = coordinates
    else:
        _fail(
            'invalid_user_map',
            'room geometry must be Polygon or MultiPolygon',
        )
    polygons = tuple(
        _parse_polygon(
            polygon,
            f'room.geometry.coordinates[{index}]',
            point_budget,
        )
        for index, polygon in enumerate(raw_polygons)
    )
    for first_index, first in enumerate(polygons):
        for second in polygons[first_index + 1:]:
            if (
                _rings_intersect(first[0], second[0])
                or _point_in_polygon(first[0][0], second)
                or _point_in_polygon(second[0][0], first)
            ):
                _fail('invalid_user_map', 'room MultiPolygon parts overlap')
    area = sum(
        _ring_area(polygon[0])
        - sum(_ring_area(hole) for hole in polygon[1:])
        for polygon in polygons
    )
    if area <= 1e-8:
        _fail('invalid_user_map', 'room geometry has no usable interior')
    canonical_coordinates = [
        [
            [[point[0], point[1]] for point in ring]
            for ring in polygon
        ]
        for polygon in polygons
    ]
    canonical_geometry = {
        'type': geometry_type,
        'coordinates': (
            canonical_coordinates[0]
            if geometry_type == 'Polygon'
            else canonical_coordinates
        ),
    }
    geometry_json = _canonical_json(canonical_geometry)
    return (
        geometry_json,
        hashlib.sha256(geometry_json.encode('utf-8')).hexdigest(),
        area,
        polygons,
    )


@dataclass(frozen=True)
class _SemanticRoom:
    room_id: str
    room_name: str
    name_key: str
    room_category: str
    geometry_digest: str
    representative_point: Point
    clearance_m: float
    area_m2: float

    def semantic_value(self) -> dict[str, Any]:
        return {
            'room_id': self.room_id,
            'room_name': self.room_name,
            'room_category': self.room_category,
            'geometry_digest': self.geometry_digest,
            'representative_point': list(self.representative_point),
            'clearance_m': self.clearance_m,
            'area_m2': self.area_m2,
        }


def _parse_room(value: Any, point_budget: list[int]) -> _SemanticRoom:
    if type(value) is not dict or value.get('type') != 'Feature':
        _fail('invalid_user_map', 'every room must be a GeoJSON Feature')
    if set(value) - {'type', 'id', 'properties', 'geometry'}:
        _fail('invalid_user_map', 'room has unsupported top-level fields')
    properties = value.get('properties')
    if type(properties) is not dict or properties.get('role') != 'room':
        _fail('invalid_user_map', 'every room must have the room role')
    room_id = _identifier(
        value.get('id'),
        'room.id',
        'invalid_user_map',
    )
    property_room_id = _identifier(
        properties.get('room_id'),
        'room.properties.room_id',
        'invalid_user_map',
    )
    if room_id != property_room_id:
        _fail('invalid_user_map', 'room ID fields do not match')
    room_name = _normalized_text(
        properties.get('name'),
        'room.properties.name',
        MAX_NAME_LENGTH,
        'invalid_user_map',
    )
    category = _normalized_text(
        properties.get('category'),
        'room.properties.category',
        MAX_CATEGORY_LENGTH,
        'invalid_user_map',
    )
    _, geometry_digest, geometry_area, polygons = _parse_geometry(
        value.get('geometry'),
        point_budget,
    )
    representative_point = _point(
        properties.get('representative_point'),
        'room.properties.representative_point',
    )
    if not any(
        _point_in_polygon(representative_point, polygon)
        for polygon in polygons
    ):
        _fail(
            'invalid_user_map',
            'room representative point must be strictly inside its geometry',
        )
    clearance_m = _finite_number(
        properties.get('clearance_m'),
        'room.properties.clearance_m',
        0.000001,
        MAX_ABS_COORDINATE,
    )
    area_m2 = _finite_number(
        properties.get('area_m2'),
        'room.properties.area_m2',
        0.000001,
        MAX_ABS_COORDINATE * MAX_ABS_COORDINATE,
    )
    if abs(area_m2 - round(geometry_area, 2)) > 0.011:
        _fail('invalid_user_map', 'room area does not match its geometry')
    return _SemanticRoom(
        room_id=room_id,
        room_name=room_name,
        name_key=room_name.casefold(),
        room_category=category,
        geometry_digest=geometry_digest,
        representative_point=representative_point,
        clearance_m=clearance_m,
        area_m2=area_m2,
    )


@dataclass(frozen=True)
class NamedNavigationTarget:
    """Immutable private target for a later confirmation and Nav2 adapter."""

    device_id: str
    map_id: str
    map_revision: str
    semantic_digest: str
    source_digest: str = field(repr=False)
    frame_id: str
    room_id: str
    room_name: str
    room_category: str
    x: float
    y: float
    yaw: float = 0.0
    schema_version: int = TARGET_SCHEMA_VERSION
    _binding_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Revalidate all execution-relevant fields and bind them."""
        if type(self.schema_version) is not int or (
            self.schema_version != TARGET_SCHEMA_VERSION
        ):
            _fail('invalid_target', 'target schema version is invalid')
        for name in ('device_id', 'map_id', 'map_revision', 'room_id'):
            _identifier(getattr(self, name), name, 'invalid_target')
        if self.frame_id != MAP_FRAME:
            _fail('invalid_target', 'target frame must be map')
        if (
            type(self.semantic_digest) is not str
            or _LOWER_SHA256.fullmatch(self.semantic_digest) is None
        ):
            _fail('invalid_target', 'semantic digest is invalid')
        if (
            type(self.source_digest) is not str
            or _LOWER_SHA256.fullmatch(self.source_digest) is None
        ):
            _fail('invalid_target', 'source digest is invalid')
        room_name = _normalized_text(
            self.room_name,
            'room_name',
            MAX_NAME_LENGTH,
            'invalid_target',
        )
        room_category = _normalized_text(
            self.room_category,
            'room_category',
            MAX_CATEGORY_LENGTH,
            'invalid_target',
        )
        x = _target_number(self.x, 'x')
        y = _target_number(self.y, 'y')
        yaw = _target_number(self.yaw, 'yaw')
        if yaw != 0.0:
            _fail('invalid_target', 'the fixed named target yaw must be zero')
        object.__setattr__(self, 'room_name', room_name)
        object.__setattr__(self, 'room_category', room_category)
        object.__setattr__(self, 'x', x)
        object.__setattr__(self, 'y', y)
        object.__setattr__(self, 'yaw', yaw)
        object.__setattr__(
            self,
            '_binding_digest',
            _digest(self._binding_value()),
        )

    @property
    def binding_digest(self) -> str:
        """Return the complete private target binding digest."""
        return self._binding_digest

    def _binding_value(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'device_id': self.device_id,
            'map_id': self.map_id,
            'map_revision': self.map_revision,
            'semantic_digest': self.semantic_digest,
            'user_map_digest': self.source_digest,
            'frame_id': self.frame_id,
            'room_id': self.room_id,
            'room_name': self.room_name,
            'room_category': self.room_category,
            'x': self.x,
            'y': self.y,
            'yaw': self.yaw,
        }

    def to_private_dict(self) -> dict[str, Any]:
        """Return the server-only target record used by later stages."""
        return {
            **self._binding_value(),
            'binding_digest': self.binding_digest,
            'execution_authorized': False,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return a confirmation-safe summary without device or coordinates."""
        return {
            'schema_version': self.schema_version,
            'room_name': self.room_name,
            'room_category': self.room_category,
            'execution_authorized': False,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the public representation by default."""
        return self.to_public_dict()


def _target_number(value: Any, field_name: str) -> float:
    if type(value) not in {int, float}:
        _fail('invalid_target', f'target {field_name} must be a number')
    result = float(value)
    if not math.isfinite(result) or abs(result) > MAX_ABS_COORDINATE:
        _fail('invalid_target', f'target {field_name} is invalid')
    return 0.0 if result == 0.0 else result


@dataclass(frozen=True)
class NamedNavigationCatalog:
    """Immutable semantic room catalog bound to one device and map."""

    device_id: str
    map_id: str
    map_revision: str
    semantic_digest: str
    source_digest: str = field(repr=False)
    frame_id: str
    _rooms: tuple[_SemanticRoom, ...] = field(repr=False)

    @property
    def room_count(self) -> int:
        """Return the number of validated semantic rooms."""
        return len(self._rooms)

    def resolve(self, location: Any) -> NamedNavigationTarget:
        """Resolve exactly one normalized room name or fail closed."""
        key = _lookup_key(location, 'target_not_found')
        matches = tuple(room for room in self._rooms if room.name_key == key)
        if not matches:
            _fail('target_not_found', 'no semantic room matches the location')
        if len(matches) > 1:
            _fail('target_ambiguous', 'multiple semantic rooms match location')
        room = matches[0]
        return NamedNavigationTarget(
            device_id=self.device_id,
            map_id=self.map_id,
            map_revision=self.map_revision,
            semantic_digest=self.semantic_digest,
            source_digest=self.source_digest,
            frame_id=self.frame_id,
            room_id=room.room_id,
            room_name=room.room_name,
            room_category=room.room_category,
            x=room.representative_point[0],
            y=room.representative_point[1],
        )


def parse_named_navigation_catalog(
    user_map: Any,
    *,
    device_id: str,
    expected_map_id: str,
    expected_map_revision: str,
    source_digest: str | None = None,
) -> NamedNavigationCatalog:
    """Parse one finalized User Map into a pure named-target catalog."""
    device_id = _identifier(device_id, 'device_id', 'invalid_device')
    expected_map_id = _identifier(
        expected_map_id,
        'expected_map_id',
        'invalid_identity',
    )
    expected_map_revision = _identifier(
        expected_map_revision,
        'expected_map_revision',
        'invalid_identity',
    )
    if type(user_map) is not dict:
        _fail('invalid_user_map', 'User Map must be an object')
    canonical_user_map = _canonical_json(user_map)
    if len(canonical_user_map.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
        _fail('invalid_user_map', 'User Map exceeds the supported size')
    if source_digest is None:
        source_digest = hashlib.sha256(
            canonical_user_map.encode('utf-8')
        ).hexdigest()
    elif (
        type(source_digest) is not str
        or _LOWER_SHA256.fullmatch(source_digest) is None
    ):
        _fail('invalid_identity', 'source_digest is invalid')
    if set(user_map) - _USER_MAP_FIELDS:
        _fail('invalid_user_map', 'User Map contains unsupported fields')
    required = {
        'type',
        'format',
        'map_id',
        'map_revision',
        'frame_id',
        'features',
    }
    if not required.issubset(user_map):
        _fail('invalid_user_map', 'User Map is missing required fields')
    if (
        user_map.get('type') != 'FeatureCollection'
        or user_map.get('format') != USER_MAP_FORMAT
    ):
        _fail('invalid_user_map', 'User Map format is unsupported')
    if user_map.get('frame_id') != MAP_FRAME:
        _fail('identity_mismatch', 'User Map frame must be map')
    if (
        user_map.get('map_id') != expected_map_id
        or user_map.get('map_revision') != expected_map_revision
    ):
        _fail('identity_mismatch', 'User Map identity does not match')
    legacy_ids = user_map.get('legacy_map_ids', [])
    if (
        type(legacy_ids) is not list
        or len(legacy_ids) > 32
        or any(
            type(item) is not str
            or _SAFE_IDENTIFIER.fullmatch(item) is None
            for item in legacy_ids
        )
    ):
        _fail('invalid_user_map', 'User Map legacy IDs are invalid')
    fixture = user_map.get('fixture')
    if fixture is not None:
        if (
            type(fixture) is not dict
            or set(fixture) != {'format', 'device_id', 'purpose'}
            or fixture.get('device_id') != device_id
            or fixture.get('format') != NAMED_NAVIGATION_FIXTURE_FORMAT
        ):
            _fail('identity_mismatch', 'User Map fixture identity is invalid')
        _normalized_text(
            fixture.get('purpose'),
            'fixture.purpose',
            160,
            'invalid_user_map',
        )
    features = user_map.get('features')
    if (
        type(features) is not list
        or not features
        or len(features) > MAX_FEATURES
    ):
        _fail('invalid_user_map', 'User Map features are missing or unbounded')
    room_features = []
    for feature in features:
        if type(feature) is not dict or feature.get('type') != 'Feature':
            _fail('invalid_user_map', 'User Map features must be Features')
        properties = feature.get('properties')
        if type(properties) is not dict:
            _fail(
                'invalid_user_map',
                'User Map feature properties are invalid',
            )
        if properties.get('role') == 'room':
            room_features.append(feature)
    if not room_features or len(room_features) > MAX_ROOMS:
        _fail('invalid_user_map', 'User Map room count is invalid')
    point_budget = [0]
    rooms = tuple(
        _parse_room(room, point_budget) for room in room_features
    )
    room_ids = [room.room_id for room in rooms]
    if len(room_ids) != len(set(room_ids)):
        _fail('invalid_user_map', 'User Map room IDs must be unique')
    segmentation = user_map.get('room_segmentation')
    if segmentation is not None:
        if type(segmentation) is not dict:
            _fail('invalid_user_map', 'room_segmentation must be an object')
        room_count = segmentation.get('room_count')
        if type(room_count) is not int or room_count != len(rooms):
            _fail(
                'invalid_user_map',
                'room_segmentation count does not match rooms',
            )
    semantic_digest = _digest({
        'schema_version': TARGET_SCHEMA_VERSION,
        'map_id': expected_map_id,
        'map_revision': expected_map_revision,
        'frame_id': MAP_FRAME,
        'rooms': [
            room.semantic_value()
            for room in sorted(rooms, key=lambda item: item.room_id)
        ],
    })
    return NamedNavigationCatalog(
        device_id=device_id,
        map_id=expected_map_id,
        map_revision=expected_map_revision,
        semantic_digest=semantic_digest,
        source_digest=source_digest,
        frame_id=MAP_FRAME,
        _rooms=rooms,
    )


def resolve_named_navigation_target(
    catalog: Any,
    location: Any,
) -> NamedNavigationTarget:
    """Resolve through a validated catalog without performing side effects."""
    if type(catalog) is not NamedNavigationCatalog:
        _fail(
            'invalid_catalog',
            'a validated named-navigation catalog is required',
        )
    return catalog.resolve(location)


__all__ = [
    'NamedNavigationCatalog',
    'NamedNavigationError',
    'NamedNavigationTarget',
    'parse_named_navigation_catalog',
    'resolve_named_navigation_target',
]
