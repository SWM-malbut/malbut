"""
Trusted semantic-room target binding for ``monitor_room`` proposals.

The module deliberately performs no HTTP, ROS, confirmation, or execution.
An authenticated adapter selects the device and proves that a Homecam
semantic response came from the finalized map row.  This module then parses
that response as untrusted data and creates an immutable, content-bound room
target suitable for a later confirmation schema.
"""

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Tuple

from malbut_agent_server.schemas import ValidationError


TARGET_BINDING_SCHEMA_VERSION = 1
EFFECTS_SCHEMA_VERSION = 1
GAZEBO_SIMULATION_EFFECTS_SCHEMA_VERSION = 2
USER_MAP_FORMAT = 'malbut-user-map-v1'
SEMANTIC_ZONES_FORMAT = 'malbut-semantic-zones-v1'
MAP_FRAME = 'map'

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_FEATURES = 1024
MAX_ROOMS = 512
MAX_TOTAL_ROOM_POINTS = 20000
MAX_RING_POINTS = 2048
MAX_POLYGONS = 128
MAX_RINGS_PER_POLYGON = 128
MAX_NAME_LENGTH = 40
MAX_LOCATION_LENGTH = 80
MAX_DURATION_SECONDS = 3600
MAX_ABS_COORDINATE = 10000.0

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_ROOM_CATEGORIES = frozenset(
    {
        'unassigned',
        'living_room',
        'bedroom',
        'kitchen',
        'dining_room',
        'bathroom',
        'entrance',
        'hallway',
        'workspace',
        'storage',
        'utility',
        'custom',
    }
)
_CATEGORY_ALIASES = {
    'living_room': ('living_room', 'living room', '거실'),
    'bedroom': ('bedroom', '침실'),
    'kitchen': ('kitchen', '주방', '부엌'),
    'dining_room': ('dining_room', 'dining room', '식당'),
    'bathroom': ('bathroom', '욕실', '화장실'),
    'entrance': ('entrance', 'entryway', '현관'),
    'hallway': ('hallway', 'corridor', '복도'),
    'workspace': ('workspace', 'study', '작업 공간', '작업실', '서재'),
    'storage': ('storage', '수납 공간', '창고'),
    'utility': ('utility', 'utility room', '다용도실'),
}
_NORMALIZED_CATEGORY_ALIASES = {
    _alias: category
    for category, aliases in _CATEGORY_ALIASES.items()
    for _alias in (
        unicodedata.normalize('NFKC', alias).casefold()
        for alias in aliases
    )
}

_HOME_CAM_FIELDS = frozenset(
    {'revision', 'mapId', 'mapRevision', 'userMap', 'zones'}
)
_USER_MAP_FIELDS = frozenset(
    {
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
    }
)


class TargetResolutionError(ValidationError):
    """Fail-closed semantic snapshot or room-resolution error."""

    def __init__(self, code: str, message: str) -> None:
        """Create one stable machine-readable failure."""
        super().__init__(message)
        self.code = code


def _fail(message: str) -> None:
    raise TargetResolutionError('invalid_semantic_snapshot', message)


def _binding_fail(message: str) -> None:
    raise TargetResolutionError('invalid_target_binding', message)


def _binding_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        _binding_fail(f'{field_name} is invalid')
    return value


def _sha256_string(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        _binding_fail(f'{field_name} must be a lowercase SHA-256 digest')
    return value


def _canonical_json(value: Any) -> str:
    """Return bounded canonical JSON after rejecting non-JSON values."""
    _validate_json_value(value, 0)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        _fail('semantic snapshot contains a non-JSON value')
    raise AssertionError('unreachable')


def _validate_json_value(value: Any, depth: int) -> None:
    if depth > 32:
        _fail('semantic snapshot is nested too deeply')
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail('semantic snapshot contains a non-finite number')
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            _fail('semantic snapshot object keys must be strings')
        for item in value.values():
            _validate_json_value(item, depth + 1)
        return
    _fail('semantic snapshot contains a non-JSON value')


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        _fail(f'{field_name} is invalid')
    return value


def _normalized_text(value: Any, field_name: str, limit: int) -> str:
    if not isinstance(value, str):
        _fail(f'{field_name} must be a string')
    normalized = unicodedata.normalize('NFKC', value)
    normalized = ' '.join(normalized.split())
    if not normalized or len(normalized) > limit:
        _fail(f'{field_name} is invalid')
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in normalized
    ):
        _fail(f'{field_name} contains control characters')
    return normalized


