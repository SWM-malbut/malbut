"""
Durable, coordinate-free command gateway for Gazebo room monitoring.

The gateway claims every request before calling the injected controller.  A
crash after that claim is recovered by observation only, never by replaying a
possibly delivered Nav2 side effect.  This module does not create ROS entities
or launch navigation on construction.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import struct
from threading import RLock
import time
from typing import Any, Iterator, Optional, Tuple

from malbut_gazebo.gazebo_monitor_room_gateway_contract import (
    GATEWAY_MAX_REQUEST_BYTES,
    GATEWAY_MAX_RESPONSE_BYTES,
    GazeboMonitorRoomGatewayRequest,
    GazeboMonitorRoomGatewayResponse,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    GazeboMonitorRoomNav2Controller,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    GazeboMonitorRoomStore,
)


GATEWAY_REPLAY_SCHEMA_VERSION = 2
# A 4,096-sample operation needs 8,193 distinct ``drive`` requests even
# when every preflight/start succeeds immediately and the following observe
# reports success.  Keep enough append-only replay capacity for that complete
# chain plus bounded retryable observations and cancellation/reconciliation.
GATEWAY_REPLAY_REQUEST_LIMIT = 65536
GATEWAY_SOCKET_MODE = 0o600
GATEWAY_LISTEN_BACKLOG = 8

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_NAMESPACE = re.compile(r'^[0-9a-f]{32}$')
_COMMANDS = frozenset({'drive', 'observe', 'cancel'})
_ERROR_CODES = frozenset(
    {
        'gateway_closed',
        'gateway_configuration_invalid',
        'gateway_operation_unavailable',
        'gateway_replay_conflict',
        'gateway_replay_full',
        'gateway_replay_invalid',
        'gateway_replay_path_invalid',
        'gateway_replay_schema_invalid',
        'gateway_response_unavailable',
        'gateway_socket_closed',
        'gateway_socket_exists',
        'gateway_socket_invalid',
        'gateway_socket_not_started',
        'gateway_socket_peer_rejected',
        'gateway_socket_timeout',
        'gateway_transport_unavailable',
    }
)


class GazeboMonitorRoomGatewayError(RuntimeError):
    """Content-free failure at the durable gateway boundary."""

    def __init__(self, code: str = 'gateway_operation_unavailable') -> None:
        """Keep only a closed public error code."""
        normalized = (
            code if type(code) is str and code in _ERROR_CODES
            else 'gateway_operation_unavailable'
        )
        super().__init__(normalized)
        self.code = normalized

    def __getattribute__(self, name: str) -> Any:
        """Do not expose collaborator exception chains."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


def _raise(code: str) -> None:
    error = GazeboMonitorRoomGatewayError(code)
    error.__cause__ = None
    error.__context__ = None
    error.__traceback__ = None
    raise error


def _digest(value: Any) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _raise('gateway_replay_invalid')
    return value


def _namespace(value: Any) -> str:
    if type(value) is not str or _NAMESPACE.fullmatch(value) is None:
        _raise('gateway_configuration_invalid')
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise('gateway_replay_invalid')
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _raise('gateway_replay_invalid')
    if not math.isfinite(result) or result < 0.0:
        _raise('gateway_replay_invalid')
    if result == 0.0:
        result = 0.0
    return result


def _boottime() -> float:
    """Read the same suspend-inclusive authority clock as the controller."""
    invalid = False
    value: Any = None
    try:
        value = time.clock_gettime(time.CLOCK_BOOTTIME)
    except Exception:
        invalid = True
    if invalid:
        _raise('gateway_replay_invalid')
    return _timestamp(value)


def _transport_now() -> float:
    """Read a transport-only monotonic clock without authority fallback."""
    invalid = False
    value: Any = None
    try:
        value = time.monotonic()
    except Exception:
        invalid = True
    if invalid:
        _raise('gateway_transport_unavailable')
    return _timestamp(value)


def _hash_json(value: Any) -> str:
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
    except (OverflowError, TypeError, UnicodeEncodeError, ValueError):
        invalid = True
    if invalid:
        _raise('gateway_replay_invalid')
    return hashlib.sha256(encoded).hexdigest()


def _bytes_digest(value: bytes) -> str:
    if type(value) is not bytes:
        _raise('gateway_replay_invalid')
    return hashlib.sha256(value).hexdigest()


def _canonical_request(value: Any) -> GazeboMonitorRoomGatewayRequest:
    invalid = False
    result: Any = None
    try:
        if type(value) is not GazeboMonitorRoomGatewayRequest:
            invalid = True
        else:
            result = GazeboMonitorRoomGatewayRequest(
                schema_version=value.schema_version,
                request_id=value.request_id,
                operation_id=value.operation_id,
                command=value.command,
            )
            invalid = (
                result != value
                or result.request_fingerprint != value.request_fingerprint
            )
    except Exception:
        invalid = True
    if invalid:
        _raise('gateway_replay_invalid')
    return result


