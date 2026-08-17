"""
Protected Gazebo-only intake for durable ``PrepareOperation`` values.

The Agent-side outbox dispatcher is the only intended peer.  This boundary
accepts one complete, immutable simulation preparation over a protected Unix
socket, converts its nanosecond deadline to the store's CLOCK_BOOTTIME
representation, and calls only ``GazeboMonitorRoomStore.prepare``.  It does
not import ROS, create ROS entities, drive, observe, cancel, or grant physical
execution authority.

Coordinates and the private map/semantic bindings are accepted only in the
request.  Successful responses deliberately contain just public correlation
identities, the persisted prepare fingerprint, and explicit non-claims.
"""

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
from threading import RLock
import time
from typing import Any, Dict, Optional, Tuple

from malbut_gazebo.gazebo_monitor_room_store import (
    GAZEBO_MONITOR_ROOM_MAX_SAMPLES,
    GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
    GazeboMonitorRoomConflictError,
    GazeboMonitorRoomDeadlineError,
    GazeboMonitorRoomStore,
    GazeboMonitorRoomStoreError,
    OperationObservation,
    OrderedSemanticSample,
    PrepareOperation,
    PrivateOperationBinding,
)


PREPARE_GATEWAY_SCHEMA_VERSION = 1
PREPARE_GATEWAY_MAX_SAMPLES = GAZEBO_MONITOR_ROOM_MAX_SAMPLES
PREPARE_GATEWAY_MAX_REQUEST_BYTES = 1024 * 1024
PREPARE_GATEWAY_MAX_RESPONSE_BYTES = 4096
PREPARE_GATEWAY_SOCKET_MODE = 0o600
PREPARE_GATEWAY_LISTEN_BACKLOG = 8

# Descriptive aliases make the bounds unambiguous to an independent client.
GAZEBO_MONITOR_ROOM_PREPARE_GATEWAY_SCHEMA_VERSION = (
    PREPARE_GATEWAY_SCHEMA_VERSION
)
GAZEBO_MONITOR_ROOM_PREPARE_GATEWAY_MAX_SAMPLES = (
    PREPARE_GATEWAY_MAX_SAMPLES
)
GAZEBO_MONITOR_ROOM_PREPARE_GATEWAY_MAX_REQUEST_BYTES = (
    PREPARE_GATEWAY_MAX_REQUEST_BYTES
)
GAZEBO_MONITOR_ROOM_PREPARE_GATEWAY_MAX_RESPONSE_BYTES = (
    PREPARE_GATEWAY_MAX_RESPONSE_BYTES
)

_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_COORDINATE_MM = 10_000_000
_MAX_ORDINAL = 1_000_000
_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_BOOT_ID = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$'
)

_SAMPLE_FIELDS = frozenset(
    {
        'index',
        'polygon_ordinal',
        'row_ordinal',
        'x_mm',
        'y_mm',
        'frame_id',
    }
)
_REQUEST_FIELDS = frozenset(
    {
        'schema_version',
        'request_id',
        'outbox_id',
        'operation_id',
        'prepare_request_id',
        'host_boot_id',
        'robot_id',
        'map_id',
        'map_revision',
        'semantic_revision',
        'zones_digest',
        'target_binding_digest',
        'effects_digest',
        'profile_digest',
        'plan_digest',
        'ordered_semantic_samples',
        'deadline_boottime_ns',
        'runtime_mode',
        'simulation',
        'physical_authorized',
        'physical_effects',
        'viewer_live',
        'camera_coverage_validated',
        'coverage_achieved',
    }
)
_RESPONSE_FIELDS = frozenset(
    {
        'schema_version',
        'request_id',
        'outbox_id',
        'operation_id',
        'state',
        'prepare_fingerprint',
        'replayed',
        'runtime_mode',
        'simulation',
        'physical_authorized',
        'physical_effects',
        'viewer_live',
        'camera_coverage_validated',
        'coverage_achieved',
    }
)

_ERROR_CODES = frozenset(
    {
        'prepare_gateway_binding_invalid',
        'prepare_gateway_boot_mismatch',
        'prepare_gateway_configuration_invalid',
        'prepare_gateway_conflict',
        'prepare_gateway_deadline_expired',
        'prepare_gateway_operation_unavailable',
        'prepare_gateway_request_invalid',
        'prepare_gateway_response_invalid',
        'prepare_gateway_robot_mismatch',
        'prepare_gateway_socket_closed',
        'prepare_gateway_socket_exists',
        'prepare_gateway_socket_invalid',
        'prepare_gateway_socket_not_started',
        'prepare_gateway_socket_peer_rejected',
        'prepare_gateway_socket_timeout',
        'prepare_gateway_transport_unavailable',
    }
)


