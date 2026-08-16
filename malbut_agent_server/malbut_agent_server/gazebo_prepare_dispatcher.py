"""
Durable Agent-side handoff to the protected Gazebo prepare intake.

The conversational process leases one already-approved Gazebo execution from
its SQLite outbox, commits that lease, sends the exact private preparation to
one fixed same-host Unix socket, and then records a fenced acknowledgement.
The wire contract is duplicated here deliberately: this package must never
import Gazebo, ROS, Nav2, or their Python packages.

No object in this module starts a background drain.  Callers explicitly invoke
``dispatch_once`` with an idempotency key and may retry that same key after an
ambiguous transport failure.  The durable outbox replays the lease and the
Gazebo store replays the exact preparation.
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
import weakref

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gazebo_execution_outbox import (
    GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS,
    GAZEBO_EXECUTION_OUTBOX_MAX_LEASE_SECONDS,
    GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES,
    GAZEBO_EXECUTION_OUTBOX_MIN_LEASE_SECONDS,
    GazeboExecutionAcknowledgement,
    GazeboExecutionClaim,
    GazeboExecutionSample,
)


GAZEBO_PREPARE_SCHEMA_VERSION = 1
GAZEBO_PREPARE_MAX_SAMPLES = GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES
GAZEBO_PREPARE_MAX_REQUEST_BYTES = 1024 * 1024
GAZEBO_PREPARE_MAX_RESPONSE_BYTES = 4096
DEFAULT_GAZEBO_PREPARE_SOCKET_PATH = (
    '/run/malbut/gazebo-monitor-room-prepare.sock'
)
DEFAULT_GAZEBO_PREPARE_TIMEOUT_SECONDS = 2.0
MAX_GAZEBO_PREPARE_TIMEOUT_SECONDS = 30.0

_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_COORDINATE_MM = 10_000_000
_MAX_ORDINAL = 1_000_000
_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_BOOT_ID = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$'
)
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{32,128}$')
_ERROR_CODE = re.compile(r'^gazebo_prepare_[a-z0-9_]{1,80}$')
_CLIENT_SEAL_LOCK = RLock()
_CLIENT_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)
_DISPATCHER_SEAL_LOCK = RLock()
_DISPATCHER_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
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


class GazeboPrepareDispatcherError(RuntimeError):
    """Content-free failure from the private preparation handoff."""

    def __init__(self, code: str = 'gazebo_prepare_unavailable') -> None:
        """Expose only a closed machine code and a generic message."""
        normalized = (
            code
            if type(code) is str
            and _ERROR_CODE.fullmatch(code) is not None
            else 'gazebo_prepare_unavailable'
        )
        super().__init__('Gazebo preparation handoff is unavailable')
        self.code = normalized

    def __getattribute__(self, name: str) -> Any:
        """Hide caught collaborator chains and tracebacks."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


class _InvalidJSON(ValueError):
    """Private duplicate/non-finite JSON decoder control flow."""


def _error(code: str) -> GazeboPrepareDispatcherError:
    return GazeboPrepareDispatcherError(code)


def _identifier(value: Any, *, prefix: Optional[str] = None) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _error('gazebo_prepare_request_invalid')
    if prefix is not None and not value.startswith(prefix):
        raise _error('gazebo_prepare_request_invalid')
    return value


def _digest(value: Any) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _error('gazebo_prepare_request_invalid')
    return value


def _response_identifier(
    value: Any,
    *,
    prefix: Optional[str] = None,
) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _error('gazebo_prepare_response_invalid')
    if prefix is not None and not value.startswith(prefix):
        raise _error('gazebo_prepare_response_invalid')
    return value


def _bounded_integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _error('gazebo_prepare_request_invalid')
    return value


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
        RecursionError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        invalid = True
    limit = (
        GAZEBO_PREPARE_MAX_RESPONSE_BYTES
        if response
        else GAZEBO_PREPARE_MAX_REQUEST_BYTES
    )
    if invalid or not encoded or len(encoded) > limit:
        raise _error(
            'gazebo_prepare_response_invalid'
            if response
            else 'gazebo_prepare_request_invalid'
        )
    return encoded