def _lookup_key(value: Any) -> str:
    if not isinstance(value, str):
        raise TargetResolutionError(
            'target_not_found',
            'monitor_room location must be a string',
        )
    normalized = unicodedata.normalize('NFKC', value)
    normalized = ' '.join(normalized.split()).casefold()
    if (
        not normalized
        or len(normalized) > MAX_LOCATION_LENGTH
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        )
    ):
        raise TargetResolutionError(
            'target_not_found',
            'monitor_room location is invalid',
        )
    return normalized


def _finite_number(
    value: Any,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f'{field_name} must be a number')
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        _fail(f'{field_name} is outside the supported range')
    if result == 0:
        return 0.0
    return result


def _point(value: Any, field_name: str) -> Tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        _fail(f'{field_name} must be an exact [x, y] point')
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


def _ring_area(ring: Tuple[Tuple[float, float], ...]) -> float:
    return abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(ring, ring[1:])
        )
    ) / 2.0


def _orientation(
    first: Tuple[float, float],
    second: Tuple[float, float],
    third: Tuple[float, float],
) -> float:
    return (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )


def _point_on_segment(
    point: Tuple[float, float],
    first: Tuple[float, float],
    second: Tuple[float, float],
) -> bool:
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


def _segments_properly_intersect(
    first: Tuple[float, float],
    second: Tuple[float, float],
    third: Tuple[float, float],
    fourth: Tuple[float, float],
) -> bool:
    one = _orientation(first, second, third)
    two = _orientation(first, second, fourth)
    three = _orientation(third, fourth, first)
    four = _orientation(third, fourth, second)
    return (
        ((one > 0 and two < 0) or (one < 0 and two > 0))
        and ((three > 0 and four < 0) or (three < 0 and four > 0))
    )


def _ring_self_intersects(
    ring: Tuple[Tuple[float, float], ...],
) -> bool:
    edge_count = len(ring) - 1
    for first in range(edge_count):
        for second in range(first + 1, edge_count):
            adjacent = (
                abs(first - second) == 1
                or (first == 0 and second == edge_count - 1)
            )
            if adjacent:
                continue
            # The current occupancy-mask builder can retrace a boundary at a
            # one-cell bottleneck.  That self-touch does not cross interior
            # area, whereas a proper crossing makes room membership
            # ambiguous and must fail closed.
            if _segments_properly_intersect(
                ring[first],
                ring[first + 1],
                ring[second],
                ring[second + 1],
            ):
                return True
    return False


def _rings_properly_intersect(
    first: Tuple[Tuple[float, float], ...],
    second: Tuple[Tuple[float, float], ...],
) -> bool:
    return any(
        _segments_properly_intersect(one, two, three, four)
        for one, two in zip(first, first[1:])
        for three, four in zip(second, second[1:])
    )


def _point_in_ring(
    point: Tuple[float, float],
    ring: Tuple[Tuple[float, float], ...],
) -> bool:
    if any(
        _point_on_segment(point, first, second)
        for first, second in zip(ring, ring[1:])
    ):
        return False
    inside = False
    x, y = point
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


def _point_in_polygon(
    point: Tuple[float, float],
    polygon: Tuple[Tuple[Tuple[float, float], ...], ...],
) -> bool:
    if not _point_in_ring(point, polygon[0]):
        return False
    for hole in polygon[1:]:
        if any(
            _point_on_segment(point, first, second)
            for first, second in zip(hole, hole[1:])
        ) or _point_in_ring(point, hole):
            return False
    return True


def _point_in_geometry(
    point: Tuple[float, float],
    polygons: Tuple[
        Tuple[Tuple[Tuple[float, float], ...], ...], ...
    ],
) -> bool:
    return any(_point_in_polygon(point, polygon) for polygon in polygons)


def _parse_ring(
    value: Any,
    field_name: str,
    point_budget: list,
) -> Tuple[Tuple[float, float], ...]:
    if (
        not isinstance(value, list)
        or len(value) < 4
        or len(value) > MAX_RING_POINTS
    ):
        _fail(f'{field_name} has an unsupported point count')
    ring = tuple(
        _point(item, f'{field_name}[{index}]')
        for index, item in enumerate(value)
    )
    point_budget[0] += len(ring)
    if point_budget[0] > MAX_TOTAL_ROOM_POINTS:
        _fail('semantic snapshot contains too many room points')
    if ring[0] != ring[-1]:
        _fail(f'{field_name} is not closed')
    if len(set(ring[:-1])) < 3 or _ring_area(ring) <= 1e-8:
        _fail(f'{field_name} has no usable area')
    if any(first == second for first, second in zip(ring, ring[1:])):
        _fail(f'{field_name} contains a zero-length edge')
    if _ring_self_intersects(ring):
        _fail(f'{field_name} self-intersects')
    return ring


