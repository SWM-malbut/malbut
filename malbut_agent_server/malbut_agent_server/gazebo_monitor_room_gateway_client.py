"""
Strict local client for the Gazebo monitor-room command gateway.

The Agent may send only an opaque request ID, operation ID, and one closed
command over the configured Unix-domain socket.  This module intentionally
duplicates the narrow wire schema instead of importing ``malbut_gazebo`` so
the conversational package never acquires a ROS/Gazebo package dependency.

Every accepted result remains explicitly Gazebo-only: it grants no physical
authority, reports no physical effects, and makes no camera/viewer/coverage
claim.  This module does not import ROS or issue navigation calls directly.
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
from typing import Any, Dict, Tuple
import weakref


GAZEBO_MONITOR_ROOM_GATEWAY_SCHEMA_VERSION = 1
GAZEBO_MONITOR_ROOM_GATEWAY_MAX_REQUEST_BYTES = 2048
GAZEBO_MONITOR_ROOM_GATEWAY_MAX_RESPONSE_BYTES = 4096
DEFAULT_GAZEBO_MONITOR_ROOM_GATEWAY_SOCKET_PATH = (
    '/run/malbut/gazebo-monitor-room-gateway.sock'
)
DEFAULT_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS = 2.0
MAX_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS = 30.0

_COMMANDS = frozenset({'drive', 'observe', 'cancel'})
_NONTERMINAL_STATES = frozenset(
    {
        'prepared',
        'preflighting',
        'send_intent',
        'navigating',
        'cancel_requested',
    }
)
_UNKNOWN_STATES = frozenset({'delivery_unknown', 'cancel_unknown'})
_RESOLVED_TERMINAL_STATES = frozenset(
    {'succeeded', 'failed', 'canceled'}
)
_TERMINAL_STATES = _UNKNOWN_STATES | _RESOLVED_TERMINAL_STATES
_OPERATION_STATES = _NONTERMINAL_STATES | _TERMINAL_STATES
_REQUEST_FIELDS = frozenset(
    {'schema_version', 'request_id', 'operation_id', 'command'}
)
_RESPONSE_FIELDS = frozenset(
    {
        'schema_version',
        'request_id',
        'operation_id',
        'command',
        'state',
        'current_sample_index',
        'navigation_samples_total',
        'navigation_samples_reached',
        'terminal',
        'robot_blocked',
        'terminal_code',
        'evidence_digest',
        'runtime_mode',
        'simulation',
        'physical_authorized',
        'physical_effects',
        'viewer_live',
        'camera_coverage_validated',
        'coverage_achieved',
    }
)
_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_STATE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_ERROR_CODE = re.compile(r'^gazebo_gateway_client_[a-z0-9_]{1,64}$')
_CLIENT_SEAL_LOCK = RLock()
_CLIENT_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)


class GazeboMonitorRoomGatewayClientError(RuntimeError):
    """Content-free failure at the Agent-side local gateway boundary."""

    def __init__(self, code: str) -> None:
        """Expose a stable code without paths, identifiers, or wire data."""
        normalized = (
            code
            if type(code) is str and _ERROR_CODE.fullmatch(code) is not None
            else 'gazebo_gateway_client_unavailable'
        )
        super().__init__('Gazebo monitor-room gateway is unavailable')
        self.code = normalized

    def __getattribute__(self, name: str) -> Any:
        """Keep caught transport exceptions out of public error chains."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


class _InvalidJSON(ValueError):
    """Private decoder control flow for duplicate/non-finite JSON."""


def _error(code: str) -> GazeboMonitorRoomGatewayClientError:
    return GazeboMonitorRoomGatewayClientError(code)


def _identifier(value: Any) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _error('gazebo_gateway_client_request_invalid')
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
        GAZEBO_MONITOR_ROOM_GATEWAY_MAX_RESPONSE_BYTES
        if response
        else GAZEBO_MONITOR_ROOM_GATEWAY_MAX_REQUEST_BYTES
    )
    if invalid or not encoded or len(encoded) > limit:
        code = (
            'gazebo_gateway_client_response_invalid'
            if response
            else 'gazebo_gateway_client_request_invalid'
        )
        raise _error(code)
    return encoded