def _canonical_response(
    request: GazeboMonitorRoomGatewayRequest,
    value: Any,
) -> GazeboMonitorRoomGatewayResponse:
    invalid = False
    result: Any = None
    try:
        if type(value) is not GazeboMonitorRoomGatewayResponse:
            invalid = True
        else:
            result = GazeboMonitorRoomGatewayResponse.from_wire_bytes(
                value.to_wire_bytes()
            )
            invalid = (
                result.request_id != request.request_id
                or result.operation_id != request.operation_id
                or result.command != request.command
                or result.response_fingerprint
                != value.response_fingerprint
            )
    except Exception:
        invalid = True
    if invalid:
        _raise('gateway_response_unavailable')
    return result


def _canonical_path(value: Any) -> Path:
    invalid = False
    try:
        raw = os.fspath(value)
    except TypeError:
        invalid = True
        raw = ''
    if (
        invalid
        or type(raw) is not str
        or not raw
        or '\x00' in raw
        or not os.path.isabs(raw)
        or os.path.normpath(raw) != raw
        or Path(raw).name == ''
    ):
        _raise('gateway_replay_path_invalid')
    try:
        if len(os.fsencode(raw)) > 4096:
            _raise('gateway_replay_path_invalid')
    except (UnicodeEncodeError, ValueError):
        _raise('gateway_replay_path_invalid')
    return Path(raw)


def _canonical_socket_path(value: Any) -> Path:
    path = _canonical_path(value)
    try:
        encoded = os.fsencode(str(path))
    except (UnicodeEncodeError, ValueError):
        _raise('gateway_socket_invalid')
    if len(encoded) > 103:
        _raise('gateway_socket_invalid')
    return path


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
            _raise('gateway_replay_path_invalid')
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
            _raise('gateway_replay_path_invalid')
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
        _raise('gateway_replay_path_invalid')
    final = result[-1]
    if final[4] != euid or final[3] & (stat.S_IWGRP | stat.S_IWOTH):
        _raise('gateway_replay_path_invalid')
    return tuple(result)


def _validate_file(path: Path) -> Tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError:
        _raise('gateway_replay_path_invalid')
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        _raise('gateway_replay_path_invalid')
    return metadata.st_dev, metadata.st_ino


METADATA_TABLE_SQL = '''
CREATE TABLE gazebo_monitor_room_gateway_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 2),
    core_store_namespace TEXT NOT NULL
        CHECK (length(core_store_namespace) = 32),
    request_limit INTEGER NOT NULL CHECK (request_limit = 65536)
)
'''.strip()

CLAIMS_TABLE_SQL = '''
CREATE TABLE gazebo_monitor_room_gateway_claims (
    request_id TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL,
    command TEXT NOT NULL CHECK (command IN ('drive', 'observe', 'cancel')),
    request_wire BLOB NOT NULL CHECK (length(request_wire) BETWEEN 1 AND 2048),
    claimed_at REAL NOT NULL CHECK (claimed_at >= 0),
    claim_digest TEXT NOT NULL CHECK (length(claim_digest) = 64)
)
'''.strip()

COMPLETIONS_TABLE_SQL = '''
CREATE TABLE gazebo_monitor_room_gateway_completions (
    request_id TEXT PRIMARY KEY,
    response_wire BLOB NOT NULL
        CHECK (length(response_wire) BETWEEN 1 AND 4096),
    response_fingerprint TEXT NOT NULL
        CHECK (length(response_fingerprint) = 64),
    completed_at REAL NOT NULL CHECK (completed_at >= 0),
    completion_digest TEXT NOT NULL CHECK (length(completion_digest) = 64),
    FOREIGN KEY (request_id)
        REFERENCES gazebo_monitor_room_gateway_claims (request_id)
        ON DELETE RESTRICT
)
'''.strip()

METADATA_NO_UPDATE_SQL = '''
CREATE TRIGGER gazebo_monitor_room_gateway_metadata_no_update
BEFORE UPDATE ON gazebo_monitor_room_gateway_metadata
BEGIN SELECT RAISE(ABORT, 'gateway metadata is immutable'); END
'''.strip()

METADATA_NO_DELETE_SQL = '''
CREATE TRIGGER gazebo_monitor_room_gateway_metadata_no_delete
BEFORE DELETE ON gazebo_monitor_room_gateway_metadata
BEGIN SELECT RAISE(ABORT, 'gateway metadata is immutable'); END
'''.strip()

CLAIM_NO_UPDATE_SQL = '''
CREATE TRIGGER gazebo_monitor_room_gateway_claim_no_update
BEFORE UPDATE ON gazebo_monitor_room_gateway_claims
BEGIN SELECT RAISE(ABORT, 'gateway claim is immutable'); END
'''.strip()