def _parse_polygon(
    value: Any,
    field_name: str,
    point_budget: list,
) -> Tuple[Tuple[Tuple[float, float], ...], ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_RINGS_PER_POLYGON
    ):
        _fail(f'{field_name} must contain bounded polygon rings')
    rings = tuple(
        _parse_ring(item, f'{field_name}[{index}]', point_budget)
        for index, item in enumerate(value)
    )
    outer = rings[0]
    for index, hole in enumerate(rings[1:], start=1):
        if not _point_in_ring(hole[0], outer):
            _fail(f'{field_name}[{index}] is not inside its outer ring')
        if _rings_properly_intersect(outer, hole):
            _fail(f'{field_name}[{index}] intersects its outer ring')
    holes = rings[1:]
    for first_index, first in enumerate(holes):
        for second in holes[first_index + 1:]:
            if (
                _rings_properly_intersect(first, second)
                or _point_in_ring(first[0], second)
                or _point_in_ring(second[0], first)
            ):
                _fail(f'{field_name} contains overlapping holes')
    area = _ring_area(outer) - sum(_ring_area(hole) for hole in holes)
    if area <= 1e-8:
        _fail(f'{field_name} has no usable interior')
    return rings


def _polygons_overlap(
    first: Tuple[Tuple[Tuple[float, float], ...], ...],
    second: Tuple[Tuple[Tuple[float, float], ...], ...],
) -> bool:
    if _rings_properly_intersect(first[0], second[0]):
        return True
    return (
        _point_in_polygon(first[0][0], second)
        or _point_in_polygon(second[0][0], first)
    )


def _geometry_value(
    geometry_type: str,
    polygons: Tuple[
        Tuple[Tuple[Tuple[float, float], ...], ...], ...
    ],
) -> Dict[str, Any]:
    coordinates = [
        [
            [[point[0], point[1]] for point in ring]
            for ring in polygon
        ]
        for polygon in polygons
    ]
    return {
        'type': geometry_type,
        'coordinates': coordinates[0]
        if geometry_type == 'Polygon'
        else coordinates,
    }


def _parse_geometry(
    value: Any,
    point_budget: list,
) -> Tuple[
    str,
    str,
    float,
    Tuple[Tuple[Tuple[Tuple[float, float], ...], ...], ...],
]:
    if not isinstance(value, dict) or set(value) != {'type', 'coordinates'}:
        _fail('room geometry must contain only type and coordinates')
    geometry_type = value.get('type')
    coordinates = value.get('coordinates')
    if geometry_type == 'Polygon':
        raw_polygons = [coordinates]
    elif geometry_type == 'MultiPolygon':
        if (
            not isinstance(coordinates, list)
            or not coordinates
            or len(coordinates) > MAX_POLYGONS
        ):
            _fail('room MultiPolygon has an unsupported polygon count')
        raw_polygons = coordinates
    else:
        _fail('room geometry must be Polygon or MultiPolygon')
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
            if _polygons_overlap(first, second):
                _fail('room MultiPolygon parts overlap')
    area = sum(
        _ring_area(polygon[0])
        - sum(_ring_area(hole) for hole in polygon[1:])
        for polygon in polygons
    )
    canonical = _geometry_value(geometry_type, polygons)
    geometry_json = _canonical_json(canonical)
    geometry_digest = hashlib.sha256(
        geometry_json.encode('utf-8')
    ).hexdigest()
    return geometry_json, geometry_digest, area, polygons