class GazeboMonitorRoomPrepareGatewayError(RuntimeError):
    """Content-free failure at the private preparation boundary."""

    def __init__(
        self,
        code: str = 'prepare_gateway_operation_unavailable',
    ) -> None:
        """Keep only one closed error code as public content."""
        normalized = (
            code
            if type(code) is str and code in _ERROR_CODES
            else 'prepare_gateway_operation_unavailable'
        )
        super().__init__(normalized)
        self.code = normalized

    def __getattribute__(self, name: str) -> Any:
        """Hide collaborator exception chains and tracebacks."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


class _InvalidJSON(ValueError):
    """Internal duplicate/non-finite JSON control flow."""


def _raise(code: str) -> None:
    error = GazeboMonitorRoomPrepareGatewayError(code)
    error.__cause__ = None
    error.__context__ = None
    error.__traceback__ = None
    raise error


def _identifier(value: Any) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _raise('prepare_gateway_request_invalid')
    return value


def _configured_identifier(value: Any) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _raise('prepare_gateway_configuration_invalid')
    return value


def _prefixed_identifier(value: Any, prefix: str) -> str:
    normalized = _identifier(value)
    if not normalized.startswith(prefix):
        _raise('prepare_gateway_request_invalid')
    return normalized


def _digest(value: Any) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _raise('prepare_gateway_request_invalid')
    return value


def _boot_id(value: Any, *, configuration: bool = False) -> str:
    if type(value) is not str or _BOOT_ID.fullmatch(value) is None:
        _raise(
            'prepare_gateway_configuration_invalid'
            if configuration
            else 'prepare_gateway_request_invalid'
        )
    return value


def _bounded_integer(
    value: Any,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _raise('prepare_gateway_request_invalid')
    return value


def _timeout(value: Any) -> float:
    if type(value) not in (int, float):
        _raise('prepare_gateway_configuration_invalid')
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _raise('prepare_gateway_configuration_invalid')
    if not math.isfinite(result) or not 0.0 < result <= 30.0:
        _raise('prepare_gateway_configuration_invalid')
    return result


def _canonical_json_bytes(value: Any, *, response: bool = False) -> bytes:
    invalid = False
    encoded = b''
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode('ascii')
    except (
        OverflowError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        invalid = True
    limit = (
        PREPARE_GATEWAY_MAX_RESPONSE_BYTES
        if response
        else PREPARE_GATEWAY_MAX_REQUEST_BYTES
    )
    if invalid or not encoded or len(encoded) > limit:
        _raise(
            'prepare_gateway_response_invalid'
            if response
            else 'prepare_gateway_request_invalid'
        )
    return encoded


def _fingerprint(value: Any, *, response: bool = False) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(value, response=response)
    ).hexdigest()


def _unique_object(pairs: list[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if type(key) is not str or key in value:
            raise _InvalidJSON()
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise _InvalidJSON()


def _mapping_from_bytes(
    payload: Any,
    *,
    response: bool,
) -> Dict[str, Any]:
    limit = (
        PREPARE_GATEWAY_MAX_RESPONSE_BYTES
        if response
        else PREPARE_GATEWAY_MAX_REQUEST_BYTES
    )
    code = (
        'prepare_gateway_response_invalid'
        if response
        else 'prepare_gateway_request_invalid'
    )
    if type(payload) is not bytes or not payload or len(payload) > limit:
        _raise(code)
    invalid = False
    value: Any = None
    try:
        value = json.loads(
            payload.decode('ascii'),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (
        _InvalidJSON,
        RecursionError,
        UnicodeDecodeError,
        ValueError,
    ):
        invalid = True
    if invalid or type(value) is not dict:
        _raise(code)
    return value


def _read_local_boot_id() -> str:
    value = ''
    invalid = False
    try:
        with open(
            '/proc/sys/kernel/random/boot_id',
            'r',
            encoding='ascii',
        ) as stream:
            value = stream.read(38)
    except (OSError, UnicodeError):
        invalid = True
    if value.endswith('\n'):
        value = value[:-1]
    if invalid:
        _raise('prepare_gateway_configuration_invalid')
    return _boot_id(value, configuration=True)


def _configured_boot_id(
    value: Any,
    *,
    boot_id_reader: Any = None,
) -> str:
    if value is not None and boot_id_reader is not None:
        _raise('prepare_gateway_configuration_invalid')
    if value is not None:
        return _boot_id(value, configuration=True)
    reader = _read_local_boot_id if boot_id_reader is None else boot_id_reader
    if not callable(reader):
        _raise('prepare_gateway_configuration_invalid')
    invalid = False
    result: Any = None
    try:
        result = reader()
    except Exception:
        invalid = True
    if invalid:
        _raise('prepare_gateway_configuration_invalid')
    return _boot_id(result, configuration=True)


def _boottime(clock: Any) -> float:
    invalid = False
    value: Any = None
    try:
        value = (
            time.clock_gettime(time.CLOCK_BOOTTIME)
            if clock is None
            else clock()
        )
    except Exception:
        invalid = True
    if type(value) not in (int, float):
        invalid = True
    if not invalid:
        try:
            value = float(value)
        except (OverflowError, TypeError, ValueError):
            invalid = True
    if invalid or not math.isfinite(value) or value < 0.0:
        _raise('prepare_gateway_operation_unavailable')
    return 0.0 if value == 0.0 else value


def _transport_now() -> float:
    invalid = False
    value: Any = None
    try:
        value = time.monotonic()
    except Exception:
        invalid = True
    if type(value) not in (int, float):
        invalid = True
    if not invalid:
        value = float(value)
    if invalid or not math.isfinite(value) or value < 0.0:
        _raise('prepare_gateway_transport_unavailable')
    return value


@dataclass(frozen=True, repr=False)
class GazeboMonitorRoomPrepareSample:
    """One private fixed-point sample accepted from the Agent outbox."""

    index: int
    polygon_ordinal: int
    row_ordinal: int
    x_mm: int
    y_mm: int
    frame_id: str = 'map'

    def __post_init__(self) -> None:
        """Require one bounded, fixed-point map-frame sample."""
        _bounded_integer(self.index, 0, PREPARE_GATEWAY_MAX_SAMPLES - 1)
        _bounded_integer(self.polygon_ordinal, 0, _MAX_ORDINAL)
        _bounded_integer(self.row_ordinal, 0, _MAX_ORDINAL)
        _bounded_integer(
            self.x_mm,
            -_MAX_COORDINATE_MM,
            _MAX_COORDINATE_MM,
        )
        _bounded_integer(
            self.y_mm,
            -_MAX_COORDINATE_MM,
            _MAX_COORDINATE_MM,
        )
        if type(self.frame_id) is not str or self.frame_id != 'map':
            _raise('prepare_gateway_request_invalid')

    @classmethod
    def from_dict(cls, value: Any) -> 'GazeboMonitorRoomPrepareSample':
        """Build one sample from an exact-key private mapping."""
        if type(value) is not dict or set(value) != _SAMPLE_FIELDS:
            _raise('prepare_gateway_request_invalid')
        return cls(
            index=value['index'],
            polygon_ordinal=value['polygon_ordinal'],
            row_ordinal=value['row_ordinal'],
            x_mm=value['x_mm'],
            y_mm=value['y_mm'],
            frame_id=value['frame_id'],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the complete coordinate-bearing sample mapping."""
        return {
            'index': self.index,
            'polygon_ordinal': self.polygon_ordinal,
            'row_ordinal': self.row_ordinal,
            'x_mm': self.x_mm,
            'y_mm': self.y_mm,
            'frame_id': self.frame_id,
        }

    def __repr__(self) -> str:
        """Render no coordinate, ordinal, or private identity."""
        return "GazeboMonitorRoomPrepareSample(frame_id='map')"