CLAIM_NO_DELETE_SQL = '''
CREATE TRIGGER gazebo_monitor_room_gateway_claim_no_delete
BEFORE DELETE ON gazebo_monitor_room_gateway_claims
BEGIN SELECT RAISE(ABORT, 'gateway claim is immutable'); END
'''.strip()

COMPLETION_NO_UPDATE_SQL = '''
CREATE TRIGGER gazebo_monitor_room_gateway_completion_no_update
BEFORE UPDATE ON gazebo_monitor_room_gateway_completions
BEGIN SELECT RAISE(ABORT, 'gateway completion is immutable'); END
'''.strip()

COMPLETION_NO_DELETE_SQL = '''
CREATE TRIGGER gazebo_monitor_room_gateway_completion_no_delete
BEFORE DELETE ON gazebo_monitor_room_gateway_completions
BEGIN SELECT RAISE(ABORT, 'gateway completion is immutable'); END
'''.strip()


def _schema() -> Tuple[Tuple[str, str, str], ...]:
    return (
        ('table', 'gazebo_monitor_room_gateway_metadata', METADATA_TABLE_SQL),
        ('table', 'gazebo_monitor_room_gateway_claims', CLAIMS_TABLE_SQL),
        (
            'table',
            'gazebo_monitor_room_gateway_completions',
            COMPLETIONS_TABLE_SQL,
        ),
        (
            'trigger',
            'gazebo_monitor_room_gateway_metadata_no_update',
            METADATA_NO_UPDATE_SQL,
        ),
        (
            'trigger',
            'gazebo_monitor_room_gateway_metadata_no_delete',
            METADATA_NO_DELETE_SQL,
        ),
        (
            'trigger',
            'gazebo_monitor_room_gateway_claim_no_update',
            CLAIM_NO_UPDATE_SQL,
        ),
        (
            'trigger',
            'gazebo_monitor_room_gateway_claim_no_delete',
            CLAIM_NO_DELETE_SQL,
        ),
        (
            'trigger',
            'gazebo_monitor_room_gateway_completion_no_update',
            COMPLETION_NO_UPDATE_SQL,
        ),
        (
            'trigger',
            'gazebo_monitor_room_gateway_completion_no_delete',
            COMPLETION_NO_DELETE_SQL,
        ),
    )


def _normalized_sql(value: Any) -> str:
    if type(value) is not str:
        _raise('gateway_replay_schema_invalid')
    return ' '.join(value.split())


def _claim_digest(
    request: GazeboMonitorRoomGatewayRequest,
    request_wire: bytes,
    claimed_at: float,
) -> str:
    return _hash_json(
        {
            'contract': 'gazebo-monitor-room-gateway-claim-v1',
            'request_id': request.request_id,
            'request_fingerprint': request.request_fingerprint,
            'operation_id': request.operation_id,
            'command': request.command,
            'request_wire_sha256': _bytes_digest(request_wire),
            'claimed_at': claimed_at,
        }
    )


def _completion_digest(
    *,
    request_id: str,
    claim_digest: str,
    response_wire: bytes,
    response_fingerprint: str,
    completed_at: float,
) -> str:
    return _hash_json(
        {
            'contract': 'gazebo-monitor-room-gateway-completion-v1',
            'request_id': request_id,
            'claim_digest': claim_digest,
            'response_wire_sha256': _bytes_digest(response_wire),
            'response_fingerprint': response_fingerprint,
            'completed_at': completed_at,
        }
    )