@dataclass(frozen=True)
class Effects:
    """Explicit monitor-room effects bound to a user confirmation."""

    physical_navigation: bool
    camera_capture: bool
    external_video_stream: bool
    video_recording: bool
    audio_capture: bool
    max_duration_seconds: int
    coverage_mode: str
    viewer_scope: str
    talkback_allowed: bool
    schema_version: int = EFFECTS_SCHEMA_VERSION
    _canonical: str = field(init=False, repr=False)
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Reject implicit truthiness and unbounded mission durations."""
        names = (
            'physical_navigation',
            'camera_capture',
            'external_video_stream',
            'video_recording',
            'audio_capture',
            'talkback_allowed',
        )
        if any(type(getattr(self, name)) is not bool for name in names):
            raise ValidationError('monitor_room effects must be booleans')
        if (
            type(self.schema_version) is not int
            or self.schema_version not in {
                EFFECTS_SCHEMA_VERSION,
                GAZEBO_SIMULATION_EFFECTS_SCHEMA_VERSION,
            }
        ):
            raise ValidationError('monitor_room effects schema is unsupported')
        duration = self.max_duration_seconds
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration < 1
            or duration > MAX_DURATION_SECONDS
        ):
            raise ValidationError(
                'max_duration_seconds must be from 1 to 3600'
            )
        if self.coverage_mode != 'whole_room':
            raise ValidationError(
                'monitor_room coverage_mode must be whole_room'
            )
        if self.viewer_scope != 'requesting_user':
            raise ValidationError(
                'monitor_room viewer_scope must be requesting_user'
            )
        if self.talkback_allowed is not False:
            raise ValidationError(
                'monitor_room talkback must remain disabled'
            )
        if self.schema_version == GAZEBO_SIMULATION_EFFECTS_SCHEMA_VERSION:
            # V2 is deliberately a single closed profile.  The simulated
            # robot will navigate, but none of the physical robot or Homecam
            # media controls are authorized.  The schema version is stored
            # in the confirmation row and included in the effects digest, so
            # this meaning survives approval, replay, and process restart.
            if (
                self.physical_navigation is not False
                or self.camera_capture is not False
                or self.external_video_stream is not False
                or self.video_recording is not False
                or self.audio_capture is not False
            ):
                raise ValidationError(
                    'Gazebo simulation effects cannot authorize physical '
                    'or Homecam actions'
                )
        canonical = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(',', ':'),
        )
        object.__setattr__(self, '_canonical', canonical)
        object.__setattr__(
            self,
            '_digest',
            hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
        )

    @classmethod
    def from_dict(cls, value: Any) -> 'Effects':
        """Parse an exact server-owned effects object."""
        if not isinstance(value, dict):
            raise ValidationError('monitor_room effects must be an object')
        fields = {
            'physical_navigation',
            'camera_capture',
            'external_video_stream',
            'video_recording',
            'audio_capture',
            'max_duration_seconds',
            'coverage_mode',
            'viewer_scope',
            'talkback_allowed',
            'schema_version',
        }
        if set(value) != fields:
            raise ValidationError(
                'monitor_room effects fields do not match the contract'
            )
        return cls(**value)

    @property
    def digest(self) -> str:
        """Return the canonical effects SHA-256 digest."""
        return self._digest

    @property
    def gazebo_simulation_navigation(self) -> bool:
        """Return whether this is the exact durable Gazebo-only profile."""
        return (
            self.schema_version
            == GAZEBO_SIMULATION_EFFECTS_SCHEMA_VERSION
            and self.physical_navigation is False
            and self.camera_capture is False
            and self.external_video_stream is False
            and self.video_recording is False
            and self.audio_capture is False
            and self.talkback_allowed is False
            and self.coverage_mode == 'whole_room'
            and self.viewer_scope == 'requesting_user'
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a detached JSON effects object."""
        return {
            'schema_version': self.schema_version,
            'physical_navigation': self.physical_navigation,
            'camera_capture': self.camera_capture,
            'external_video_stream': self.external_video_stream,
            'video_recording': self.video_recording,
            'audio_capture': self.audio_capture,
            'max_duration_seconds': self.max_duration_seconds,
            'coverage_mode': self.coverage_mode,
            'viewer_scope': self.viewer_scope,
            'talkback_allowed': self.talkback_allowed,
        }


def gazebo_simulation_navigation_effects(
    max_duration_seconds: int = 300,
) -> Effects:
    """
    Return the only profile that permits Gazebo navigation alone.

    This does not start, stop, reconfigure, or otherwise control the existing
    Homecam/KVS stream.  It also grants no physical robot authority.
    """
    return Effects(
        schema_version=GAZEBO_SIMULATION_EFFECTS_SCHEMA_VERSION,
        physical_navigation=False,
        camera_capture=False,
        external_video_stream=False,
        video_recording=False,
        audio_capture=False,
        max_duration_seconds=max_duration_seconds,
        coverage_mode='whole_room',
        viewer_scope='requesting_user',
        talkback_allowed=False,
    )