@dataclass(frozen=True, repr=False)
class GazeboMonitorRoomPrepareRequest:
    """Strict coordinate-bearing private request with no authority claims."""

    request_id: str = field(repr=False)
    outbox_id: str = field(repr=False)
    operation_id: str = field(repr=False)
    prepare_request_id: str = field(repr=False)
    host_boot_id: str = field(repr=False)
    robot_id: str = field(repr=False)
    map_id: str = field(repr=False)
    map_revision: str = field(repr=False)
    semantic_revision: str = field(repr=False)
    zones_digest: str = field(repr=False)
    target_binding_digest: str = field(repr=False)
    effects_digest: str = field(repr=False)
    profile_digest: str = field(repr=False)
    plan_digest: str = field(repr=False)
    ordered_semantic_samples: Tuple[
        GazeboMonitorRoomPrepareSample, ...
    ] = field(repr=False)
    deadline_boottime_ns: int = field(repr=False)
    schema_version: int = PREPARE_GATEWAY_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _request_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate and bind the exact immutable private request."""
        if (
            type(self.schema_version) is not int
            or self.schema_version != PREPARE_GATEWAY_SCHEMA_VERSION
        ):
            _raise('prepare_gateway_request_invalid')
        _identifier(self.request_id)
        _prefixed_identifier(
            self.outbox_id,
            'gazebo-execution-outbox-',
        )
        _prefixed_identifier(
            self.operation_id,
            'gazebo-operation-',
        )
        _prefixed_identifier(
            self.prepare_request_id,
            'gazebo-prepare-',
        )
        for value in (
            self.robot_id,
            self.map_id,
            self.map_revision,
            self.semantic_revision,
        ):
            _identifier(value)
        _boot_id(self.host_boot_id)
        for value in (
            self.zones_digest,
            self.target_binding_digest,
            self.effects_digest,
            self.profile_digest,
            self.plan_digest,
        ):
            _digest(value)
        samples = self.ordered_semantic_samples
        if (
            type(samples) is not tuple
            or not 1 <= len(samples) <= PREPARE_GATEWAY_MAX_SAMPLES
            or any(
                type(sample) is not GazeboMonitorRoomPrepareSample
                or sample.index != index
                for index, sample in enumerate(samples)
            )
        ):
            _raise('prepare_gateway_request_invalid')
        _bounded_integer(
            self.deadline_boottime_ns,
            1,
            _MAX_SQLITE_INTEGER,
        )
        if (
            self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            _raise('prepare_gateway_request_invalid')
        object.__setattr__(
            self,
            '_request_fingerprint',
            _fingerprint(self.to_dict()),
        )

    @classmethod
    def from_wire_bytes(
        cls,
        payload: Any,
    ) -> 'GazeboMonitorRoomPrepareRequest':
        """Parse one bounded, duplicate-free canonical JSON request."""
        value = _mapping_from_bytes(payload, response=False)
        if set(value) != _REQUEST_FIELDS:
            _raise('prepare_gateway_request_invalid')
        if (
            value['runtime_mode'] != 'gazebo'
            or value['simulation'] is not True
            or value['physical_authorized'] is not False
            or value['physical_effects'] is not False
            or value['viewer_live'] is not False
            or value['camera_coverage_validated'] is not False
            or value['coverage_achieved'] is not False
            or type(value['ordered_semantic_samples']) is not list
            or not 1 <= len(value['ordered_semantic_samples'])
            <= PREPARE_GATEWAY_MAX_SAMPLES
        ):
            _raise('prepare_gateway_request_invalid')
        request = cls(
            schema_version=value['schema_version'],
            request_id=value['request_id'],
            outbox_id=value['outbox_id'],
            operation_id=value['operation_id'],
            prepare_request_id=value['prepare_request_id'],
            host_boot_id=value['host_boot_id'],
            robot_id=value['robot_id'],
            map_id=value['map_id'],
            map_revision=value['map_revision'],
            semantic_revision=value['semantic_revision'],
            zones_digest=value['zones_digest'],
            target_binding_digest=value['target_binding_digest'],
            effects_digest=value['effects_digest'],
            profile_digest=value['profile_digest'],
            plan_digest=value['plan_digest'],
            ordered_semantic_samples=tuple(
                GazeboMonitorRoomPrepareSample.from_dict(sample)
                for sample in value['ordered_semantic_samples']
            ),
            deadline_boottime_ns=value['deadline_boottime_ns'],
        )
        if request.to_wire_bytes() != payload:
            _raise('prepare_gateway_request_invalid')
        return request

    def to_dict(self) -> Dict[str, Any]:
        """Return the complete private request wire mapping."""
        return {
            'schema_version': self.schema_version,
            'request_id': self.request_id,
            'outbox_id': self.outbox_id,
            'operation_id': self.operation_id,
            'prepare_request_id': self.prepare_request_id,
            'host_boot_id': self.host_boot_id,
            'robot_id': self.robot_id,
            'map_id': self.map_id,
            'map_revision': self.map_revision,
            'semantic_revision': self.semantic_revision,
            'zones_digest': self.zones_digest,
            'target_binding_digest': self.target_binding_digest,
            'effects_digest': self.effects_digest,
            'profile_digest': self.profile_digest,
            'plan_digest': self.plan_digest,
            'ordered_semantic_samples': [
                sample.to_dict()
                for sample in self.ordered_semantic_samples
            ],
            'deadline_boottime_ns': self.deadline_boottime_ns,
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }

    def to_wire_bytes(self) -> bytes:
        """Serialize the exact request as bounded canonical JSON."""
        self.request_fingerprint
        return _canonical_json_bytes(self.to_dict())

    @property
    def request_fingerprint(self) -> str:
        """Return the request digest after detecting object drift."""
        current = _fingerprint(self.to_dict())
        if current != self._request_fingerprint:
            _raise('prepare_gateway_request_invalid')
        return current

    def __repr__(self) -> str:
        """Render only sample count and fixed simulation non-claims."""
        return (
            'GazeboMonitorRoomPrepareRequest('
            f'schema_version={self.schema_version}, '
            f'sample_count={len(self.ordered_semantic_samples)}, '
            "runtime_mode='gazebo', simulation=True, "
            'physical_authorized=False, physical_effects=False, '
            'viewer_live=False, camera_coverage_validated=False, '
            'coverage_achieved=False)'
        )


@dataclass(frozen=True, repr=False)
class GazeboMonitorRoomPreparedAcknowledgement:
    """Coordinate-free proof of one exact persisted preparation."""

    request_id: str = field(repr=False)
    outbox_id: str = field(repr=False)
    operation_id: str = field(repr=False)
    prepare_fingerprint: str = field(repr=False)
    replayed: bool
    state: str = field(default='prepared', init=False)
    schema_version: int = PREPARE_GATEWAY_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _response_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the coordinate-free prepared acknowledgement."""
        if (
            type(self.schema_version) is not int
            or self.schema_version != PREPARE_GATEWAY_SCHEMA_VERSION
        ):
            _raise('prepare_gateway_response_invalid')
        if (
            type(self.request_id) is not str
            or _IDENTIFIER.fullmatch(self.request_id) is None
            or type(self.outbox_id) is not str
            or _IDENTIFIER.fullmatch(self.outbox_id) is None
            or not self.outbox_id.startswith(
                'gazebo-execution-outbox-'
            )
            or type(self.operation_id) is not str
            or _IDENTIFIER.fullmatch(self.operation_id) is None
            or not self.operation_id.startswith('gazebo-operation-')
        ):
            _raise('prepare_gateway_response_invalid')
        if (
            type(self.prepare_fingerprint) is not str
            or _DIGEST.fullmatch(self.prepare_fingerprint) is None
            or type(self.replayed) is not bool
            or self.state != 'prepared'
            or self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            _raise('prepare_gateway_response_invalid')
        object.__setattr__(
            self,
            '_response_fingerprint',
            _fingerprint(self.to_dict(), response=True),
        )

    @classmethod
    def from_wire_bytes(
        cls,
        payload: Any,
    ) -> 'GazeboMonitorRoomPreparedAcknowledgement':
        """Parse one exact canonical prepared acknowledgement."""
        value = _mapping_from_bytes(payload, response=True)
        if set(value) != _RESPONSE_FIELDS:
            _raise('prepare_gateway_response_invalid')
        if (
            value['state'] != 'prepared'
            or value['runtime_mode'] != 'gazebo'
            or value['simulation'] is not True
            or value['physical_authorized'] is not False
            or value['physical_effects'] is not False
            or value['viewer_live'] is not False
            or value['camera_coverage_validated'] is not False
            or value['coverage_achieved'] is not False
        ):
            _raise('prepare_gateway_response_invalid')
        response = cls(
            schema_version=value['schema_version'],
            request_id=value['request_id'],
            outbox_id=value['outbox_id'],
            operation_id=value['operation_id'],
            prepare_fingerprint=value['prepare_fingerprint'],
            replayed=value['replayed'],
        )
        if response.to_wire_bytes() != payload:
            _raise('prepare_gateway_response_invalid')
        return response

    def to_dict(self) -> Dict[str, Any]:
        """Return the coordinate-free acknowledgement mapping."""
        return {
            'schema_version': self.schema_version,
            'request_id': self.request_id,
            'outbox_id': self.outbox_id,
            'operation_id': self.operation_id,
            'state': 'prepared',
            'prepare_fingerprint': self.prepare_fingerprint,
            'replayed': self.replayed,
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }

    def to_wire_bytes(self) -> bytes:
        """Serialize the acknowledgement as bounded canonical JSON."""
        self.response_fingerprint
        return _canonical_json_bytes(self.to_dict(), response=True)

    @property
    def response_fingerprint(self) -> str:
        """Return the response digest after detecting object drift."""
        current = _fingerprint(self.to_dict(), response=True)
        if current != self._response_fingerprint:
            _raise('prepare_gateway_response_invalid')
        return current

    def __repr__(self) -> str:
        """Render no correlation identity or prepare fingerprint."""
        return (
            'GazeboMonitorRoomPreparedAcknowledgement('
            "state='prepared', "
            f'replayed={self.replayed}, runtime_mode=\'gazebo\', '
            'simulation=True, physical_authorized=False, '
            'physical_effects=False, viewer_live=False, '
            'camera_coverage_validated=False, coverage_achieved=False)'
        )


def _canonical_request(value: Any) -> GazeboMonitorRoomPrepareRequest:
    invalid = False
    result: Any = None
    try:
        if type(value) is not GazeboMonitorRoomPrepareRequest:
            invalid = True
        else:
            result = GazeboMonitorRoomPrepareRequest.from_wire_bytes(
                value.to_wire_bytes()
            )
            invalid = (
                result != value
                or result.request_fingerprint != value.request_fingerprint
            )
    except Exception:
        invalid = True
    if invalid:
        _raise('prepare_gateway_request_invalid')
    return result


def _canonical_observation(value: Any) -> OperationObservation:
    invalid = False
    result: Any = None
    try:
        if type(value) is not OperationObservation:
            invalid = True
        else:
            result = OperationObservation(
                operation_id=value.operation_id,
                robot_id=value.robot_id,
                state=value.state,
                current_sample_index=value.current_sample_index,
                current_sample_state=value.current_sample_state,
                current_goal_uuid=value.current_goal_uuid,
                navigation_samples_total=value.navigation_samples_total,
                navigation_samples_reached=value.navigation_samples_reached,
                fence_epoch=value.fence_epoch,
                lease_owner=value.lease_owner,
                lease_expires_at=value.lease_expires_at,
                deadline=value.deadline,
                terminal_code=value.terminal_code,
                cancel_request_id=value.cancel_request_id,
                created_at=value.created_at,
                updated_at=value.updated_at,
                replayed=value.replayed,
            )
            invalid = (
                result.__dict__.keys() != value.__dict__.keys()
                or any(
                    type(value.__dict__[name])
                    is not type(expected)
                    or value.__dict__[name] != expected
                    for name, expected in result.__dict__.items()
                )
            )
    except Exception:
        invalid = True
    if invalid:
        _raise('prepare_gateway_binding_invalid')
    return result


def _canonical_binding(value: Any) -> PrivateOperationBinding:
    invalid = False
    result: Any = None
    try:
        if type(value) is not PrivateOperationBinding:
            invalid = True
        else:
            result = PrivateOperationBinding(
                operation_id=value.operation_id,
                prepare_fingerprint=value.prepare_fingerprint,
                robot_id=value.robot_id,
                map_id=value.map_id,
                map_revision=value.map_revision,
                semantic_revision=value.semantic_revision,
                zones_digest=value.zones_digest,
                target_binding_digest=value.target_binding_digest,
                effects_digest=value.effects_digest,
                profile_digest=value.profile_digest,
                plan_digest=value.plan_digest,
                sample_count=value.sample_count,
                deadline=value.deadline,
            )
            invalid = any(
                type(getattr(value, name)) is not type(expected)
                or getattr(value, name) != expected
                for name, expected in result.__dict__.items()
            ) or value.__dict__.keys() != result.__dict__.keys()
    except Exception:
        invalid = True
    if invalid:
        _raise('prepare_gateway_binding_invalid')
    return result


class GazeboMonitorRoomPrepareProcessor:
    """Materialize and verify exactly one private store preparation."""

    def __init__(
        self,
        store: GazeboMonitorRoomStore,
        *,
        expected_robot_id: str,
        local_boot_id: Optional[str] = None,
        boot_id_reader: Any = None,
        clock: Any = None,
    ) -> None:
        """Fix the exact store, robot, boot, and authority clock."""
        if (
            type(store) is not GazeboMonitorRoomStore
            or type(expected_robot_id) is not str
            or (
                local_boot_id is not None
                and boot_id_reader is not None
            )
        ):
            _raise('prepare_gateway_configuration_invalid')
        if clock is not None and not callable(clock):
            _raise('prepare_gateway_configuration_invalid')
        self._store = store
        self._clock = clock
        self._expected_robot_id = _configured_identifier(
            expected_robot_id
        )
        self._local_boot_id = _configured_boot_id(
            local_boot_id,
            boot_id_reader=boot_id_reader,
        )
        store_boot = getattr(store, '_host_boot_id', None)
        if (
            type(store_boot) is not str
            or store_boot != self._local_boot_id
        ):
            _raise('prepare_gateway_configuration_invalid')
        self._lock = RLock()
        self._configuration_seal = (
            store,
            self._expected_robot_id,
            self._local_boot_id,
            clock,
        )

    @property
    def expected_robot_id(self) -> str:
        """Return the one configured simulation robot identity."""
        return self._expected_robot_id

    @property
    def local_boot_id(self) -> str:
        """Return the canonical local boot identity fixed at creation."""
        return self._local_boot_id

    def handle_wire_bytes(
        self,
        payload: bytes,
    ) -> bytes:
        """Parse, prepare, verify, and serialize one private request."""
        failure: Optional[GazeboMonitorRoomPrepareGatewayError] = None
        try:
            request = GazeboMonitorRoomPrepareRequest.from_wire_bytes(
                payload
            )
            response = self.prepare(request)
            return response.to_wire_bytes()
        except GazeboMonitorRoomPrepareGatewayError as error:
            failure = error
        except GazeboMonitorRoomConflictError:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_conflict'
            )
        except GazeboMonitorRoomDeadlineError:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_deadline_expired'
            )
        except GazeboMonitorRoomStoreError:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_operation_unavailable'
            )
        except Exception:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_operation_unavailable'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def prepare(
        self,
        request: GazeboMonitorRoomPrepareRequest,
    ) -> GazeboMonitorRoomPreparedAcknowledgement:
        """Persist or exact-replay one canonical preparation."""
        failure: Optional[GazeboMonitorRoomPrepareGatewayError] = None
        try:
            return self._prepare_impl(request)
        except GazeboMonitorRoomPrepareGatewayError as error:
            failure = error
        except GazeboMonitorRoomConflictError:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_conflict'
            )
        except GazeboMonitorRoomDeadlineError:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_deadline_expired'
            )
        except GazeboMonitorRoomStoreError:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_operation_unavailable'
            )
        except Exception:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_operation_unavailable'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _prepare_impl(
        self,
        request: GazeboMonitorRoomPrepareRequest,
    ) -> GazeboMonitorRoomPreparedAcknowledgement:
        self._attest_configuration()
        canonical = _canonical_request(request)
        if canonical.robot_id != self._expected_robot_id:
            _raise('prepare_gateway_robot_mismatch')
        if canonical.host_boot_id != self._local_boot_id:
            _raise('prepare_gateway_boot_mismatch')
        deadline = canonical.deadline_boottime_ns / 1_000_000_000
        with self._lock:
            now = _boottime(self._clock)
            if not math.isfinite(deadline) or deadline <= now:
                _raise('prepare_gateway_deadline_expired')
            operation = PrepareOperation(
                prepare_request_id=canonical.prepare_request_id,
                operation_id=canonical.operation_id,
                robot_id=canonical.robot_id,
                map_id=canonical.map_id,
                map_revision=canonical.map_revision,
                semantic_revision=canonical.semantic_revision,
                zones_digest=canonical.zones_digest,
                target_binding_digest=canonical.target_binding_digest,
                effects_digest=canonical.effects_digest,
                profile_digest=canonical.profile_digest,
                plan_digest=canonical.plan_digest,
                ordered_semantic_samples=tuple(
                    OrderedSemanticSample(
                        index=sample.index,
                        polygon_ordinal=sample.polygon_ordinal,
                        row_ordinal=sample.row_ordinal,
                        x_mm=sample.x_mm,
                        y_mm=sample.y_mm,
                        frame_id=sample.frame_id,
                    )
                    for sample in canonical.ordered_semantic_samples
                ),
                deadline=deadline,
            )
            observation = _canonical_observation(
                GazeboMonitorRoomStore.prepare(
                    self._store,
                    operation,
                    now=now,
                )
            )
            binding = _canonical_binding(
                GazeboMonitorRoomStore.private_operation_binding(
                    self._store,
                    canonical.operation_id,
                )
            )
        expected_binding = {
            'operation_id': operation.operation_id,
            'prepare_fingerprint': operation.payload_fingerprint,
            'robot_id': operation.robot_id,
            'map_id': operation.map_id,
            'map_revision': operation.map_revision,
            'semantic_revision': operation.semantic_revision,
            'zones_digest': operation.zones_digest,
            'target_binding_digest': operation.target_binding_digest,
            'effects_digest': operation.effects_digest,
            'profile_digest': operation.profile_digest,
            'plan_digest': operation.plan_digest,
            'sample_count': len(operation.ordered_semantic_samples),
            'deadline': operation.deadline,
            'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }
        if (
            any(
                type(getattr(binding, name)) is not type(expected)
                or getattr(binding, name) != expected
                for name, expected in expected_binding.items()
            )
            or observation.operation_id != operation.operation_id
            or observation.robot_id != operation.robot_id
            or observation.deadline != operation.deadline
            or observation.navigation_samples_total
            != len(operation.ordered_semantic_samples)
            or observation.runtime_mode != 'gazebo'
            or observation.simulation is not True
            or observation.physical_authorized is not False
            or observation.physical_effects is not False
            or observation.viewer_live is not False
            or observation.camera_coverage_validated is not False
            or observation.coverage_achieved is not False
        ):
            _raise('prepare_gateway_binding_invalid')
        return GazeboMonitorRoomPreparedAcknowledgement(
            request_id=canonical.request_id,
            outbox_id=canonical.outbox_id,
            operation_id=canonical.operation_id,
            prepare_fingerprint=operation.payload_fingerprint,
            replayed=observation.replayed,
        )

    def _attest_configuration(self) -> None:
        seal = getattr(self, '_configuration_seal', None)
        current = (
            getattr(self, '_store', None),
            getattr(self, '_expected_robot_id', None),
            getattr(self, '_local_boot_id', None),
            getattr(self, '_clock', None),
        )
        if (
            type(seal) is not tuple
            or len(seal) != 4
            or any(
                actual is not expected
                if index in {0, 3}
                else type(actual) is not str or actual != expected
                for index, (actual, expected) in enumerate(
                    zip(current, seal)
                )
            )
            or type(self._store) is not GazeboMonitorRoomStore
            or getattr(self._store, '_host_boot_id', None)
            != self._local_boot_id
        ):
            _raise('prepare_gateway_configuration_invalid')


def _canonical_socket_path(value: Any) -> Path:
    invalid = False
    raw = ''
    try:
        raw = os.fspath(value)
    except TypeError:
        invalid = True
    if (
        invalid
        or type(raw) is not str
        or not raw
        or '\x00' in raw
        or not os.path.isabs(raw)
        or os.path.normpath(raw) != raw
        or Path(raw).name == ''
    ):
        _raise('prepare_gateway_socket_invalid')
    try:
        encoded = os.fsencode(raw)
    except (UnicodeEncodeError, ValueError):
        _raise('prepare_gateway_socket_invalid')
    if len(encoded) > 103:
        _raise('prepare_gateway_socket_invalid')
    return Path(raw)


def _validate_parent_chain(path: Path) -> Tuple[Tuple[Any, ...], ...]:
    parent = path.parent
    current = Path(parent.anchor)
    result = []
    euid = os.geteuid()
    for component in parent.parts[1:]:
        current = current / component
        try:
            metadata = os.lstat(current)
        except OSError:
            _raise('prepare_gateway_socket_invalid')
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        sticky_root = (
            metadata.st_uid == 0
            and bool(metadata.st_mode & stat.S_ISVTX)
        )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, euid}
            or (writable and not sticky_root)
        ):
            _raise('prepare_gateway_socket_invalid')
        result.append(
            (
                str(current),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_uid,
            )
        )
    if not result:
        _raise('prepare_gateway_socket_invalid')
    final = result[-1]
    if final[4] != euid or final[3] & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        _raise('prepare_gateway_socket_invalid')
    return tuple(result)