@dataclass(frozen=True)
class GatewayReplayClaim:
    """Closed result of durable request claiming."""

    first: bool
    recovery_required: bool
    response_wire: Optional[bytes] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Require exactly one first/recovery/completed disposition."""
        if type(self.first) is not bool or type(
            self.recovery_required
        ) is not bool:
            _raise('gateway_replay_invalid')
        completed = self.response_wire is not None
        if completed and type(self.response_wire) is not bytes:
            _raise('gateway_replay_invalid')
        if sum((self.first, self.recovery_required, completed)) != 1:
            _raise('gateway_replay_invalid')


class GazeboMonitorRoomGatewayReplayStore:
    """Append-only request claims and terminal response receipts."""

    def __init__(
        self,
        path: Any,
        *,
        core_store_namespace: str,
        clock=None,
    ) -> None:
        """Open one protected, namespace-bound replay database."""
        self._path = _canonical_path(path)
        self._core_store_namespace = _namespace(core_store_namespace)
        self._clock = _boottime if clock is None else clock
        if not callable(self._clock):
            _raise('gateway_configuration_invalid')
        parents = _validate_parent_chain(self._path)
        created = False
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(self._path, flags, 0o600)
            created = True
            os.close(descriptor)
        except FileExistsError:
            pass
        except OSError:
            _raise('gateway_replay_path_invalid')
        identity = _validate_file(self._path)
        if _validate_parent_chain(self._path) != parents:
            _raise('gateway_replay_path_invalid')
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(
                str(self._path),
                isolation_level=None,
                check_same_thread=False,
                timeout=1.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute('PRAGMA journal_mode = DELETE')
            connection.execute('PRAGMA synchronous = FULL')
            connection.execute('PRAGMA busy_timeout = 1000')
            self._connection = connection
            self._identity = identity
            self._parents = parents
            self._lock = RLock()
            self._closed = False
            self._initialize(created)
        except Exception:
            if connection is not None:
                connection.close()
            if created:
                try:
                    os.unlink(self._path)
                except OSError:
                    pass
            _raise('gateway_replay_schema_invalid')

    def _now(self) -> float:
        invalid = False
        value: Any = None
        try:
            value = self._clock()
        except Exception:
            invalid = True
        if invalid:
            _raise('gateway_replay_invalid')
        return _timestamp(value)

    def _initialize(self, created: bool) -> None:
        with self._lock:
            connection = self._require_connection()
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._attest_locked()
                objects = connection.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE name LIKE 'gazebo_monitor_room_gateway_%'"
                ).fetchall()
                if created:
                    if objects:
                        _raise('gateway_replay_schema_invalid')
                    for object_type, _name, sql in _schema():
                        del object_type
                        connection.execute(sql)
                    connection.execute(
                        '''
                        INSERT INTO gazebo_monitor_room_gateway_metadata (
                            singleton, schema_version,
                            core_store_namespace, request_limit
                        ) VALUES (1, ?, ?, ?)
                        ''',
                        (
                            GATEWAY_REPLAY_SCHEMA_VERSION,
                            self._core_store_namespace,
                            GATEWAY_REPLAY_REQUEST_LIMIT,
                        ),
                    )
                elif not objects:
                    _raise('gateway_replay_schema_invalid')
                self._validate_locked()
                self._attest_locked()
                connection.execute('COMMIT')
                self._attest_locked()
            except Exception:
                try:
                    connection.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
                raise

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed:
            _raise('gateway_closed')
        return self._connection

    def _attest_locked(self) -> None:
        connection = self._require_connection()
        if _validate_parent_chain(self._path) != self._parents:
            _raise('gateway_replay_path_invalid')
        if _validate_file(self._path) != self._identity:
            _raise('gateway_replay_path_invalid')
        database = connection.execute('PRAGMA database_list').fetchall()
        if (
            len(database) != 1
            or database[0]['name'] != 'main'
            or os.path.realpath(database[0]['file']) != str(self._path)
            or connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1
            or connection.execute('PRAGMA journal_mode').fetchone()[0]
            != 'delete'
            or connection.execute('PRAGMA synchronous').fetchone()[0] != 2
        ):
            _raise('gateway_replay_path_invalid')

    def _validate_schema_locked(self) -> None:
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        actual = {
            (row['type'], row['name']): _normalized_sql(row['sql'])
            for row in rows
        }
        expected = {
            (object_type, name): _normalized_sql(sql)
            for object_type, name, sql in _schema()
        }
        if actual != expected:
            _raise('gateway_replay_schema_invalid')

    def _request_from_claim_row(
        self, row: sqlite3.Row
    ) -> GazeboMonitorRoomGatewayRequest:
        if (
            type(row['request_wire']) is not bytes
            or row['request_id_type'] != 'text'
            or row['request_fingerprint_type'] != 'text'
            or row['operation_id_type'] != 'text'
            or row['command_type'] != 'text'
            or row['request_wire_type'] != 'blob'
            or row['claimed_at_type'] not in {'integer', 'real'}
            or row['claim_digest_type'] != 'text'
        ):
            _raise('gateway_replay_invalid')
        try:
            request = GazeboMonitorRoomGatewayRequest.from_wire_bytes(
                row['request_wire']
            )
        except Exception:
            _raise('gateway_replay_invalid')
        claimed_at = _timestamp(row['claimed_at'])
        expected = _claim_digest(request, row['request_wire'], claimed_at)
        if (
            request.request_id != row['request_id']
            or request.request_fingerprint != row['request_fingerprint']
            or request.operation_id != row['operation_id']
            or request.command != row['command']
            or expected != row['claim_digest']
        ):
            _raise('gateway_replay_invalid')
        return request

    def _response_from_rows(
        self,
        claim: sqlite3.Row,
        completion: sqlite3.Row,
    ) -> GazeboMonitorRoomGatewayResponse:
        if (
            type(completion['response_wire']) is not bytes
            or completion['request_id_type'] != 'text'
            or completion['response_wire_type'] != 'blob'
            or completion['response_fingerprint_type'] != 'text'
            or completion['completed_at_type'] not in {'integer', 'real'}
            or completion['completion_digest_type'] != 'text'
        ):
            _raise('gateway_replay_invalid')
        try:
            response = GazeboMonitorRoomGatewayResponse.from_wire_bytes(
                completion['response_wire']
            )
        except Exception:
            _raise('gateway_replay_invalid')
        completed_at = _timestamp(completion['completed_at'])
        claimed_at = _timestamp(claim['claimed_at'])
        expected = _completion_digest(
            request_id=claim['request_id'],
            claim_digest=claim['claim_digest'],
            response_wire=completion['response_wire'],
            response_fingerprint=completion['response_fingerprint'],
            completed_at=completed_at,
        )
        if (
            completion['request_id'] != claim['request_id']
            or completed_at < claimed_at
            or response.request_id != claim['request_id']
            or response.operation_id != claim['operation_id']
            or response.command != claim['command']
            or response.response_fingerprint
            != completion['response_fingerprint']
            or expected != completion['completion_digest']
        ):
            _raise('gateway_replay_invalid')
        return response

    def _claim_rows_locked(self) -> Tuple[sqlite3.Row, ...]:
        return tuple(
            self._require_connection().execute(
                '''
                SELECT *,
                    typeof(request_id) AS request_id_type,
                    typeof(request_fingerprint) AS request_fingerprint_type,
                    typeof(operation_id) AS operation_id_type,
                    typeof(command) AS command_type,
                    typeof(request_wire) AS request_wire_type,
                    typeof(claimed_at) AS claimed_at_type,
                    typeof(claim_digest) AS claim_digest_type
                FROM gazebo_monitor_room_gateway_claims
                ORDER BY request_id
                '''
            ).fetchall()
        )

    def _completion_rows_locked(self) -> Tuple[sqlite3.Row, ...]:
        return tuple(
            self._require_connection().execute(
                '''
                SELECT *,
                    typeof(request_id) AS request_id_type,
                    typeof(response_wire) AS response_wire_type,
                    typeof(response_fingerprint) AS response_fingerprint_type,
                    typeof(completed_at) AS completed_at_type,
                    typeof(completion_digest) AS completion_digest_type
                FROM gazebo_monitor_room_gateway_completions
                ORDER BY request_id
                '''
            ).fetchall()
        )

    def _validate_locked(self) -> None:
        self._validate_schema_locked()
        connection = self._require_connection()
        metadata = connection.execute(
            '''
            SELECT *, typeof(singleton) AS singleton_type,
                typeof(schema_version) AS schema_version_type,
                typeof(core_store_namespace) AS namespace_type,
                typeof(request_limit) AS request_limit_type
            FROM gazebo_monitor_room_gateway_metadata
            '''
        ).fetchall()
        if (
            len(metadata) != 1
            or metadata[0]['singleton'] != 1
            or metadata[0]['schema_version'] != GATEWAY_REPLAY_SCHEMA_VERSION
            or metadata[0]['core_store_namespace']
            != self._core_store_namespace
            or metadata[0]['request_limit'] != GATEWAY_REPLAY_REQUEST_LIMIT
            or tuple(metadata[0][name] for name in (
                'singleton_type', 'schema_version_type',
                'namespace_type', 'request_limit_type'
            )) != ('integer', 'integer', 'text', 'integer')
        ):
            _raise('gateway_replay_schema_invalid')
        claims = self._claim_rows_locked()
        completions = self._completion_rows_locked()
        if (
            len(claims) > GATEWAY_REPLAY_REQUEST_LIMIT
            or len(completions) > len(claims)
        ):
            _raise('gateway_replay_invalid')
        by_id = {}
        for claim in claims:
            request = self._request_from_claim_row(claim)
            if request.request_id in by_id:
                _raise('gateway_replay_invalid')
            by_id[request.request_id] = claim
        completed_ids = set()
        for completion in completions:
            request_id = completion['request_id']
            if request_id in completed_ids or request_id not in by_id:
                _raise('gateway_replay_invalid')
            self._response_from_rows(by_id[request_id], completion)
            completed_ids.add(request_id)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            try:
                self._attest_locked()
                connection.execute('BEGIN IMMEDIATE')
                self._attest_locked()
                self._validate_locked()
                yield connection
                self._validate_locked()
                self._attest_locked()
                connection.execute('COMMIT')
                self._attest_locked()
            except Exception:
                try:
                    connection.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
                raise

    def claim(
        self, request: GazeboMonitorRoomGatewayRequest
    ) -> GatewayReplayClaim:
        """Append the request before any possible controller side effect."""
        failure = None
        try:
            return self._claim_impl(request)
        except GazeboMonitorRoomGatewayError as error:
            failure = error
        except Exception:
            failure = GazeboMonitorRoomGatewayError(
                'gateway_replay_invalid'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _claim_impl(
        self, request: GazeboMonitorRoomGatewayRequest
    ) -> GatewayReplayClaim:
        """Implement claim while the public wrapper strips error context."""
        canonical = _canonical_request(request)
        wire = canonical.to_wire_bytes()
        with self._transaction() as connection:
            row = connection.execute(
                '''
                SELECT *,
                    typeof(request_id) AS request_id_type,
                    typeof(request_fingerprint) AS request_fingerprint_type,
                    typeof(operation_id) AS operation_id_type,
                    typeof(command) AS command_type,
                    typeof(request_wire) AS request_wire_type,
                    typeof(claimed_at) AS claimed_at_type,
                    typeof(claim_digest) AS claim_digest_type
                FROM gazebo_monitor_room_gateway_claims
                WHERE request_id = ?
                ''',
                (canonical.request_id,),
            ).fetchone()
            if row is not None:
                stored = self._request_from_claim_row(row)
                if (
                    stored != canonical
                    or stored.request_fingerprint
                    != canonical.request_fingerprint
                ):
                    _raise('gateway_replay_conflict')
                completion = connection.execute(
                    '''
                    SELECT *,
                        typeof(request_id) AS request_id_type,
                        typeof(response_wire) AS response_wire_type,
                        typeof(response_fingerprint)
                            AS response_fingerprint_type,
                        typeof(completed_at) AS completed_at_type,
                        typeof(completion_digest) AS completion_digest_type
                    FROM gazebo_monitor_room_gateway_completions
                    WHERE request_id = ?
                    ''',
                    (canonical.request_id,),
                ).fetchone()
                if completion is None:
                    return GatewayReplayClaim(
                        first=False,
                        recovery_required=True,
                    )
                response = self._response_from_rows(row, completion)
                return GatewayReplayClaim(
                    first=False,
                    recovery_required=False,
                    response_wire=response.to_wire_bytes(),
                )
            count = connection.execute(
                'SELECT count(*) FROM gazebo_monitor_room_gateway_claims'
            ).fetchone()[0]
            if type(count) is not int or count >= GATEWAY_REPLAY_REQUEST_LIMIT:
                _raise('gateway_replay_full')
            claimed_at = self._now()
            claim_digest = _claim_digest(canonical, wire, claimed_at)
            connection.execute(
                '''
                INSERT INTO gazebo_monitor_room_gateway_claims (
                    request_id, request_fingerprint, operation_id, command,
                    request_wire, claimed_at, claim_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    canonical.request_id,
                    canonical.request_fingerprint,
                    canonical.operation_id,
                    canonical.command,
                    wire,
                    claimed_at,
                    claim_digest,
                ),
            )
            return GatewayReplayClaim(
                first=True,
                recovery_required=False,
            )

    def complete(
        self,
        request: GazeboMonitorRoomGatewayRequest,
        response: GazeboMonitorRoomGatewayResponse,
    ) -> bytes:
        """Append one terminal response receipt, or exact-replay it."""
        failure = None
        try:
            return self._complete_impl(request, response)
        except GazeboMonitorRoomGatewayError as error:
            failure = error
        except Exception:
            failure = GazeboMonitorRoomGatewayError(
                'gateway_replay_invalid'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _complete_impl(
        self,
        request: GazeboMonitorRoomGatewayRequest,
        response: GazeboMonitorRoomGatewayResponse,
    ) -> bytes:
        """Implement completion while its wrapper strips error context."""
        canonical_request = _canonical_request(request)
        canonical_response = _canonical_response(
            canonical_request, response
        )
        response_wire = canonical_response.to_wire_bytes()
        with self._transaction() as connection:
            claim = connection.execute(
                '''
                SELECT *,
                    typeof(request_id) AS request_id_type,
                    typeof(request_fingerprint) AS request_fingerprint_type,
                    typeof(operation_id) AS operation_id_type,
                    typeof(command) AS command_type,
                    typeof(request_wire) AS request_wire_type,
                    typeof(claimed_at) AS claimed_at_type,
                    typeof(claim_digest) AS claim_digest_type
                FROM gazebo_monitor_room_gateway_claims
                WHERE request_id = ?
                ''',
                (canonical_request.request_id,),
            ).fetchone()
            if claim is None:
                _raise('gateway_replay_conflict')
            stored = self._request_from_claim_row(claim)
            if stored != canonical_request:
                _raise('gateway_replay_conflict')
            existing = connection.execute(
                '''
                SELECT *,
                    typeof(request_id) AS request_id_type,
                    typeof(response_wire) AS response_wire_type,
                    typeof(response_fingerprint)
                        AS response_fingerprint_type,
                    typeof(completed_at) AS completed_at_type,
                    typeof(completion_digest) AS completion_digest_type
                FROM gazebo_monitor_room_gateway_completions
                WHERE request_id = ?
                ''',
                (canonical_request.request_id,),
            ).fetchone()
            if existing is not None:
                replay = self._response_from_rows(claim, existing)
                if replay != canonical_response:
                    _raise('gateway_replay_conflict')
                return replay.to_wire_bytes()
            completed_at = self._now()
            if completed_at < _timestamp(claim['claimed_at']):
                _raise('gateway_replay_invalid')
            fingerprint = canonical_response.response_fingerprint
            digest = _completion_digest(
                request_id=canonical_request.request_id,
                claim_digest=claim['claim_digest'],
                response_wire=response_wire,
                response_fingerprint=fingerprint,
                completed_at=completed_at,
            )
            connection.execute(
                '''
                INSERT INTO gazebo_monitor_room_gateway_completions (
                    request_id, response_wire, response_fingerprint,
                    completed_at, completion_digest
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    canonical_request.request_id,
                    response_wire,
                    fingerprint,
                    completed_at,
                    digest,
                ),
            )
            return response_wire

    def close(self) -> None:
        """Close idempotently without removing durable replay evidence."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connection = self._connection
        try:
            connection.close()
        except sqlite3.Error:
            pass