def _unique_object(pairs: Any) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if type(key) is not str or key in value:
            raise _InvalidJSON()
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise _InvalidJSON()


def _response_mapping(payload: Any) -> Dict[str, Any]:
    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > GAZEBO_PREPARE_MAX_RESPONSE_BYTES
    ):
        raise _error('gazebo_prepare_response_invalid')
    invalid = False
    result: Any = None
    try:
        result = json.loads(
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
    if (
        invalid
        or type(result) is not dict
        or set(result) != _RESPONSE_FIELDS
        or _canonical_json_bytes(result, response=True) != payload
    ):
        raise _error('gazebo_prepare_response_invalid')
    return result


@dataclass(frozen=True, repr=False)
class _PrepareSample:
    """Exact private sample copied from one durable claim."""

    index: int
    polygon_ordinal: int
    row_ordinal: int
    x_mm: int
    y_mm: int
    frame_id: str = 'map'

    def __post_init__(self) -> None:
        _bounded_integer(self.index, 0, GAZEBO_PREPARE_MAX_SAMPLES - 1)
        _bounded_integer(self.polygon_ordinal, 0, _MAX_ORDINAL)
        _bounded_integer(self.row_ordinal, 0, _MAX_ORDINAL)
        _bounded_integer(
            self.x_mm, -_MAX_COORDINATE_MM, _MAX_COORDINATE_MM
        )
        _bounded_integer(
            self.y_mm, -_MAX_COORDINATE_MM, _MAX_COORDINATE_MM
        )
        if type(self.frame_id) is not str or self.frame_id != 'map':
            raise _error('gazebo_prepare_request_invalid')

    @classmethod
    def from_claim(cls, sample: Any) -> '_PrepareSample':
        if type(sample) is not GazeboExecutionSample:
            raise _error('gazebo_prepare_request_invalid')
        return cls(
            index=sample.index,
            polygon_ordinal=sample.polygon_ordinal,
            row_ordinal=sample.row_ordinal,
            x_mm=sample.x_mm,
            y_mm=sample.y_mm,
            frame_id=sample.frame_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'index': self.index,
            'polygon_ordinal': self.polygon_ordinal,
            'row_ordinal': self.row_ordinal,
            'x_mm': self.x_mm,
            'y_mm': self.y_mm,
            'frame_id': self.frame_id,
        }


@dataclass(frozen=True, repr=False)
class _PrepareRequest:
    """Complete schema-v1 private preparation wire value."""

    request_id: str
    outbox_id: str
    operation_id: str
    prepare_request_id: str
    host_boot_id: str
    robot_id: str
    map_id: str
    map_revision: str
    semantic_revision: str
    zones_digest: str
    target_binding_digest: str
    effects_digest: str
    profile_digest: str
    plan_digest: str
    ordered_semantic_samples: Tuple[_PrepareSample, ...]
    deadline_boottime_ns: int
    schema_version: int = GAZEBO_PREPARE_SCHEMA_VERSION
    _fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != GAZEBO_PREPARE_SCHEMA_VERSION
        ):
            raise _error('gazebo_prepare_request_invalid')
        _identifier(self.request_id)
        _identifier(
            self.outbox_id, prefix='gazebo-execution-outbox-'
        )
        _identifier(self.operation_id, prefix='gazebo-operation-')
        _identifier(
            self.prepare_request_id, prefix='gazebo-prepare-'
        )
        for value in (
            self.robot_id,
            self.map_id,
            self.map_revision,
            self.semantic_revision,
        ):
            _identifier(value)
        if (
            type(self.host_boot_id) is not str
            or _BOOT_ID.fullmatch(self.host_boot_id) is None
        ):
            raise _error('gazebo_prepare_request_invalid')
        for value in (
            self.zones_digest,
            self.target_binding_digest,
            self.effects_digest,
            self.profile_digest,
            self.plan_digest,
        ):
            _digest(value)
        if (
            type(self.ordered_semantic_samples) is not tuple
            or not 1 <= len(self.ordered_semantic_samples)
            <= GAZEBO_PREPARE_MAX_SAMPLES
            or any(
                type(sample) is not _PrepareSample
                or sample.index != index
                for index, sample in enumerate(
                    self.ordered_semantic_samples
                )
            )
        ):
            raise _error('gazebo_prepare_request_invalid')
        _bounded_integer(
            self.deadline_boottime_ns, 1, _MAX_SQLITE_INTEGER
        )
        object.__setattr__(
            self,
            '_fingerprint',
            hashlib.sha256(
                _canonical_json_bytes(self.to_dict())
            ).hexdigest(),
        )

    @classmethod
    def from_claim(cls, claim: Any) -> '_PrepareRequest':
        """Snapshot one exact lease without retaining its secret token."""
        if type(claim) is not GazeboExecutionClaim:
            raise _error('gazebo_prepare_claim_invalid')
        return cls(
            request_id=claim.claim_request_id,
            outbox_id=claim.outbox_id,
            operation_id=claim.operation_id,
            prepare_request_id=claim.prepare_request_id,
            host_boot_id=claim.host_boot_id,
            robot_id=claim.robot_id,
            map_id=claim.map_id,
            map_revision=claim.map_revision,
            semantic_revision=claim.semantic_revision,
            zones_digest=claim.zones_digest,
            target_binding_digest=claim.target_binding_digest,
            effects_digest=claim.effects_digest,
            profile_digest=claim.profile_digest,
            plan_digest=claim.plan_digest,
            ordered_semantic_samples=tuple(
                _PrepareSample.from_claim(sample)
                for sample in claim.ordered_semantic_samples
            ),
            deadline_boottime_ns=claim.deadline_boottime_ns,
        )

    def to_dict(self) -> Dict[str, Any]:
        value = {
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
        if set(value) != _REQUEST_FIELDS:
            raise _error('gazebo_prepare_request_invalid')
        return value

    def to_wire_bytes(self) -> bytes:
        value = self.to_dict()
        encoded = _canonical_json_bytes(value)
        if hashlib.sha256(encoded).hexdigest() != self._fingerprint:
            raise _error('gazebo_prepare_request_invalid')
        return encoded


@dataclass(frozen=True, repr=False)
class _PreparedResponse:
    """Strict coordinate-free response from the Gazebo intake."""

    request_id: str
    outbox_id: str
    operation_id: str
    prepare_fingerprint: str
    replayed: bool

    @classmethod
    def from_wire_bytes(cls, payload: Any) -> '_PreparedResponse':
        value = _response_mapping(payload)
        if (
            type(value['schema_version']) is not int
            or value['schema_version'] != GAZEBO_PREPARE_SCHEMA_VERSION
            or value['state'] != 'prepared'
            or value['runtime_mode'] != 'gazebo'
            or value['simulation'] is not True
            or value['physical_authorized'] is not False
            or value['physical_effects'] is not False
            or value['viewer_live'] is not False
            or value['camera_coverage_validated'] is not False
            or value['coverage_achieved'] is not False
            or type(value['replayed']) is not bool
        ):
            raise _error('gazebo_prepare_response_invalid')
        _response_identifier(value['request_id'])
        _response_identifier(
            value['outbox_id'], prefix='gazebo-execution-outbox-'
        )
        _response_identifier(
            value['operation_id'], prefix='gazebo-operation-'
        )
        if (
            type(value['prepare_fingerprint']) is not str
            or _DIGEST.fullmatch(value['prepare_fingerprint']) is None
        ):
            raise _error('gazebo_prepare_response_invalid')
        return cls(
            request_id=value['request_id'],
            outbox_id=value['outbox_id'],
            operation_id=value['operation_id'],
            prepare_fingerprint=value['prepare_fingerprint'],
            replayed=value['replayed'],
        )


@dataclass(frozen=True, repr=False)
class GazeboPrepareDispatchResult:
    """Frozen coordinate-free proof of the prepare and outbox ACK."""

    outbox_id: str = field(repr=False)
    operation_id: str = field(repr=False)
    claim_fence: int
    prepare_replayed: bool
    state: str = field(default='prepared', init=False)
    schema_version: int = GAZEBO_PREPARE_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _result_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the complete public, simulation-only result."""
        GazeboPrepareDispatchResult._validate_values(self)
        object.__setattr__(
            self,
            '_result_fingerprint',
            hashlib.sha256(
                _canonical_json_bytes(
                    GazeboPrepareDispatchResult._public_values(self),
                    response=True,
                )
            ).hexdigest(),
        )

    def _validate_values(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != GAZEBO_PREPARE_SCHEMA_VERSION
            or self.state != 'prepared'
            or type(self.claim_fence) is not int
            or not 1 <= self.claim_fence
            <= GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS
            or type(self.prepare_replayed) is not bool
            or self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            raise _error('gazebo_prepare_result_invalid')
        _identifier(
            self.outbox_id, prefix='gazebo-execution-outbox-'
        )
        _identifier(self.operation_id, prefix='gazebo-operation-')

    def _public_values(self) -> Dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'outbox_id': self.outbox_id,
            'operation_id': self.operation_id,
            'state': self.state,
            'claim_fence': self.claim_fence,
            'prepare_replayed': self.prepare_replayed,
            'runtime_mode': self.runtime_mode,
            'simulation': self.simulation,
            'physical_authorized': self.physical_authorized,
            'physical_effects': self.physical_effects,
            'viewer_live': self.viewer_live,
            'camera_coverage_validated': (
                self.camera_coverage_validated
            ),
            'coverage_achieved': self.coverage_achieved,
        }

    def _attest(self) -> None:
        GazeboPrepareDispatchResult._validate_values(self)
        current = hashlib.sha256(
            _canonical_json_bytes(
                GazeboPrepareDispatchResult._public_values(self),
                response=True,
            )
        ).hexdigest()
        if current != self._result_fingerprint:
            raise _error('gazebo_prepare_result_invalid')

    def to_public_dict(self) -> Dict[str, Any]:
        """Return no coordinates, digests, tokens, or private bindings."""
        GazeboPrepareDispatchResult._attest(self)
        return GazeboPrepareDispatchResult._public_values(self)

    def __repr__(self) -> str:
        """Render state only, never correlation identities or bindings."""
        GazeboPrepareDispatchResult._attest(self)
        return (
            'GazeboPrepareDispatchResult('
            f"state='{self.state}', claim_fence={self.claim_fence}, "
            f'prepare_replayed={self.prepare_replayed}, '
            "runtime_mode='gazebo', simulation=True, "
            'physical_authorized=False, physical_effects=False, '
            'viewer_live=False, camera_coverage_validated=False, '
            'coverage_achieved=False)'
        )


class GazeboPrepareClient:
    """Exchange one strict preparation with a fixed protected endpoint."""

    def __init__(
        self,
        socket_path: str = DEFAULT_GAZEBO_PREPARE_SOCKET_PATH,
        *,
        expected_gazebo_uid: int,
        timeout_seconds: float = DEFAULT_GAZEBO_PREPARE_TIMEOUT_SECONDS,
    ) -> None:
        """Fix the absolute endpoint, peer UID, and total timeout."""
        normalized_path = self._configured_socket_path(socket_path)
        if (
            type(expected_gazebo_uid) is not int
            or not 0 <= expected_gazebo_uid <= (1 << 31) - 1
        ):
            raise _error('gazebo_prepare_configuration_invalid')
        if type(timeout_seconds) not in {int, float}:
            raise _error('gazebo_prepare_configuration_invalid')
        timeout = float(timeout_seconds)
        if (
            not math.isfinite(timeout)
            or not 0.0 < timeout
            <= MAX_GAZEBO_PREPARE_TIMEOUT_SECONDS
        ):
            raise _error('gazebo_prepare_configuration_invalid')
        self._socket_path = normalized_path
        self._expected_gazebo_uid = expected_gazebo_uid
        self._timeout_seconds = timeout
        self._configuration_seal = (
            normalized_path,
            expected_gazebo_uid,
            timeout,
        )
        with _CLIENT_SEAL_LOCK:
            _CLIENT_SEALS[self] = self._configuration_seal

    @property
    def socket_path(self) -> str:
        """Return the fixed absolute preparation socket path."""
        return self._socket_path

    @property
    def expected_gazebo_uid(self) -> int:
        """Return the only accepted Linux peer and socket owner UID."""
        return self._expected_gazebo_uid

    @property
    def timeout_seconds(self) -> float:
        """Return the total monotonic transport timeout."""
        return self._timeout_seconds

    def prepare(self, claim: GazeboExecutionClaim) -> _PreparedResponse:
        """Send an exact claim snapshot and return a correlated response."""
        failure: Optional[GazeboPrepareDispatcherError] = None
        try:
            GazeboPrepareClient._attest_configuration(self)
            request = _PrepareRequest.from_claim(claim)
            return GazeboPrepareClient._exchange(self, request)
        except GazeboPrepareDispatcherError as error:
            failure = error
        except socket.timeout:
            failure = _error('gazebo_prepare_timeout')
        except Exception:
            failure = _error('gazebo_prepare_transport_unavailable')
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _exchange(self, request: _PrepareRequest) -> _PreparedResponse:
        payload = request.to_wire_bytes()
        frame = struct.pack('!I', len(payload)) + payload
        deadline = (
            GazeboPrepareClient._transport_monotonic()
            + self.timeout_seconds
        )
        path_snapshot = GazeboPrepareClient._check_socket_path(self)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            GazeboPrepareClient._set_remaining_timeout(
                connection, deadline
            )
            connection.connect(self.socket_path)
            GazeboPrepareClient._check_peer(self, connection)
            if (
                GazeboPrepareClient._check_socket_path(self)
                != path_snapshot
            ):
                raise _error('gazebo_prepare_socket_path_changed')
            GazeboPrepareClient._set_remaining_timeout(
                connection, deadline
            )
            connection.sendall(frame)
            connection.shutdown(socket.SHUT_WR)
            header = GazeboPrepareClient._recv_exact(
                connection, 4, deadline
            )
            size = struct.unpack('!I', header)[0]
            if size < 1:
                raise _error('gazebo_prepare_response_truncated')
            if size > GAZEBO_PREPARE_MAX_RESPONSE_BYTES:
                raise _error('gazebo_prepare_response_too_large')
            response_payload = GazeboPrepareClient._recv_exact(
                connection, size, deadline
            )
            GazeboPrepareClient._set_remaining_timeout(
                connection, deadline
            )
            if connection.recv(1):
                raise _error('gazebo_prepare_response_extra_data')
        finally:
            connection.close()
        response = _PreparedResponse.from_wire_bytes(response_payload)
        if (
            response.request_id != request.request_id
            or response.outbox_id != request.outbox_id
            or response.operation_id != request.operation_id
        ):
            raise _error('gazebo_prepare_response_mismatch')
        return response

    @staticmethod
    def _configured_socket_path(value: Any) -> str:
        invalid = False
        encoded = b''
        if (
            type(value) is not str
            or not value
            or '\x00' in value
            or not os.path.isabs(value)
            or os.path.normpath(value) != value
            or Path(value).name == ''
        ):
            invalid = True
        if not invalid:
            try:
                encoded = os.fsencode(value)
            except (UnicodeEncodeError, ValueError):
                invalid = True
        if invalid or len(encoded) > 103:
            raise _error('gazebo_prepare_configuration_invalid')
        return value

    def _check_socket_path(
        self,
    ) -> Tuple[Tuple[str, int, int, int, int, int, int], ...]:
        """Snapshot every inode without following symlink components."""
        current = Path(self.socket_path).anchor
        snapshot = []
        metadata = None
        for component in Path(self.socket_path).parts[1:]:
            current = os.path.join(current, component)
            try:
                metadata = os.lstat(current)
            except OSError:
                raise _error('gazebo_prepare_socket_unavailable')
            if stat.S_ISLNK(metadata.st_mode):
                raise _error('gazebo_prepare_socket_path_invalid')
            if current != self.socket_path and not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise _error('gazebo_prepare_socket_path_invalid')
            if current != self.socket_path:
                writable = metadata.st_mode & (
                    stat.S_IWGRP | stat.S_IWOTH
                )
                sticky_root = (
                    metadata.st_uid == 0
                    and bool(metadata.st_mode & stat.S_ISVTX)
                )
                if (
                    metadata.st_uid
                    not in {0, self.expected_gazebo_uid}
                    or (writable and not sticky_root)
                ):
                    raise _error(
                        'gazebo_prepare_socket_path_unprotected'
                    )
            snapshot.append(
                (
                    current,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_uid,
                    metadata.st_gid,
                    metadata.st_nlink,
                )
            )
        if metadata is None or not stat.S_ISSOCK(metadata.st_mode):
            raise _error('gazebo_prepare_socket_not_socket')
        parent = snapshot[-2] if len(snapshot) >= 2 else None
        if (
            parent is None
            or parent[4] != self.expected_gazebo_uid
            or parent[3] & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise _error('gazebo_prepare_socket_path_unprotected')
        if metadata.st_uid != self.expected_gazebo_uid:
            raise _error('gazebo_prepare_socket_owner_mismatch')
        if (
            stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise _error('gazebo_prepare_socket_mode_invalid')
        return tuple(snapshot)

    def _check_peer(self, connection: socket.socket) -> None:
        option = getattr(socket, 'SO_PEERCRED', None)
        if option is None:
            raise _error('gazebo_prepare_peer_unavailable')
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
        if invalid:
            raise _error('gazebo_prepare_peer_unavailable')
        if uid != self.expected_gazebo_uid:
            raise _error('gazebo_prepare_peer_uid_mismatch')

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
            cls._set_remaining_timeout(connection, deadline)
            chunk = connection.recv(remaining)
            if not chunk:
                raise _error('gazebo_prepare_response_truncated')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    @staticmethod
    def _set_remaining_timeout(
        connection: socket.socket,
        deadline: float,
    ) -> None:
        remaining = deadline - GazeboPrepareClient._transport_monotonic()
        if not math.isfinite(remaining) or remaining <= 0.0:
            raise socket.timeout()
        connection.settimeout(remaining)

    @staticmethod
    def _transport_monotonic() -> float:
        invalid = False
        value: Any = 0.0
        try:
            value = time.monotonic()
            if (
                type(value) not in {int, float}
                or not math.isfinite(float(value))
                or value < 0.0
            ):
                invalid = True
        except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
            invalid = True
        if invalid:
            raise _error('gazebo_prepare_clock_unavailable')
        return float(value)

    def _attest_configuration(self) -> None:
        seal = getattr(self, '_configuration_seal', None)
        current = (
            getattr(self, '_socket_path', None),
            getattr(self, '_expected_gazebo_uid', None),
            getattr(self, '_timeout_seconds', None),
        )
        external = None
        try:
            with _CLIENT_SEAL_LOCK:
                external = _CLIENT_SEALS.get(self)
        except Exception:
            external = None
        if (
            type(self) is not GazeboPrepareClient
            or type(seal) is not tuple
            or len(seal) != 3
            or external is None
            or external != seal
            or set(getattr(self, '__dict__', {}))
            != {
                '_socket_path',
                '_expected_gazebo_uid',
                '_timeout_seconds',
                '_configuration_seal',
            }
            or type(current[0]) is not str
            or current[0] != seal[0]
            or type(current[1]) is not int
            or current[1] != seal[1]
            or type(current[2]) is not float
            or current[2] != seal[2]
        ):
            raise _error('gazebo_prepare_configuration_changed')


class GazeboPrepareDispatcher:
    """Perform one explicit claim, prepare, and fenced ACK sequence."""

    def __init__(
        self,
        store: SQLiteConversationStore,
        client: GazeboPrepareClient,
        *,
        lease_seconds: int = 30,
    ) -> None:
        """Fix the exact durable store and protected transport client."""
        if (
            type(store) is not SQLiteConversationStore
            or type(client) is not GazeboPrepareClient
            or type(lease_seconds) is not int
            or not GAZEBO_EXECUTION_OUTBOX_MIN_LEASE_SECONDS
            <= lease_seconds
            <= GAZEBO_EXECUTION_OUTBOX_MAX_LEASE_SECONDS
        ):
            raise _error('gazebo_prepare_configuration_invalid')
        client._attest_configuration()
        self._store = store
        self._client = client
        self._lease_seconds = lease_seconds
        self._dispatch_lock = RLock()
        self._configuration_seal = (
            store,
            client,
            lease_seconds,
            self._dispatch_lock,
        )
        with _DISPATCHER_SEAL_LOCK:
            _DISPATCHER_SEALS[self] = self._configuration_seal

    def dispatch_once(
        self,
        claim_request_id: str,
        *,
        expected_outbox_id: Optional[str] = None,
        expected_operation_id: Optional[str] = None,
        expected_confirmation_request_id: Optional[str] = None,
    ) -> Optional[GazeboPrepareDispatchResult]:
        """Commit one lease, prepare it, then persist its fenced ACK."""
        failure: Optional[GazeboPrepareDispatcherError] = None
        try:
            return GazeboPrepareDispatcher._dispatch_once_impl(
                self,
                claim_request_id,
                expected_outbox_id=expected_outbox_id,
                expected_operation_id=expected_operation_id,
                expected_confirmation_request_id=(
                    expected_confirmation_request_id
                ),
            )
        except GazeboPrepareDispatcherError as error:
            failure = error
        except Exception:
            failure = _error('gazebo_prepare_dispatch_unavailable')
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _dispatch_once_impl(
        self,
        claim_request_id: Any,
        *,
        expected_outbox_id: Optional[str],
        expected_operation_id: Optional[str],
        expected_confirmation_request_id: Optional[str],
    ) -> Optional[GazeboPrepareDispatchResult]:
        GazeboPrepareDispatcher._attest_configuration(self)
        normalized_request = _identifier(claim_request_id)
        with self._dispatch_lock:
            GazeboPrepareDispatcher._attest_configuration(self)
            try:
                claim = SQLiteConversationStore.claim_gazebo_execution(
                    self._store,
                    normalized_request,
                    lease_seconds=self._lease_seconds,
                    expected_outbox_id=expected_outbox_id,
                    expected_operation_id=expected_operation_id,
                    expected_confirmation_request_id=(
                        expected_confirmation_request_id
                    ),
                )
            except Exception:
                raise _error('gazebo_prepare_claim_unavailable')
            if claim is None:
                return None
            if (
                type(claim) is not GazeboExecutionClaim
                or claim.claim_request_id != normalized_request
                or (
                    expected_outbox_id is not None
                    and claim.outbox_id != expected_outbox_id
                )
                or (
                    expected_operation_id is not None
                    and claim.operation_id != expected_operation_id
                )
            ):
                raise _error('gazebo_prepare_claim_invalid')
            request = _PrepareRequest.from_claim(claim)
            response = GazeboPrepareClient.prepare(self._client, claim)
            if (
                type(response) is not _PreparedResponse
                or response.request_id != request.request_id
                or response.outbox_id != request.outbox_id
                or response.operation_id != request.operation_id
            ):
                raise _error('gazebo_prepare_response_mismatch')
            try:
                acknowledgement = (
                    SQLiteConversationStore.acknowledge_gazebo_execution(
                        self._store,
                        outbox_id=claim.outbox_id,
                        claim_token=claim.claim_token,
                        claim_fence=claim.claim_fence,
                        prepare_fingerprint=(
                            response.prepare_fingerprint
                        ),
                    )
                )
            except Exception:
                raise _error('gazebo_prepare_ack_unavailable')
            GazeboPrepareDispatcher._validate_acknowledgement(
                acknowledgement,
                claim=claim,
                prepare_fingerprint=response.prepare_fingerprint,
            )
            return GazeboPrepareDispatchResult(
                outbox_id=acknowledgement.outbox_id,
                operation_id=acknowledgement.operation_id,
                claim_fence=acknowledgement.claim_fence,
                prepare_replayed=response.replayed,
            )

    @staticmethod
    def _validate_acknowledgement(
        acknowledgement: Any,
        *,
        claim: GazeboExecutionClaim,
        prepare_fingerprint: str,
    ) -> None:
        if (
            type(acknowledgement) is not GazeboExecutionAcknowledgement
            or acknowledgement.outbox_id != claim.outbox_id
            or acknowledgement.operation_id != claim.operation_id
            or acknowledgement.prepare_request_id
            != claim.prepare_request_id
            or acknowledgement.claim_fence != claim.claim_fence
            or acknowledgement.prepare_fingerprint
            != prepare_fingerprint
            or acknowledgement.state != 'prepared'
        ):
            raise _error('gazebo_prepare_ack_invalid')

    def _attest_configuration(self) -> None:
        seal = getattr(self, '_configuration_seal', None)
        current_store = getattr(self, '_store', None)
        current_client = getattr(self, '_client', None)
        current_lease = getattr(self, '_lease_seconds', None)
        current_lock = getattr(self, '_dispatch_lock', None)
        external = None
        try:
            with _DISPATCHER_SEAL_LOCK:
                external = _DISPATCHER_SEALS.get(self)
        except Exception:
            external = None
        if (
            type(self) is not GazeboPrepareDispatcher
            or type(seal) is not tuple
            or len(seal) != 4
            or external is None
            or external != seal
            or set(getattr(self, '__dict__', {}))
            != {
                '_store',
                '_client',
                '_lease_seconds',
                '_dispatch_lock',
                '_configuration_seal',
            }
            or current_store is not seal[0]
            or current_client is not seal[1]
            or type(current_lease) is not int
            or current_lease != seal[2]
            or current_lock is not seal[3]
            or type(current_store) is not SQLiteConversationStore
            or type(current_client) is not GazeboPrepareClient
        ):
            raise _error('gazebo_prepare_configuration_changed')
        GazeboPrepareClient._attest_configuration(current_client)


__all__ = [
    'DEFAULT_GAZEBO_PREPARE_SOCKET_PATH',
    'DEFAULT_GAZEBO_PREPARE_TIMEOUT_SECONDS',
    'GAZEBO_PREPARE_MAX_REQUEST_BYTES',
    'GAZEBO_PREPARE_MAX_RESPONSE_BYTES',
    'GAZEBO_PREPARE_MAX_SAMPLES',
    'GAZEBO_PREPARE_SCHEMA_VERSION',
    'MAX_GAZEBO_PREPARE_TIMEOUT_SECONDS',
    'GazeboPrepareClient',
    'GazeboPrepareDispatchResult',
    'GazeboPrepareDispatcher',
    'GazeboPrepareDispatcherError',
]