class GazeboMonitorRoomPrepareServer:
    """Serve bounded preparations over one protected AF_UNIX socket."""

    def __init__(
        self,
        processor: GazeboMonitorRoomPrepareProcessor,
        socket_path: Any,
        *,
        expected_agent_uid: int,
        timeout_seconds: float = 2.0,
    ) -> None:
        """Fix the protected endpoint and its only trusted peer."""
        if type(processor) is not GazeboMonitorRoomPrepareProcessor:
            _raise('prepare_gateway_configuration_invalid')
        if (
            type(expected_agent_uid) is not int
            or not 0 <= expected_agent_uid <= (1 << 31) - 1
        ):
            _raise('prepare_gateway_configuration_invalid')
        processor._attest_configuration()
        robot = processor.expected_robot_id
        boot = processor.local_boot_id
        store_boot = getattr(processor._store, '_host_boot_id', None)
        if store_boot is not None and (
            type(store_boot) is not str or store_boot != boot
        ):
            _raise('prepare_gateway_configuration_invalid')
        self._processor = processor
        self._socket_path = _canonical_socket_path(socket_path)
        self._expected_agent_uid = expected_agent_uid
        self._expected_robot_id = robot
        self._local_boot_id = boot
        self._timeout_seconds = _timeout(timeout_seconds)
        self._lifecycle_lock = RLock()
        self._serve_lock = RLock()
        self._listener: Optional[socket.socket] = None
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._socket_parents: Optional[Tuple[Tuple[Any, ...], ...]] = None
        self._active_connections = set()
        self._closed = False
        self._ever_started = False
        self._configuration_seal = (
            processor,
            expected_agent_uid,
            robot,
            boot,
            self._timeout_seconds,
            self._socket_path,
        )

    @property
    def socket_path(self) -> str:
        """Return the canonical absolute configured socket path."""
        return str(self._socket_path)

    @property
    def expected_agent_uid(self) -> int:
        """Return the only Linux UID allowed to submit requests."""
        return self._expected_agent_uid

    @property
    def expected_robot_id(self) -> str:
        """Return the processor-fixed simulation robot identity."""
        return self._expected_robot_id

    @property
    def local_boot_id(self) -> str:
        """Return the processor-fixed canonical local boot identity."""
        return self._local_boot_id

    def start(self) -> None:
        """Bind once without preparing an operation or calling ROS."""
        failure: Optional[GazeboMonitorRoomPrepareGatewayError] = None
        try:
            self._start_impl()
            return
        except GazeboMonitorRoomPrepareGatewayError as error:
            failure = error
        except Exception:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_transport_unavailable'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _start_impl(self) -> None:
        with self._lifecycle_lock:
            self._attest_configuration_locked()
            if self._closed or self._ever_started:
                _raise('prepare_gateway_socket_closed')
            parents = _validate_parent_chain(self._socket_path)
            try:
                os.lstat(self._socket_path)
            except FileNotFoundError:
                pass
            except OSError:
                _raise('prepare_gateway_socket_invalid')
            else:
                _raise('prepare_gateway_socket_exists')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            identity: Optional[Tuple[int, int]] = None
            try:
                listener.bind(str(self._socket_path))
                metadata = os.lstat(self._socket_path)
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                ):
                    _raise('prepare_gateway_socket_invalid')
                identity = (metadata.st_dev, metadata.st_ino)
                os.chmod(self._socket_path, PREPARE_GATEWAY_SOCKET_MODE)
                metadata = os.lstat(self._socket_path)
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or (metadata.st_dev, metadata.st_ino) != identity
                    or stat.S_IMODE(metadata.st_mode)
                    != PREPARE_GATEWAY_SOCKET_MODE
                    or metadata.st_nlink != 1
                    or _validate_parent_chain(self._socket_path) != parents
                ):
                    _raise('prepare_gateway_socket_invalid')
                listener.listen(PREPARE_GATEWAY_LISTEN_BACKLOG)
            except Exception:
                listener.close()
                if identity is not None:
                    self._unlink_owned(identity)
                raise
            self._listener = listener
            self._socket_identity = identity
            self._socket_parents = parents
            self._ever_started = True

    def serve_once(self) -> None:
        """Serve one credentialed peer within bounded I/O time."""
        failure: Optional[GazeboMonitorRoomPrepareGatewayError] = None
        try:
            self._serve_once_impl()
            return
        except GazeboMonitorRoomPrepareGatewayError as error:
            failure = error
        except socket.timeout:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_socket_timeout'
            )
        except Exception:
            failure = GazeboMonitorRoomPrepareGatewayError(
                'prepare_gateway_transport_unavailable'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _serve_once_impl(self) -> None:
        with self._serve_lock:
            with self._lifecycle_lock:
                listener = self._listener
                if listener is None or self._closed:
                    _raise('prepare_gateway_socket_not_started')
                self._attest_configuration_locked()
                self._attest_socket_locked()
            deadline = _transport_now() + self._timeout_seconds
            connection = self._accept(listener, deadline)
            with self._lifecycle_lock:
                if self._closed:
                    connection.close()
                    _raise('prepare_gateway_socket_closed')
                try:
                    self._attest_configuration_locked()
                    self._attest_socket_locked()
                except Exception:
                    connection.close()
                    raise
                self._active_connections.add(connection)
            try:
                self._check_peer(connection)
                header = self._recv_exact(connection, 4, deadline)
                size = struct.unpack('!I', header)[0]
                if (
                    size < 1
                    or size > PREPARE_GATEWAY_MAX_REQUEST_BYTES
                ):
                    _raise('prepare_gateway_socket_invalid')
                payload = self._recv_exact(connection, size, deadline)
                self._set_timeout(connection, deadline)
                if connection.recv(1):
                    _raise('prepare_gateway_socket_invalid')
                self._processor._attest_configuration()
                response = self._processor.handle_wire_bytes(payload)
                if (
                    type(response) is not bytes
                    or not response
                    or len(response)
                    > PREPARE_GATEWAY_MAX_RESPONSE_BYTES
                ):
                    _raise('prepare_gateway_response_invalid')
                frame = struct.pack('!I', len(response)) + response
                self._set_timeout(connection, deadline)
                connection.sendall(frame)
                try:
                    connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            finally:
                with self._lifecycle_lock:
                    self._active_connections.discard(connection)
                connection.close()

    def serve_forever(self) -> None:
        """Contain untrusted request failures until explicitly closed."""
        recoverable = {
            'prepare_gateway_boot_mismatch',
            'prepare_gateway_conflict',
            'prepare_gateway_deadline_expired',
            'prepare_gateway_operation_unavailable',
            'prepare_gateway_request_invalid',
            'prepare_gateway_robot_mismatch',
            'prepare_gateway_socket_invalid',
            'prepare_gateway_socket_peer_rejected',
            'prepare_gateway_socket_timeout',
            'prepare_gateway_transport_unavailable',
        }
        while True:
            with self._lifecycle_lock:
                if self._closed:
                    return
            try:
                self.serve_once()
            except GazeboMonitorRoomPrepareGatewayError as error:
                with self._lifecycle_lock:
                    if self._closed:
                        return
                if error.code not in recoverable:
                    raise

    def close(self) -> None:
        """Close peers and unlink only this server's exact socket inode."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            listener = self._listener
            identity = self._socket_identity
            connections = tuple(self._active_connections)
            self._listener = None
            self._socket_identity = None
            self._socket_parents = None
            self._active_connections.clear()
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if identity is not None:
            self._unlink_owned(identity)

    def _attest_socket_locked(self) -> None:
        identity = self._socket_identity
        parents = self._socket_parents
        if identity is None or parents is None:
            _raise('prepare_gateway_socket_not_started')
        try:
            metadata = os.lstat(self._socket_path)
        except OSError:
            _raise('prepare_gateway_socket_invalid')
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode)
            != PREPARE_GATEWAY_SOCKET_MODE
            or metadata.st_nlink != 1
            or _validate_parent_chain(self._socket_path) != parents
        ):
            _raise('prepare_gateway_socket_invalid')

    def _attest_configuration_locked(self) -> None:
        seal = getattr(self, '_configuration_seal', None)
        current = (
            getattr(self, '_processor', None),
            getattr(self, '_expected_agent_uid', None),
            getattr(self, '_expected_robot_id', None),
            getattr(self, '_local_boot_id', None),
            getattr(self, '_timeout_seconds', None),
            getattr(self, '_socket_path', None),
        )
        if (
            type(seal) is not tuple
            or len(seal) != 6
            or current[0] is not seal[0]
            or type(current[1]) is not int
            or current[1] != seal[1]
            or type(current[2]) is not str
            or current[2] != seal[2]
            or type(current[3]) is not str
            or current[3] != seal[3]
            or type(current[4]) is not float
            or current[4] != seal[4]
            or current[5].__class__ is not seal[5].__class__
            or current[5] != seal[5]
            or type(self._processor)
            is not GazeboMonitorRoomPrepareProcessor
        ):
            _raise('prepare_gateway_configuration_invalid')
        self._processor._attest_configuration()

    def _unlink_owned(self, identity: Tuple[int, int]) -> None:
        try:
            metadata = os.lstat(self._socket_path)
            if (
                stat.S_ISSOCK(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino) == identity
            ):
                os.unlink(self._socket_path)
        except OSError:
            return

    def _check_peer(self, connection: socket.socket) -> None:
        option = getattr(socket, 'SO_PEERCRED', None)
        if option is None:
            _raise('prepare_gateway_socket_peer_rejected')
        invalid = False
        uid = -1
        try:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET,
                option,
                struct.calcsize('3i'),
            )
            _pid, uid, _gid = struct.unpack('3i', credentials)
        except (OSError, struct.error):
            invalid = True
        if invalid or uid != self._expected_agent_uid:
            _raise('prepare_gateway_socket_peer_rejected')

    @staticmethod
    def _set_timeout(connection: socket.socket, deadline: float) -> None:
        remaining = deadline - _transport_now()
        if not math.isfinite(remaining) or remaining <= 0.0:
            raise socket.timeout()
        connection.settimeout(remaining)

    def _accept(
        self,
        listener: socket.socket,
        deadline: float,
    ) -> socket.socket:
        while True:
            remaining = deadline - _transport_now()
            if not math.isfinite(remaining) or remaining <= 0.0:
                raise socket.timeout()
            listener.settimeout(min(remaining, 0.1))
            try:
                connection, _address = listener.accept()
                return connection
            except socket.timeout:
                with self._lifecycle_lock:
                    if self._closed:
                        _raise('prepare_gateway_socket_closed')

    @classmethod
    def _recv_exact(
        cls,
        connection: socket.socket,
        size: int,
        deadline: float,
    ) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            cls._set_timeout(connection, deadline)
            chunk = connection.recv(remaining)
            if not chunk:
                _raise('prepare_gateway_socket_invalid')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)


# Alternate explicit names for callers that use the existing gateway style.
GazeboMonitorRoomPrepareGatewayRequest = GazeboMonitorRoomPrepareRequest
GazeboMonitorRoomPrepareGatewayResponse = (
    GazeboMonitorRoomPreparedAcknowledgement
)
GazeboMonitorRoomPrepareGatewayProcessor = (
    GazeboMonitorRoomPrepareProcessor
)
GazeboMonitorRoomPrepareGatewayServer = GazeboMonitorRoomPrepareServer


__all__ = [
    'GAZEBO_MONITOR_ROOM_PREPARE_GATEWAY_MAX_REQUEST_BYTES',
    'GAZEBO_MONITOR_ROOM_PREPARE_GATEWAY_MAX_RESPONSE_BYTES',
    'GAZEBO_MONITOR_ROOM_PREPARE_GATEWAY_MAX_SAMPLES',
    'GAZEBO_MONITOR_ROOM_PREPARE_GATEWAY_SCHEMA_VERSION',
    'PREPARE_GATEWAY_MAX_REQUEST_BYTES',
    'PREPARE_GATEWAY_MAX_RESPONSE_BYTES',
    'PREPARE_GATEWAY_MAX_SAMPLES',
    'PREPARE_GATEWAY_SCHEMA_VERSION',
    'GazeboMonitorRoomPrepareGatewayError',
    'GazeboMonitorRoomPrepareGatewayProcessor',
    'GazeboMonitorRoomPrepareGatewayRequest',
    'GazeboMonitorRoomPrepareGatewayResponse',
    'GazeboMonitorRoomPrepareGatewayServer',
    'GazeboMonitorRoomPrepareProcessor',
    'GazeboMonitorRoomPrepareRequest',
    'GazeboMonitorRoomPrepareSample',
    'GazeboMonitorRoomPrepareServer',
    'GazeboMonitorRoomPreparedAcknowledgement',
]