class GazeboMonitorRoomGatewayProcessor:
    """Route strict requests to one store/controller composition."""

    def __init__(
        self,
        store: GazeboMonitorRoomStore,
        controller: GazeboMonitorRoomNav2Controller,
        replay_store: GazeboMonitorRoomGatewayReplayStore,
    ) -> None:
        """Bind trusted local collaborators without executing a command."""
        if (
            type(store) is not GazeboMonitorRoomStore
            or type(controller) is not GazeboMonitorRoomNav2Controller
            or type(replay_store) is not GazeboMonitorRoomGatewayReplayStore
            or controller._store is not store
            or replay_store._core_store_namespace != store.store_namespace
        ):
            _raise('gateway_configuration_invalid')
        self._store = store
        self._controller = controller
        self._replay_store = replay_store
        self._lock = RLock()

    def handle_wire_bytes(self, payload: bytes) -> bytes:
        """Execute once, exact-replay, or recover by observation only."""
        failure = None
        try:
            request = GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)
            with self._lock:
                if (
                    self._controller._store is not self._store
                    or self._replay_store._core_store_namespace
                    != self._store.store_namespace
                ):
                    _raise('gateway_configuration_invalid')
                disposition = self._replay_store.claim(request)
                if disposition.response_wire is not None:
                    cached = GazeboMonitorRoomGatewayResponse.from_wire_bytes(
                        disposition.response_wire
                    )
                    if (
                        cached.request_id != request.request_id
                        or cached.operation_id != request.operation_id
                        or cached.command != request.command
                    ):
                        _raise('gateway_replay_invalid')
                    return cached.to_wire_bytes()
                if disposition.recovery_required:
                    observation = self._store.observe(request.operation_id)
                elif request.command == 'observe':
                    observation = self._store.observe(request.operation_id)
                elif request.command == 'drive':
                    observation = self._controller.drive_once(
                        request.operation_id
                    )
                elif request.command == 'cancel':
                    observation = self._controller.cancel_once(
                        request.operation_id,
                        request.cancel_request_id,
                    )
                else:
                    _raise('gateway_operation_unavailable')
                response = GazeboMonitorRoomGatewayResponse.from_observation(
                    request, observation
                )
                return self._replay_store.complete(request, response)
        except GazeboMonitorRoomGatewayError as error:
            failure = error
        except Exception:
            failure = GazeboMonitorRoomGatewayError(
                'gateway_operation_unavailable'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure


class GazeboMonitorRoomGatewayServer:
    """Serve strict framed commands over one protected Unix socket."""

    def __init__(
        self,
        processor: GazeboMonitorRoomGatewayProcessor,
        socket_path: Any,
        *,
        expected_agent_uid: int,
        timeout_seconds: float = 2.0,
    ) -> None:
        """Bind configuration without creating a socket or issuing commands."""
        if type(processor) is not GazeboMonitorRoomGatewayProcessor:
            _raise('gateway_configuration_invalid')
        if (
            type(expected_agent_uid) is not int
            or expected_agent_uid < 0
            or expected_agent_uid > (1 << 31) - 1
        ):
            _raise('gateway_configuration_invalid')
        timeout = _timestamp(timeout_seconds)
        if timeout <= 0.0 or timeout > 30.0:
            _raise('gateway_configuration_invalid')
        self._processor = processor
        self._socket_path = _canonical_socket_path(socket_path)
        self._expected_agent_uid = expected_agent_uid
        self._timeout_seconds = timeout
        self._lifecycle_lock = RLock()
        self._serve_lock = RLock()
        self._listener: Optional[socket.socket] = None
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._socket_parents: Optional[Tuple[Tuple[Any, ...], ...]] = None
        self._active_connections = set()
        self._closed = False
        self._ever_started = False

    @property
    def socket_path(self) -> str:
        """Return the fixed configured endpoint path."""
        return str(self._socket_path)

    @property
    def expected_agent_uid(self) -> int:
        """Return the only Linux UID allowed to issue commands."""
        return self._expected_agent_uid

    def start(self) -> None:
        """Bind exactly once, rejecting residues and path replacement."""
        failure = None
        try:
            self._start_impl()
            return
        except GazeboMonitorRoomGatewayError as error:
            failure = error
        except Exception:
            failure = GazeboMonitorRoomGatewayError(
                'gateway_transport_unavailable'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def _start_impl(self) -> None:
        with self._lifecycle_lock:
            if self._closed or self._ever_started:
                _raise('gateway_socket_closed')
            parents = _validate_parent_chain(self._socket_path)
            try:
                os.lstat(self._socket_path)
            except FileNotFoundError:
                pass
            except OSError:
                _raise('gateway_socket_invalid')
            else:
                _raise('gateway_socket_exists')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            identity: Optional[Tuple[int, int]] = None
            try:
                listener.bind(str(self._socket_path))
                metadata = os.lstat(self._socket_path)
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                ):
                    _raise('gateway_socket_invalid')
                identity = (metadata.st_dev, metadata.st_ino)
                os.chmod(self._socket_path, GATEWAY_SOCKET_MODE)
                metadata = os.lstat(self._socket_path)
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or (metadata.st_dev, metadata.st_ino) != identity
                    or stat.S_IMODE(metadata.st_mode)
                    != GATEWAY_SOCKET_MODE
                    or _validate_parent_chain(self._socket_path) != parents
                ):
                    _raise('gateway_socket_invalid')
                listener.listen(GATEWAY_LISTEN_BACKLOG)
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
        """Serve one peer within a single bounded transport deadline."""
        failure = None
        try:
            self._serve_once_impl()
            return
        except GazeboMonitorRoomGatewayError as error:
            failure = error
        except socket.timeout:
            failure = GazeboMonitorRoomGatewayError(
                'gateway_socket_timeout'
            )
        except Exception:
            failure = GazeboMonitorRoomGatewayError(
                'gateway_transport_unavailable'
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
                    _raise('gateway_socket_not_started')
                self._attest_socket_locked()
            deadline = _transport_now() + self._timeout_seconds
            connection = self._accept(listener, deadline)
            with self._lifecycle_lock:
                if self._closed:
                    connection.close()
                    _raise('gateway_socket_closed')
                try:
                    self._attest_socket_locked()
                except Exception:
                    connection.close()
                    raise
                self._active_connections.add(connection)
            try:
                self._check_peer(connection)
                header = self._recv_exact(connection, 4, deadline)
                size = struct.unpack('!I', header)[0]
                if size < 1 or size > GATEWAY_MAX_REQUEST_BYTES:
                    _raise('gateway_socket_invalid')
                payload = self._recv_exact(connection, size, deadline)
                self._set_timeout(connection, deadline)
                trailing = connection.recv(1)
                if trailing:
                    _raise('gateway_socket_invalid')
                response = self._processor.handle_wire_bytes(payload)
                if (
                    type(response) is not bytes
                    or not response
                    or len(response) > GATEWAY_MAX_RESPONSE_BYTES
                ):
                    _raise('gateway_response_unavailable')
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
        """Contain malformed clients while preserving fatal store failures."""
        recoverable = {
            'gateway_operation_unavailable',
            'gateway_replay_conflict',
            'gateway_response_unavailable',
            'gateway_socket_invalid',
            'gateway_socket_peer_rejected',
            'gateway_socket_timeout',
            'gateway_transport_unavailable',
        }
        while True:
            with self._lifecycle_lock:
                if self._closed:
                    return
            try:
                self.serve_once()
            except GazeboMonitorRoomGatewayError as error:
                with self._lifecycle_lock:
                    if self._closed:
                        return
                if error.code not in recoverable:
                    raise

    def close(self) -> None:
        """Stop serving and unlink only the exact socket inode we bound."""
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
            _raise('gateway_socket_not_started')
        try:
            metadata = os.lstat(self._socket_path)
        except OSError:
            _raise('gateway_socket_invalid')
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != GATEWAY_SOCKET_MODE
            or _validate_parent_chain(self._socket_path) != parents
        ):
            _raise('gateway_socket_invalid')

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
            _raise('gateway_socket_peer_rejected')
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
            _raise('gateway_socket_peer_rejected')

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
                        _raise('gateway_socket_closed')

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
                _raise('gateway_socket_invalid')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)


__all__ = [
    'GATEWAY_REPLAY_REQUEST_LIMIT',
    'GATEWAY_REPLAY_SCHEMA_VERSION',
    'GatewayReplayClaim',
    'GazeboMonitorRoomGatewayError',
    'GazeboMonitorRoomGatewayProcessor',
    'GazeboMonitorRoomGatewayReplayStore',
    'GazeboMonitorRoomGatewayServer',
]