def _unique_object(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _InvalidJSON()
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise _InvalidJSON()


def _response_mapping(payload: Any) -> Dict[str, Any]:
    if (
        type(payload) is not bytes
        or not payload
        or len(payload)
        > GAZEBO_MONITOR_ROOM_GATEWAY_MAX_RESPONSE_BYTES
    ):
        raise _error('gazebo_gateway_client_response_invalid')
    invalid = False
    value: Any = None
    try:
        value = json.loads(
            payload.decode('utf-8'),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (
        _InvalidJSON,
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        invalid = True
    if invalid or type(value) is not dict or set(value) != _RESPONSE_FIELDS:
        raise _error('gazebo_gateway_client_response_invalid')
    return value


def _validate_result_values(values: Dict[str, Any]) -> None:
    if set(values) != _RESPONSE_FIELDS:
        raise _error('gazebo_gateway_client_response_invalid')
    if (
        type(values['schema_version']) is not int
        or values['schema_version']
        != GAZEBO_MONITOR_ROOM_GATEWAY_SCHEMA_VERSION
    ):
        raise _error('gazebo_gateway_client_response_invalid')
    for name in ('request_id', 'operation_id'):
        if (
            type(values[name]) is not str
            or _IDENTIFIER.fullmatch(values[name]) is None
        ):
            raise _error('gazebo_gateway_client_response_invalid')
    if (
        type(values['command']) is not str
        or values['command'] not in _COMMANDS
        or type(values['state']) is not str
        or values['state'] not in _OPERATION_STATES
    ):
        raise _error('gazebo_gateway_client_response_invalid')
    for name in (
        'current_sample_index',
        'navigation_samples_total',
        'navigation_samples_reached',
    ):
        value = values[name]
        if type(value) is not int or value < 0 or value > 4096:
            raise _error('gazebo_gateway_client_response_invalid')
    total = values['navigation_samples_total']
    index = values['current_sample_index']
    reached = values['navigation_samples_reached']
    if (
        total < 1
        or index >= total
        or reached > total
        or type(values['terminal']) is not bool
        or type(values['robot_blocked']) is not bool
    ):
        raise _error('gazebo_gateway_client_response_invalid')
    state = values['state']
    expected_terminal = state in _TERMINAL_STATES
    expected_blocked = state in (
        _NONTERMINAL_STATES | _UNKNOWN_STATES
    )
    terminal_code = values['terminal_code']
    if (
        values['terminal'] is not expected_terminal
        or values['robot_blocked'] is not expected_blocked
        or (terminal_code is not None) is not expected_terminal
    ):
        raise _error('gazebo_gateway_client_response_invalid')
    if terminal_code is not None and (
        type(terminal_code) is not str
        or _STATE.fullmatch(terminal_code) is None
    ):
        raise _error('gazebo_gateway_client_response_invalid')
    if state == 'succeeded':
        if reached != total:
            raise _error('gazebo_gateway_client_response_invalid')
    elif reached != index:
        raise _error('gazebo_gateway_client_response_invalid')
    if (
        type(values['evidence_digest']) is not str
        or _DIGEST.fullmatch(values['evidence_digest']) is None
        or type(values['runtime_mode']) is not str
        or values['runtime_mode'] != 'gazebo'
        or values['simulation'] is not True
        or values['physical_authorized'] is not False
        or values['physical_effects'] is not False
        or values['viewer_live'] is not False
        or values['camera_coverage_validated'] is not False
        or values['coverage_achieved'] is not False
    ):
        raise _error('gazebo_gateway_client_response_invalid')


@dataclass(frozen=True)
class GazeboMonitorRoomGatewayResult:
    """One immutable, correlated, Gazebo-only operation observation."""

    request_id: str = field(repr=False)
    operation_id: str = field(repr=False)
    command: str
    state: str
    current_sample_index: int
    navigation_samples_total: int
    navigation_samples_reached: int
    terminal: bool
    robot_blocked: bool
    terminal_code: Any = None
    evidence_digest: str = field(default='', repr=False)
    schema_version: int = GAZEBO_MONITOR_ROOM_GATEWAY_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _response_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the complete wire value and cache its identity."""
        values = self.to_dict()
        _validate_result_values(values)
        object.__setattr__(
            self,
            '_response_fingerprint',
            hashlib.sha256(
                _canonical_json_bytes(values, response=True)
            ).hexdigest(),
        )

    @classmethod
    def _from_mapping(
        cls,
        values: Dict[str, Any],
    ) -> 'GazeboMonitorRoomGatewayResult':
        _validate_result_values(values)
        return cls(
            schema_version=values['schema_version'],
            request_id=values['request_id'],
            operation_id=values['operation_id'],
            command=values['command'],
            state=values['state'],
            current_sample_index=values['current_sample_index'],
            navigation_samples_total=values['navigation_samples_total'],
            navigation_samples_reached=(
                values['navigation_samples_reached']
            ),
            terminal=values['terminal'],
            robot_blocked=values['robot_blocked'],
            terminal_code=values['terminal_code'],
            evidence_digest=values['evidence_digest'],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the exact coordinate-free response value."""
        return {
            'schema_version': self.schema_version,
            'request_id': self.request_id,
            'operation_id': self.operation_id,
            'command': self.command,
            'state': self.state,
            'current_sample_index': self.current_sample_index,
            'navigation_samples_total': self.navigation_samples_total,
            'navigation_samples_reached': self.navigation_samples_reached,
            'terminal': self.terminal,
            'robot_blocked': self.robot_blocked,
            'terminal_code': self.terminal_code,
            'evidence_digest': self.evidence_digest,
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

    @property
    def response_fingerprint(self) -> str:
        """Return the canonical response digest and detect mutation."""
        values = self.to_dict()
        _validate_result_values(values)
        current = hashlib.sha256(
            _canonical_json_bytes(values, response=True)
        ).hexdigest()
        if current != self._response_fingerprint:
            raise _error('gazebo_gateway_client_response_invalid')
        return current


class GazeboMonitorRoomGatewayClient:
    """Exchange strict commands with one fixed, same-host Gazebo gateway."""

    def __init__(
        self,
        socket_path: str = (
            DEFAULT_GAZEBO_MONITOR_ROOM_GATEWAY_SOCKET_PATH
        ),
        *,
        expected_server_uid: int,
        timeout_seconds: float = (
            DEFAULT_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS
        ),
    ) -> None:
        """Bind the client to one absolute endpoint and Linux peer UID."""
        if (
            type(socket_path) is not str
            or not socket_path
            or '\x00' in socket_path
            or not os.path.isabs(socket_path)
            or os.path.normpath(socket_path) != socket_path
            or Path(socket_path).name == ''
        ):
            raise ValueError('Gazebo gateway socket path is invalid')
        invalid_path = False
        encoded_path = b''
        try:
            encoded_path = os.fsencode(socket_path)
        except (UnicodeEncodeError, ValueError):
            invalid_path = True
        if invalid_path or len(encoded_path) > 103:
            raise ValueError('Gazebo gateway socket path is invalid')
        if (
            type(expected_server_uid) is not int
            or expected_server_uid < 0
            or expected_server_uid > (1 << 31) - 1
        ):
            raise ValueError('Gazebo gateway server UID is invalid')
        if type(timeout_seconds) not in {int, float}:
            raise ValueError('Gazebo gateway timeout is invalid')
        timeout = float(timeout_seconds)
        if (
            not math.isfinite(timeout)
            or timeout <= 0.0
            or timeout
            > MAX_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS
        ):
            raise ValueError('Gazebo gateway timeout is invalid')
        self._socket_path = socket_path
        self._expected_server_uid = expected_server_uid
        self._timeout_seconds = timeout
        self._configuration_seal = (
            socket_path,
            expected_server_uid,
            timeout,
        )
        with _CLIENT_SEAL_LOCK:
            _CLIENT_SEALS[self] = self._configuration_seal

    @property
    def socket_path(self) -> str:
        """Return the fixed absolute gateway path."""
        return self._socket_path

    @property
    def expected_server_uid(self) -> int:
        """Return the only accepted Linux server UID."""
        return self._expected_server_uid

    @property
    def timeout_seconds(self) -> float:
        """Return the total deadline for a complete framed exchange."""
        return self._timeout_seconds

    def exchange(
        self,
        *,
        request_id: str,
        operation_id: str,
        command: str,
        timeout_seconds: Any = None,
    ) -> GazeboMonitorRoomGatewayResult:
        """Send one command within the fixed or a shorter caller timeout."""
        failure = None
        try:
            GazeboMonitorRoomGatewayClient._attest_configuration(self)
            return GazeboMonitorRoomGatewayClient._exchange_impl(
                self,
                request_id=request_id,
                operation_id=operation_id,
                command=command,
                timeout_seconds=timeout_seconds,
            )
        except GazeboMonitorRoomGatewayClientError as error:
            failure = error
        except socket.timeout:
            failure = _error('gazebo_gateway_client_timeout')
        except Exception:
            failure = _error('gazebo_gateway_client_transport_unavailable')
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _exchange_impl(
        self,
        *,
        request_id: Any,
        operation_id: Any,
        command: Any,
        timeout_seconds: Any = None,
    ) -> GazeboMonitorRoomGatewayResult:
        GazeboMonitorRoomGatewayClient._attest_configuration(self)
        request = GazeboMonitorRoomGatewayClient._request_bytes(
            request_id=request_id,
            operation_id=operation_id,
            command=command,
        )
        frame = struct.pack('!I', len(request)) + request
        effective_timeout = self._timeout_seconds
        if timeout_seconds is not None:
            if type(timeout_seconds) not in {int, float}:
                raise _error('gazebo_gateway_client_timeout_invalid')
            requested_timeout = float(timeout_seconds)
            if (
                not math.isfinite(requested_timeout)
                or requested_timeout <= 0.0
                or requested_timeout
                > MAX_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS
            ):
                raise _error('gazebo_gateway_client_timeout_invalid')
            effective_timeout = min(
                effective_timeout,
                requested_timeout,
            )
        deadline = (
            GazeboMonitorRoomGatewayClient._transport_monotonic()
            + effective_timeout
        )
        path_snapshot = (
            GazeboMonitorRoomGatewayClient._check_socket_path(self)
        )
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            GazeboMonitorRoomGatewayClient._set_remaining_timeout(
                connection, deadline
            )
            connection.connect(self._socket_path)
            GazeboMonitorRoomGatewayClient._check_peer(self, connection)
            if (
                GazeboMonitorRoomGatewayClient._check_socket_path(self)
                != path_snapshot
            ):
                raise _error('gazebo_gateway_client_socket_path_changed')
            GazeboMonitorRoomGatewayClient._set_remaining_timeout(
                connection, deadline
            )
            connection.sendall(frame)
            connection.shutdown(socket.SHUT_WR)
            header = GazeboMonitorRoomGatewayClient._recv_exact(
                connection, 4, deadline
            )
            size = struct.unpack('!I', header)[0]
            if size < 1:
                raise _error('gazebo_gateway_client_response_truncated')
            if size > GAZEBO_MONITOR_ROOM_GATEWAY_MAX_RESPONSE_BYTES:
                raise _error('gazebo_gateway_client_response_too_large')
            payload = GazeboMonitorRoomGatewayClient._recv_exact(
                connection, size, deadline
            )
            GazeboMonitorRoomGatewayClient._set_remaining_timeout(
                connection, deadline
            )
            if connection.recv(1):
                raise _error('gazebo_gateway_client_response_extra_data')
        finally:
            connection.close()
        values = _response_mapping(payload)
        if (
            values['request_id'] != request_id
            or values['operation_id'] != operation_id
            or values['command'] != command
        ):
            raise _error('gazebo_gateway_client_response_mismatch')
        return GazeboMonitorRoomGatewayResult._from_mapping(values)

    @staticmethod
    def _request_bytes(
        *,
        request_id: Any,
        operation_id: Any,
        command: Any,
    ) -> bytes:
        _identifier(request_id)
        _identifier(operation_id)
        if type(command) is not str or command not in _COMMANDS:
            raise _error('gazebo_gateway_client_request_invalid')
        values = {
            'schema_version': (
                GAZEBO_MONITOR_ROOM_GATEWAY_SCHEMA_VERSION
            ),
            'request_id': request_id,
            'operation_id': operation_id,
            'command': command,
        }
        if set(values) != _REQUEST_FIELDS:
            raise _error('gazebo_gateway_client_request_invalid')
        return _canonical_json_bytes(values)

    def _check_socket_path(
        self,
    ) -> Tuple[Tuple[str, int, int, int, int, int], ...]:
        """Snapshot the fixed path without following symlink components."""
        current = Path(self._socket_path).anchor
        snapshot = []
        metadata = None
        for component in Path(self._socket_path).parts[1:]:
            current = os.path.join(current, component)
            unavailable = False
            try:
                metadata = os.lstat(current)
            except OSError:
                unavailable = True
            if unavailable or metadata is None:
                raise _error('gazebo_gateway_client_socket_unavailable')
            if stat.S_ISLNK(metadata.st_mode):
                raise _error('gazebo_gateway_client_socket_path_invalid')
            if current != self._socket_path and not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise _error('gazebo_gateway_client_socket_path_invalid')
            snapshot.append(
                (
                    current,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_uid,
                    metadata.st_gid,
                )
            )
        if metadata is None or not stat.S_ISSOCK(metadata.st_mode):
            raise _error('gazebo_gateway_client_socket_not_socket')
        if metadata.st_uid != self._expected_server_uid:
            raise _error('gazebo_gateway_client_socket_owner_mismatch')
        if (
            stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise _error('gazebo_gateway_client_socket_mode_invalid')
        return tuple(snapshot)

    def _check_peer(self, connection: socket.socket) -> None:
        option = getattr(socket, 'SO_PEERCRED', None)
        if option is None:
            raise _error('gazebo_gateway_client_peer_unavailable')
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
            raise _error('gazebo_gateway_client_peer_unavailable')
        if uid != self._expected_server_uid:
            raise _error('gazebo_gateway_client_peer_uid_mismatch')

    def _attest_configuration(self) -> None:
        """Reject instance replacement, field drift, and method shadowing."""
        seal = getattr(self, '_configuration_seal', None)
        current = (
            getattr(self, '_socket_path', None),
            getattr(self, '_expected_server_uid', None),
            getattr(self, '_timeout_seconds', None),
        )
        external = None
        try:
            with _CLIENT_SEAL_LOCK:
                external = _CLIENT_SEALS.get(self)
        except Exception:
            external = None
        if (
            type(self) is not GazeboMonitorRoomGatewayClient
            or type(seal) is not tuple
            or len(seal) != 3
            or external is None
            or external != seal
            or set(getattr(self, '__dict__', {}))
            != {
                '_socket_path',
                '_expected_server_uid',
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
            raise _error('gazebo_gateway_client_configuration_changed')

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
                raise _error('gazebo_gateway_client_response_truncated')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    @staticmethod
    def _set_remaining_timeout(
        connection: socket.socket,
        deadline: float,
    ) -> None:
        remaining = (
            deadline
            - GazeboMonitorRoomGatewayClient._transport_monotonic()
        )
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
            raise _error('gazebo_gateway_client_clock_unavailable')
        return float(value)


__all__ = [
    'DEFAULT_GAZEBO_MONITOR_ROOM_GATEWAY_SOCKET_PATH',
    'DEFAULT_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS',
    'GAZEBO_MONITOR_ROOM_GATEWAY_MAX_REQUEST_BYTES',
    'GAZEBO_MONITOR_ROOM_GATEWAY_MAX_RESPONSE_BYTES',
    'GAZEBO_MONITOR_ROOM_GATEWAY_SCHEMA_VERSION',
    'MAX_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS',
    'GazeboMonitorRoomGatewayClient',
    'GazeboMonitorRoomGatewayClientError',
    'GazeboMonitorRoomGatewayResult',
]