@dataclass(frozen=True)
class _SemanticRoom:
    room_id: str
    name: str
    name_key: str
    category: str
    geometry_json: str
    geometry_digest: str
    representative_point: Tuple[float, float]
    clearance_m: float
    area_m2: float

    def semantic_value(self) -> Dict[str, Any]:
        return {
            'room_id': self.room_id,
            'name': self.name,
            'category': self.category,
            'geometry_digest': self.geometry_digest,
            'representative_point': list(self.representative_point),
            'clearance_m': self.clearance_m,
            'area_m2': self.area_m2,
        }


@dataclass(frozen=True)
class TrustedSemanticSnapshot:
    """Validated immutable view of one finalized Homecam map row."""

    device_id: str
    device_binding_revision: str
    source_revision: str
    map_id: str
    map_revision: str
    semantic_revision: str
    frame_id: str
    zones_digest: str
    rooms: Tuple[_SemanticRoom, ...] = field(repr=False)


@dataclass(frozen=True)
class TargetBinding:
    """Immutable room, map, device, and effects binding."""

    device_id: str
    device_binding_revision: str
    source_revision: str
    map_id: str
    map_revision: str
    semantic_revision: str
    frame_id: str
    room_id: str
    room_name: str
    room_category: str
    source_arguments_digest: str
    geometry_json: str = field(repr=False)
    geometry_digest: str
    representative_point: Tuple[float, float]
    clearance_m: float
    area_m2: float
    effects: Effects
    schema_version: int = TARGET_BINDING_SCHEMA_VERSION
    _binding_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Revalidate and bind every execution-relevant target field."""
        if (
            type(self.schema_version) is not int
            or self.schema_version != TARGET_BINDING_SCHEMA_VERSION
        ):
            _binding_fail('target binding schema_version is invalid')
        for name in (
            'device_id',
            'device_binding_revision',
            'source_revision',
            'map_id',
            'map_revision',
            'room_id',
        ):
            object.__setattr__(
                self,
                name,
                _binding_identifier(getattr(self, name), name),
            )
        if self.frame_id != MAP_FRAME:
            _binding_fail('target binding frame_id must be map')
        _sha256_string(self.semantic_revision, 'semantic_revision')
        _sha256_string(
            self.source_arguments_digest,
            'source_arguments_digest',
        )
        _sha256_string(self.geometry_digest, 'geometry_digest')
        try:
            room_name = _normalized_text(
                self.room_name,
                'room_name',
                MAX_NAME_LENGTH,
            )
        except TargetResolutionError as error:
            _binding_fail(str(error))
        object.__setattr__(self, 'room_name', room_name)
        if self.room_category not in _ROOM_CATEGORIES:
            _binding_fail('target binding room_category is unsupported')
        if (
            not isinstance(self.geometry_json, str)
            or not self.geometry_json
            or len(self.geometry_json.encode('utf-8'))
            > MAX_SNAPSHOT_BYTES
        ):
            _binding_fail('target binding geometry_json is invalid')
        try:
            geometry_value = json.loads(
                self.geometry_json,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError('non-finite JSON number')
                ),
            )
            geometry_json, geometry_digest, area, polygons = (
                _parse_geometry(geometry_value, [0])
            )
        except (ValueError, TypeError, TargetResolutionError) as error:
            _binding_fail(f'target binding geometry is invalid: {error}')
        if geometry_json != self.geometry_json:
            _binding_fail('target binding geometry_json is not canonical')
        if geometry_digest != self.geometry_digest:
            _binding_fail('target binding geometry_digest does not match')
        point_value = self.representative_point
        if (
            not isinstance(point_value, (list, tuple))
            or len(point_value) != 2
        ):
            _binding_fail('target binding representative_point is invalid')
        try:
            representative_point = _point(
                list(point_value),
                'representative_point',
            )
            clearance_m = _finite_number(
                self.clearance_m,
                'clearance_m',
                0.000001,
                MAX_ABS_COORDINATE,
            )
            area_m2 = _finite_number(
                self.area_m2,
                'area_m2',
                0.000001,
                MAX_ABS_COORDINATE * MAX_ABS_COORDINATE,
            )
        except TargetResolutionError as error:
            _binding_fail(str(error))
        if not _point_in_geometry(representative_point, polygons):
            _binding_fail(
                'target binding representative_point is outside geometry'
            )
        if abs(area_m2 - round(area, 2)) > 0.011:
            _binding_fail('target binding area_m2 does not match geometry')
        object.__setattr__(
            self,
            'representative_point',
            representative_point,
        )
        object.__setattr__(self, 'clearance_m', clearance_m)
        object.__setattr__(self, 'area_m2', area_m2)
        if not isinstance(self.effects, Effects):
            _binding_fail('target binding effects are invalid')
        body = self._digest_value()
        object.__setattr__(self, '_binding_digest', _digest_json(body))

    @classmethod
    def from_private_dict(cls, value: Any) -> 'TargetBinding':
        """Reconstruct one exact private persistence record fail-closed."""
        fields = {
            'schema_version',
            'device_id',
            'device_binding_revision',
            'source_revision',
            'map_id',
            'map_revision',
            'semantic_revision',
            'frame_id',
            'room_id',
            'room_name',
            'room_category',
            'source_arguments_digest',
            'geometry',
            'geometry_digest',
            'representative_point',
            'clearance_m',
            'area_m2',
            'effects',
            'effects_digest',
            'binding_digest',
            'execution_authorized',
        }
        if not isinstance(value, dict) or set(value) != fields:
            _binding_fail('private target binding fields are invalid')
        if value.get('execution_authorized') is not False:
            _binding_fail('private target binding cannot authorize execution')
        point = value.get('representative_point')
        if not isinstance(point, list) or len(point) != 2:
            _binding_fail('private representative_point is invalid')
        try:
            effects = Effects.from_dict(value.get('effects'))
            geometry_json = _canonical_json(value.get('geometry'))
        except (ValidationError, TargetResolutionError) as error:
            _binding_fail(f'private target binding is invalid: {error}')
        binding = cls(
            schema_version=value.get('schema_version'),
            device_id=value.get('device_id'),
            device_binding_revision=value.get(
                'device_binding_revision'
            ),
            source_revision=value.get('source_revision'),
            map_id=value.get('map_id'),
            map_revision=value.get('map_revision'),
            semantic_revision=value.get('semantic_revision'),
            frame_id=value.get('frame_id'),
            room_id=value.get('room_id'),
            room_name=value.get('room_name'),
            room_category=value.get('room_category'),
            source_arguments_digest=value.get(
                'source_arguments_digest'
            ),
            geometry_json=geometry_json,
            geometry_digest=value.get('geometry_digest'),
            representative_point=tuple(point),
            clearance_m=value.get('clearance_m'),
            area_m2=value.get('area_m2'),
            effects=effects,
        )
        _sha256_string(value.get('effects_digest'), 'effects_digest')
        _sha256_string(value.get('binding_digest'), 'binding_digest')
        if value['effects_digest'] != binding.effects_digest:
            _binding_fail('private effects_digest does not match')
        if value['binding_digest'] != binding.binding_digest:
            _binding_fail('private binding_digest does not match')
        return binding

    @property
    def effects_digest(self) -> str:
        """Return the immutable effects digest."""
        return self.effects.digest

    @property
    def binding_digest(self) -> str:
        """Return the canonical complete binding SHA-256 digest."""
        return self._binding_digest

    def geometry_dict(self) -> Dict[str, Any]:
        """Return a detached geometry object for a later planner."""
        return json.loads(self.geometry_json)

    def matches_snapshot(self, snapshot: Any) -> bool:
        """Check all source revisions before a later execution preflight."""
        return (
            isinstance(snapshot, TrustedSemanticSnapshot)
            and self.device_id == snapshot.device_id
            and self.device_binding_revision
            == snapshot.device_binding_revision
            and self.source_revision == snapshot.source_revision
            and self.map_id == snapshot.map_id
            and self.map_revision == snapshot.map_revision
            and self.semantic_revision == snapshot.semantic_revision
            and self.frame_id == snapshot.frame_id
        )

    def _digest_value(self) -> Dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'device_id': self.device_id,
            'device_binding_revision': self.device_binding_revision,
            'source_revision': self.source_revision,
            'map_id': self.map_id,
            'map_revision': self.map_revision,
            'semantic_revision': self.semantic_revision,
            'frame_id': self.frame_id,
            'room_id': self.room_id,
            'room_name': self.room_name,
            'room_category': self.room_category,
            'source_arguments_digest': self.source_arguments_digest,
            'geometry_digest': self.geometry_digest,
            'representative_point': list(self.representative_point),
            'clearance_m': self.clearance_m,
            'area_m2': self.area_m2,
            'effects_digest': self.effects_digest,
        }

    def to_private_dict(self) -> Dict[str, Any]:
        """Return private persistence and planner evidence."""
        result = self._digest_value()
        result.update(
            {
                'geometry': self.geometry_dict(),
                'effects': self.effects.to_dict(),
                'binding_digest': self.binding_digest,
                'execution_authorized': False,
            }
        )
        return result

    def to_dict(self) -> Dict[str, Any]:
        """Return a public-safe confirmation summary without target data."""
        return {
            'schema_version': self.schema_version,
            'room_name': self.room_name,
            'room_category': self.room_category,
            'effects': self.effects.to_dict(),
            'effects_digest': self.effects_digest,
            'binding_digest': self.binding_digest,
            'execution_authorized': False,
        }


def _parse_room(value: Any, point_budget: list) -> _SemanticRoom:
    if not isinstance(value, dict) or value.get('type') != 'Feature':
        _fail('every room must be a GeoJSON Feature')
    if set(value) - {'type', 'id', 'properties', 'geometry'}:
        _fail('room feature contains unsupported top-level fields')
    properties = value.get('properties')
    if not isinstance(properties, dict) or properties.get('role') != 'room':
        _fail('every room must have the room role')
    room_id = _identifier(value.get('id'), 'room.id')
    property_room_id = _identifier(
        properties.get('room_id'),
        'room.properties.room_id',
    )
    if room_id != property_room_id:
        _fail('room id and properties.room_id do not match')
    name = _normalized_text(
        properties.get('name'),
        'room.properties.name',
        MAX_NAME_LENGTH,
    )
    category = properties.get('category')
    if not isinstance(category, str) or category not in _ROOM_CATEGORIES:
        _fail('room.properties.category is unsupported')
    geometry_json, geometry_digest, area, polygons = _parse_geometry(
        value.get('geometry'),
        point_budget,
    )
    representative_point = _point(
        properties.get('representative_point'),
        'room.properties.representative_point',
    )
    if not _point_in_geometry(representative_point, polygons):
        _fail('room representative_point is not strictly inside geometry')
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
    computed_area = round(area, 2)
    if abs(area_m2 - computed_area) > 0.011:
        _fail('room area_m2 does not match its geometry')
    return _SemanticRoom(
        room_id=room_id,
        name=name,
        name_key=_lookup_key(name),
        category=category,
        geometry_json=geometry_json,
        geometry_digest=geometry_digest,
        representative_point=representative_point,
        clearance_m=clearance_m,
        area_m2=area_m2,
    )


def _validate_zones(
    value: Any,
    map_id: str,
    map_revision: str,
) -> str:
    if value is None:
        return _digest_json(None)
    if not isinstance(value, dict):
        _fail('zones must be an object or null')
    if (
        value.get('type') != 'FeatureCollection'
        or value.get('format') != SEMANTIC_ZONES_FORMAT
        or value.get('map_id') != map_id
        or value.get('map_revision') != map_revision
        or not isinstance(value.get('features'), list)
        or len(value['features']) > MAX_ROOMS
    ):
        _fail('zones do not match the finalized map')
    return _digest_json(value)


def parse_trusted_semantic_snapshot(
    value: Any,
    *,
    device_id: str,
    device_binding_revision: str,
    source_is_finalized: bool,
) -> TrustedSemanticSnapshot:
    """
    Parse one finalized Homecam semantic response fail-closed.

    The device fields and ``source_is_finalized`` must come from an
    authenticated repository or transport adapter, never from the model or
    request body.
    """
    device_id = _identifier(device_id, 'device_id')
    device_binding_revision = _identifier(
        device_binding_revision,
        'device_binding_revision',
    )
    if source_is_finalized is not True:
        _fail('only a finalized semantic source may bind a room target')
    if not isinstance(value, dict) or set(value) != _HOME_CAM_FIELDS:
        _fail('Homecam semantic response fields do not match the contract')
    canonical_payload = _canonical_json(value)
    if len(canonical_payload.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
        _fail('semantic snapshot exceeds the supported size')
    source_revision = _identifier(value.get('revision'), 'revision')
    map_id = _identifier(value.get('mapId'), 'mapId')
    map_revision = _identifier(value.get('mapRevision'), 'mapRevision')
    user_map = value.get('userMap')
    if not isinstance(user_map, dict) or set(user_map) - _USER_MAP_FIELDS:
        _fail('userMap fields do not match the contract')
    required_user_map_fields = {
        'type',
        'format',
        'map_id',
        'map_revision',
        'frame_id',
        'features',
    }
    if not required_user_map_fields.issubset(user_map):
        _fail('userMap is missing required fields')
    if (
        user_map.get('type') != 'FeatureCollection'
        or user_map.get('format') != USER_MAP_FORMAT
        or user_map.get('frame_id') != MAP_FRAME
    ):
        _fail('userMap format or frame is unsupported')
    if (
        user_map.get('map_id') != map_id
        or user_map.get('map_revision') != map_revision
    ):
        _fail('nested userMap identity does not match the finalized map')
    legacy_map_ids = user_map.get('legacy_map_ids', [])
    if (
        not isinstance(legacy_map_ids, list)
        or len(legacy_map_ids) > 32
        or any(
            not isinstance(item, str)
            or not _SAFE_IDENTIFIER.fullmatch(item)
            for item in legacy_map_ids
        )
    ):
        _fail('userMap legacy_map_ids are invalid')
    features = user_map.get('features')
    if (
        not isinstance(features, list)
        or not features
        or len(features) > MAX_FEATURES
    ):
        _fail('userMap features are missing or unbounded')
    room_features = []
    for feature in features:
        if not isinstance(feature, dict):
            _fail('userMap features must be objects')
        properties = feature.get('properties')
        if not isinstance(properties, dict):
            _fail('userMap feature properties must be objects')
        if properties.get('role') == 'room':
            room_features.append(feature)
    if not room_features or len(room_features) > MAX_ROOMS:
        _fail('userMap must contain from 1 to 512 rooms')
    point_budget = [0]
    rooms = tuple(
        _parse_room(room, point_budget) for room in room_features
    )
    room_ids = [room.room_id for room in rooms]
    if len(room_ids) != len(set(room_ids)):
        _fail('room IDs must be unique')
    zones_digest = _validate_zones(
        value.get('zones'),
        map_id,
        map_revision,
    )
    semantic_revision = _digest_json(
        {
            'format': USER_MAP_FORMAT,
            'map_id': map_id,
            'map_revision': map_revision,
            'frame_id': MAP_FRAME,
            'zones_digest': zones_digest,
            'rooms': [
                room.semantic_value()
                for room in sorted(rooms, key=lambda item: item.room_id)
            ],
        }
    )
    return TrustedSemanticSnapshot(
        device_id=device_id,
        device_binding_revision=device_binding_revision,
        source_revision=source_revision,
        map_id=map_id,
        map_revision=map_revision,
        semantic_revision=semantic_revision,
        frame_id=MAP_FRAME,
        zones_digest=zones_digest,
        rooms=rooms,
    )


def _one_room(
    rooms: Iterable[_SemanticRoom],
    location: str,
) -> _SemanticRoom:
    matches = tuple(rooms)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise TargetResolutionError(
            'target_ambiguous',
            f'multiple rooms match {location!r}',
        )
    raise TargetResolutionError(
        'target_not_found',
        f'no room matches {location!r}',
    )


def resolve_monitor_room_target(
    snapshot: Any,
    location: Any,
    effects: Any,
) -> TargetBinding:
    """
    Resolve one exact room name or fixed category alias.

    Name matches take precedence.  Substring, fuzzy, room-ID, and historical
    split/merge aliases are intentionally unsupported.
    """
    if not isinstance(snapshot, TrustedSemanticSnapshot):
        raise TargetResolutionError(
            'invalid_semantic_snapshot',
            'target resolution requires a parsed trusted snapshot',
        )
    if not isinstance(effects, Effects):
        raise TargetResolutionError(
            'invalid_effects',
            'target resolution requires validated effects',
        )
    key = _lookup_key(location)
    source_arguments_digest = _digest_json({'location': location})
    name_matches = tuple(
        room for room in snapshot.rooms if room.name_key == key
    )
    if name_matches:
        room = _one_room(name_matches, str(location))
    else:
        category = _NORMALIZED_CATEGORY_ALIASES.get(key)
        if category is None:
            room = _one_room((), str(location))
        else:
            room = _one_room(
                (
                    item
                    for item in snapshot.rooms
                    if item.category == category
                ),
                str(location),
            )
    return TargetBinding(
        device_id=snapshot.device_id,
        device_binding_revision=snapshot.device_binding_revision,
        source_revision=snapshot.source_revision,
        map_id=snapshot.map_id,
        map_revision=snapshot.map_revision,
        semantic_revision=snapshot.semantic_revision,
        frame_id=snapshot.frame_id,
        room_id=room.room_id,
        room_name=room.name,
        room_category=room.category,
        source_arguments_digest=source_arguments_digest,
        geometry_json=room.geometry_json,
        geometry_digest=room.geometry_digest,
        representative_point=room.representative_point,
        clearance_m=room.clearance_m,
        area_m2=room.area_m2,
        effects=effects,
    )
