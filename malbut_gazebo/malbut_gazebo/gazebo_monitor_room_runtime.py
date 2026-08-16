"""
Default-off production composition for Gazebo monitor-room execution.

The runtime owns two protected Unix sockets.  One accepts exact durable
preparations from the Agent server and the other accepts coordinate-free
``drive``/``observe``/``cancel`` commands.  Merely loading, constructing, or
starting this runtime never prepares an operation, drives the controller,
sends a NavigateToPose goal, cancels a goal, or changes Homecam streaming.
"""

from dataclasses import InitVar, dataclass, field
import argparse
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from threading import current_thread, Event, RLock, Thread
from typing import Any, Dict, Mapping, Optional, Tuple
import weakref

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter

from malbut_agent_server.homecam_semantic import (
    AuthenticatedHomecamSemanticResolver,
    HomecamSemanticConfig,
    VerifiedSemanticSnapshotEvidence,
)
from malbut_gazebo.gazebo_monitor_room_active_map import (
    ActiveMapEvidenceResolver,
    ActiveMapResolverConfig,
    ActiveMapStaticNavigationProjection,
)
from malbut_gazebo.gazebo_monitor_room_gateway import (
    GazeboMonitorRoomGatewayProcessor,
    GazeboMonitorRoomGatewayReplayStore,
    GazeboMonitorRoomGatewayServer,
)
from malbut_gazebo.gazebo_monitor_room_live_ros_source import (
    GazeboMonitorRoomLiveRosSource,
    GazeboMonitorRoomRclpyLiveRosFacade,
)
from malbut_gazebo.gazebo_monitor_room_live_validator import (
    GazeboMonitorRoomLiveValidator,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    GazeboMonitorRoomNav2Controller,
)
from malbut_gazebo.gazebo_monitor_room_nav2_ros_port import (
    GazeboMonitorRoomNav2RosPort,
)
from malbut_gazebo.gazebo_monitor_room_prepare_gateway import (
    GazeboMonitorRoomPrepareProcessor,
    GazeboMonitorRoomPrepareServer,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    GAZEBO_MONITOR_ROOM_MAX_LEASE_SECONDS,
    GazeboMonitorRoomStore,
)


GAZEBO_MONITOR_ROOM_RUNTIME_SCHEMA_VERSION = 1
GAZEBO_MONITOR_ROOM_RUNTIME_MAX_CONFIG_BYTES = 32 * 1024
GAZEBO_MONITOR_ROOM_RUNTIME_DEFAULT_CONFIG = (
    '/etc/malbut/gazebo-monitor-room-runtime.json'
)

_CONFIG_TOKEN = object()
_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_BOOT_ID = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$'
)
_DISABLED_FIELDS = frozenset({'schema_version', 'enabled'})
_ENABLED_FIELDS = frozenset({
    'schema_version',
    'enabled',
    'robot_id',
    'worker_id',
    'expected_agent_uid',
    'host_boot_id',
    'map_store_path',
    'map_store_owner_uid',
    'expected_map_id',
    'expected_map_revision',
    'core_database_path',
    'gateway_replay_database_path',
    'prepare_socket_path',
    'gateway_socket_path',
    'homecam_origin',
    'homecam_service_token',
    'homecam_envelope_signing_secret',
    'homecam_agent_user_id',
    'homecam_principal_subject_digest',
    'homecam_device_id',
    'homecam_timeout_seconds',
    'lease_seconds',
    'socket_timeout_seconds',
    'nav2_response_timeout_seconds',
    'nav2_cancel_timeout_seconds',
})
_CONFIG_SEAL_LOCK = RLock()
_CONFIG_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)
_SEMANTIC_SEAL_LOCK = RLock()
_SEMANTIC_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)
_RUNTIME_SEAL_LOCK = RLock()
_RUNTIME_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)
_TARGET_SEAL_LOCK = RLock()
_TARGET_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)
_VERIFIED_SEMANTIC_CANONICAL_COPY_UNBOUND = (
    VerifiedSemanticSnapshotEvidence.canonical_copy
)

_SERVER_MUTABLE_FIELDS = frozenset({
    '_listener',
    '_socket_identity',
    '_socket_parents',
    '_active_connections',
    '_closed',
    '_ever_started',
    'started',
    'served',
    'closed',
})
_RESOURCE_MUTABLE_FIELDS = frozenset({
    '_closed',
    '_last_now',
    '_status_snapshot_seen',
    '_status_snapshot_valid',
    '_dispatch_tracking_exhausted',
    '_cancel_tracking_exhausted',
    'closed',
})


class GazeboMonitorRoomRuntimeError(RuntimeError):
    """Expose only bounded runtime failures without private configuration."""

    _CODES = frozenset({
        'runtime_config_invalid',
        'runtime_config_changed',
        'runtime_disabled',
        'runtime_binding_invalid',
        'runtime_unavailable',
        'runtime_already_started',
        'runtime_closed',
        'runtime_worker_failed',
    })

    def __init__(self, code: str = 'runtime_unavailable') -> None:
        """Create one content-free failure code."""
        normalized = (
            code if type(code) is str and code in self._CODES
            else 'runtime_unavailable'
        )
        super().__init__(normalized)
        self.code = normalized

    def __getattribute__(self, name: str) -> Any:
        """Keep private paths and collaborators out of error chains."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


def _raise(code: str) -> None:
    raise GazeboMonitorRoomRuntimeError(code)


def _field_seal(value: Any) -> tuple:
    if type(value) in {str, bool, int, float, bytes, type(None)}:
        return type(value), value
    return type(value), id(value)


def _register_trusted_target(
    target: Any,
    method_names: tuple[str, ...],
    *,
    mutable_fields: frozenset[str],
) -> None:
    invalid = False
    seal = None
    try:
        storage = object.__getattribute__(target, '__dict__')
        target_type = type(target)
        methods = tuple(
            (name, inspect.getattr_static(target_type, name))
            for name in method_names
        )
        if (
            type(storage) is not dict
            or any(not callable(method) for _name, method in methods)
            or any(name in storage for name in method_names)
        ):
            raise TypeError
        stable = tuple(
            (name, _field_seal(value))
            for name, value in sorted(storage.items())
            if name not in mutable_fields
        )
        seal = (
            target_type,
            frozenset(storage),
            stable,
            methods,
            mutable_fields,
        )
    except Exception:
        invalid = True
    if invalid or seal is None:
        _raise('runtime_binding_invalid')
    with _TARGET_SEAL_LOCK:
        existing = _TARGET_SEALS.get(target)
        if existing is not None and existing != seal:
            _raise('runtime_binding_invalid')
        _TARGET_SEALS[target] = seal


def _trusted_target_method(target: Any, method_name: str) -> Any:
    invalid = False
    method = None
    try:
        with _TARGET_SEAL_LOCK:
            seal = _TARGET_SEALS.get(target)
        storage = object.__getattribute__(target, '__dict__')
        if (
            type(seal) is not tuple
            or len(seal) != 5
            or type(target) is not seal[0]
            or type(storage) is not dict
            or frozenset(storage) != seal[1]
            or method_name in storage
        ):
            raise TypeError
        stable = tuple(
            (name, _field_seal(value))
            for name, value in sorted(storage.items())
            if name not in seal[4]
        )
        methods = dict(seal[3])
        method = methods.get(method_name)
        if (
            stable != seal[2]
            or method is None
            or inspect.getattr_static(type(target), method_name) is not method
        ):
            raise TypeError
    except Exception:
        invalid = True
    if invalid or method is None:
        _raise('runtime_binding_invalid')
    return method


def _call_trusted_target(
    target: Any,
    method_name: str,
    *arguments: Any,
    **keywords: Any,
) -> Any:
    method = _trusted_target_method(target, method_name)
    return method(target, *arguments, **keywords)


def _identifier(value: Any) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _raise('runtime_config_invalid')
    return value


def _digest(value: Any) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _raise('runtime_config_invalid')
    return value


def _uid(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= (1 << 31) - 1:
        _raise('runtime_config_invalid')
    return value


def _positive_seconds(value: Any, maximum: float) -> float:
    if (
        type(value) not in (int, float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= maximum
    ):
        _raise('runtime_config_invalid')
    return float(value)


def _absolute_path(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or '\x00' in value
        or not value.isascii()
        or not os.path.isabs(value)
        or os.path.normpath(value) != value
        or value == os.path.sep
    ):
        _raise('runtime_config_invalid')
    return value


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except Exception:
        _raise('runtime_config_invalid')


def _unique_object(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError


def _strict_json(payload: bytes) -> Mapping[str, Any]:
    failed = False
    value: Any = None
    try:
        value = json.loads(
            payload.decode('utf-8'),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except Exception:
        failed = True
    if failed or type(value) is not dict:
        _raise('runtime_config_invalid')
    return value


def _validate_parent_chain(path: Path) -> Tuple[Tuple[Any, ...], ...]:
    current = Path(path.anchor)
    result = []
    euid = os.geteuid()
    for part in path.parent.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError:
            _raise('runtime_config_invalid')
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
            _raise('runtime_config_invalid')
        result.append((
            str(current),
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
        ))
    if not result:
        _raise('runtime_config_invalid')
    final = result[-1]
    if final[4] != euid or final[3] & (stat.S_IWGRP | stat.S_IWOTH):
        _raise('runtime_config_invalid')
    return tuple(result)


def _read_private_config(path_value: Any) -> tuple:
    invalid = False
    raw_path = ''
    try:
        raw_path = os.fspath(path_value)
    except TypeError:
        invalid = True
    if invalid or type(raw_path) is not str:
        _raise('runtime_config_invalid')
    path = Path(_absolute_path(raw_path))
    parents = _validate_parent_chain(path)
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = None
    failed = False
    payload = b''
    before = None
    after = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 1
            or before.st_size > GAZEBO_MONITOR_ROOM_RUNTIME_MAX_CONFIG_BYTES
        ):
            raise ValueError
        chunks = []
        remaining = GAZEBO_MONITOR_ROOM_RUNTIME_MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b''.join(chunks)
        after = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            len(payload) != before.st_size
            or len(payload) > GAZEBO_MONITOR_ROOM_RUNTIME_MAX_CONFIG_BYTES
            or (before.st_dev, before.st_ino, before.st_mode,
                before.st_uid, before.st_nlink, before.st_size)
            != (after.st_dev, after.st_ino, after.st_mode,
                after.st_uid, after.st_nlink, after.st_size)
            or (current.st_dev, current.st_ino, current.st_mode,
                current.st_uid, current.st_nlink, current.st_size)
            != (after.st_dev, after.st_ino, after.st_mode,
                after.st_uid, after.st_nlink, after.st_size)
            or _validate_parent_chain(path) != parents
        ):
            raise ValueError
    except Exception:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed or before is None or after is None:
        _raise('runtime_config_invalid')
    identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_nlink,
        after.st_size,
    )
    return str(path), identity, hashlib.sha256(payload).hexdigest(), payload


def _read_current_boot_id() -> str:
    value = ''
    failed = False
    try:
        with open(
            '/proc/sys/kernel/random/boot_id',
            'r',
            encoding='ascii',
        ) as stream:
            value = stream.read(128).strip().lower()
    except Exception:
        failed = True
    if failed or _BOOT_ID.fullmatch(value) is None:
        _raise('runtime_binding_invalid')
    return value


@dataclass(frozen=True, repr=False, eq=False)
class GazeboMonitorRoomRuntimeConfig:
    """Resolver-issued snapshot of one protected runtime configuration."""

    schema_version: int
    enabled: bool
    robot_id: Optional[str] = field(default=None, repr=False)
    worker_id: Optional[str] = field(default=None, repr=False)
    expected_agent_uid: Optional[int] = field(default=None, repr=False)
    host_boot_id: Optional[str] = field(default=None, repr=False)
    map_store_path: Optional[str] = field(default=None, repr=False)
    map_store_owner_uid: Optional[int] = field(default=None, repr=False)
    expected_map_id: Optional[str] = field(default=None, repr=False)
    expected_map_revision: Optional[str] = field(default=None, repr=False)
    core_database_path: Optional[str] = field(default=None, repr=False)
    gateway_replay_database_path: Optional[str] = field(
        default=None, repr=False
    )
    prepare_socket_path: Optional[str] = field(default=None, repr=False)
    gateway_socket_path: Optional[str] = field(default=None, repr=False)
    homecam_origin: Optional[str] = field(default=None, repr=False)
    homecam_service_token: Optional[str] = field(default=None, repr=False)
    homecam_envelope_signing_secret: Optional[str] = field(
        default=None, repr=False
    )
    homecam_agent_user_id: Optional[str] = field(default=None, repr=False)
    homecam_principal_subject_digest: Optional[str] = field(
        default=None, repr=False
    )
    homecam_device_id: Optional[str] = field(default=None, repr=False)
    homecam_timeout_seconds: Optional[int] = field(default=None, repr=False)
    lease_seconds: Optional[float] = field(default=None, repr=False)
    socket_timeout_seconds: Optional[float] = field(default=None, repr=False)
    nav2_response_timeout_seconds: Optional[float] = field(
        default=None, repr=False
    )
    nav2_cancel_timeout_seconds: Optional[float] = field(
        default=None, repr=False
    )
    _source_path: str = field(default='', repr=False)
    _source_identity: tuple = field(default=(), repr=False)
    _source_digest: str = field(default='', repr=False)
    _values_digest: str = field(default='', repr=False)
    _construction_token: InitVar[object] = None

    def __getattribute__(self, name: str) -> Any:
        """Bypass instance shadows for the trusted attestation boundary."""
        if name == 'assert_current':
            method = globals().get('_CONFIG_ASSERT_CURRENT_UNBOUND')
            if callable(method):
                return method.__get__(self, type(self))
        return object.__getattribute__(self, name)

    def __post_init__(self, _construction_token: object) -> None:
        """Reject direct construction and externally seal current values."""
        if _construction_token is not _CONFIG_TOKEN:
            raise TypeError('runtime config must come from its loader')
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != GAZEBO_MONITOR_ROOM_RUNTIME_SCHEMA_VERSION
            or type(self.enabled) is not bool
            or not self._source_path
            or type(self._source_identity) is not tuple
            or len(self._source_identity) != 6
            or _SHA256.fullmatch(self._source_digest) is None
            or _SHA256.fullmatch(self._values_digest) is None
        ):
            _raise('runtime_config_invalid')
        seal = _CONFIG_SEAL_VALUE_UNBOUND(self)
        with _CONFIG_SEAL_LOCK:
            _CONFIG_SEALS[self] = seal

    def __repr__(self) -> str:
        """Avoid rendering paths, identities, or Homecam credentials."""
        state = 'enabled' if self.enabled else 'disabled'
        return f'GazeboMonitorRoomRuntimeConfig({state}, <redacted>)'

    def _seal_value(self) -> tuple:
        storage = object.__getattribute__(self, '__dict__')
        expected_keys = {
            item.name for item in self.__dataclass_fields__.values()
            if item.name != '_construction_token'
        }
        if type(storage) is not dict or set(storage) != expected_keys:
            _raise('runtime_config_changed')
        return tuple(
            (name, type(storage[name]), storage[name])
            for name in sorted(expected_keys)
        )

    def assert_current(self) -> None:
        """Revalidate the object, protected file, and host boot."""
        invalid = False
        expected = None
        current = None
        try:
            current = _CONFIG_SEAL_VALUE_UNBOUND(self)
            with _CONFIG_SEAL_LOCK:
                expected = _CONFIG_SEALS.get(self)
        except Exception:
            invalid = True
        if invalid or expected is None or current != expected:
            _raise('runtime_config_changed')
        try:
            path, identity, source_digest, payload = _read_private_config(
                self._source_path
            )
        except GazeboMonitorRoomRuntimeError:
            _raise('runtime_config_changed')
        if (
            path != self._source_path
            or identity != self._source_identity
            or source_digest != self._source_digest
            or hashlib.sha256(
                _canonical_json(_strict_json(payload))
            ).hexdigest()
            != self._values_digest
        ):
            _raise('runtime_config_changed')
        if self.enabled and _read_current_boot_id() != self.host_boot_id:
            _raise('runtime_binding_invalid')


_CONFIG_SEAL_VALUE_UNBOUND = GazeboMonitorRoomRuntimeConfig._seal_value
_CONFIG_ASSERT_CURRENT_UNBOUND = (
    GazeboMonitorRoomRuntimeConfig.assert_current
)


def load_gazebo_monitor_room_runtime_config(
    path: Any,
) -> GazeboMonitorRoomRuntimeConfig:
    """Read one exact private JSON file without following a symlink."""
    source_path, identity, source_digest, payload = _read_private_config(path)
    value = _strict_json(payload)
    fields = frozenset(value)
    if (
        type(value.get('schema_version')) is not int
        or value.get('schema_version')
        != GAZEBO_MONITOR_ROOM_RUNTIME_SCHEMA_VERSION
        or type(value.get('enabled')) is not bool
    ):
        _raise('runtime_config_invalid')
    values_digest = hashlib.sha256(_canonical_json(value)).hexdigest()
    if value['enabled'] is False:
        if fields != _DISABLED_FIELDS:
            _raise('runtime_config_invalid')
        return GazeboMonitorRoomRuntimeConfig(
            schema_version=value['schema_version'],
            enabled=False,
            _source_path=source_path,
            _source_identity=identity,
            _source_digest=source_digest,
            _values_digest=values_digest,
            _construction_token=_CONFIG_TOKEN,
        )
    if fields != _ENABLED_FIELDS:
        _raise('runtime_config_invalid')
    robot_id = _identifier(value['robot_id'])
    worker_id = _identifier(value['worker_id'])
    host_boot_id = value['host_boot_id']
    if (
        type(host_boot_id) is not str
        or _BOOT_ID.fullmatch(host_boot_id) is None
    ):
        _raise('runtime_config_invalid')
    paths = {
        name: _absolute_path(value[name])
        for name in (
            'map_store_path',
            'core_database_path',
            'gateway_replay_database_path',
            'prepare_socket_path',
            'gateway_socket_path',
        )
    }
    if len(set(paths.values())) != len(paths):
        _raise('runtime_config_invalid')
    expected_map_id = _identifier(value['expected_map_id'])
    expected_map_revision = _identifier(value['expected_map_revision'])
    expected_agent_uid = _uid(value['expected_agent_uid'])
    map_store_owner_uid = _uid(value['map_store_owner_uid'])
    principal_digest = _digest(
        value['homecam_principal_subject_digest']
    )
    homecam = None
    try:
        homecam = HomecamSemanticConfig(
            origin=value['homecam_origin'],
            service_token=value['homecam_service_token'],
            envelope_signing_secret=(
                value['homecam_envelope_signing_secret']
            ),
            agent_user_id=value['homecam_agent_user_id'],
            principal_subject_digest=principal_digest,
            device_id=value['homecam_device_id'],
            timeout_seconds=value['homecam_timeout_seconds'],
        )
    except Exception:
        _raise('runtime_config_invalid')
    assert homecam is not None
    if robot_id != homecam.device_id:
        _raise('runtime_binding_invalid')
    config = GazeboMonitorRoomRuntimeConfig(
        schema_version=value['schema_version'],
        enabled=True,
        robot_id=robot_id,
        worker_id=worker_id,
        expected_agent_uid=expected_agent_uid,
        host_boot_id=host_boot_id,
        map_store_path=paths['map_store_path'],
        map_store_owner_uid=map_store_owner_uid,
        expected_map_id=expected_map_id,
        expected_map_revision=expected_map_revision,
        core_database_path=paths['core_database_path'],
        gateway_replay_database_path=paths[
            'gateway_replay_database_path'
        ],
        prepare_socket_path=paths['prepare_socket_path'],
        gateway_socket_path=paths['gateway_socket_path'],
        homecam_origin=homecam.origin,
        homecam_service_token=homecam.service_token,
        homecam_envelope_signing_secret=(
            homecam.envelope_signing_secret
        ),
        homecam_agent_user_id=homecam.agent_user_id,
        homecam_principal_subject_digest=(
            homecam.principal_subject_digest
        ),
        homecam_device_id=homecam.device_id,
        homecam_timeout_seconds=homecam.timeout_seconds,
        lease_seconds=_positive_seconds(
            value['lease_seconds'],
            GAZEBO_MONITOR_ROOM_MAX_LEASE_SECONDS,
        ),
        socket_timeout_seconds=_positive_seconds(
            value['socket_timeout_seconds'], 30.0
        ),
        nav2_response_timeout_seconds=_positive_seconds(
            value['nav2_response_timeout_seconds'], 30.0
        ),
        nav2_cancel_timeout_seconds=_positive_seconds(
            value['nav2_cancel_timeout_seconds'], 30.0
        ),
        _source_path=source_path,
        _source_identity=identity,
        _source_digest=source_digest,
        _values_digest=values_digest,
        _construction_token=_CONFIG_TOKEN,
    )
    _CONFIG_ASSERT_CURRENT_UNBOUND(config)
    return config


class _BoundSemanticEvidenceSource:
    """Keep Homecam semantic evidence on one configured device and map."""

    def __init__(
        self,
        resolver: AuthenticatedHomecamSemanticResolver,
        *,
        expected_device_id: str,
        expected_map_id: str,
        expected_map_revision: str,
    ) -> None:
        if type(resolver) is not AuthenticatedHomecamSemanticResolver:
            _raise('runtime_binding_invalid')
        fetch = resolver.fetch_snapshot_evidence
        if not callable(fetch):
            _raise('runtime_binding_invalid')
        self._resolver = resolver
        self._fetch = fetch
        self._expected_device_id = _identifier(expected_device_id)
        self._expected_map_id = _identifier(expected_map_id)
        self._expected_map_revision = _identifier(expected_map_revision)
        self._token = object()
        with _SEMANTIC_SEAL_LOCK:
            _SEMANTIC_SEALS[self] = (
                _BOUND_SEMANTIC_SEAL_VALUE_UNBOUND(self),
                fetch,
                self._expected_device_id,
                self._expected_map_id,
                self._expected_map_revision,
            )

    @staticmethod
    def _object_state(value: Any) -> tuple:
        storage = object.__getattribute__(value, '__dict__')
        if type(storage) is not dict:
            _raise('runtime_binding_invalid')
        return tuple(
            (
                name,
                type(item),
                item
                if type(item) in {str, bool, int, float, type(None)}
                else id(item),
            )
            for name, item in sorted(storage.items())
        )

    @staticmethod
    def _resolver_state(resolver: Any) -> tuple:
        storage = object.__getattribute__(resolver, '__dict__')
        if type(storage) is not dict:
            _raise('runtime_binding_invalid')
        nested = []
        for name in ('_config', 'config', '_transport', '_effects'):
            value = storage.get(name)
            if value is not None:
                nested.append((
                    name,
                    _BOUND_SEMANTIC_OBJECT_STATE_UNBOUND(value),
                ))
        return (
            _BOUND_SEMANTIC_OBJECT_STATE_UNBOUND(resolver),
            tuple(nested),
        )

    def _seal_value(self) -> tuple:
        storage = object.__getattribute__(self, '__dict__')
        if type(storage) is not dict or set(storage) != {
            '_resolver',
            '_fetch',
            '_expected_device_id',
            '_expected_map_id',
            '_expected_map_revision',
            '_token',
        }:
            _raise('runtime_binding_invalid')
        return (
            id(storage['_resolver']),
            id(storage['_fetch'].__self__),
            id(storage['_fetch'].__func__),
            storage['_expected_device_id'],
            storage['_expected_map_id'],
            storage['_expected_map_revision'],
            id(storage['_token']),
            _BOUND_SEMANTIC_RESOLVER_STATE_UNBOUND(
                storage['_resolver']
            ),
        )

    def _attest(self) -> Any:
        invalid = False
        current = None
        roots = None
        try:
            current = _BOUND_SEMANTIC_SEAL_VALUE_UNBOUND(self)
            with _SEMANTIC_SEAL_LOCK:
                roots = _SEMANTIC_SEALS.get(self)
        except Exception:
            invalid = True
        if (
            invalid
            or type(roots) is not tuple
            or len(roots) != 5
            or current != roots[0]
            or not callable(roots[1])
            or any(type(value) is not str for value in roots[2:])
        ):
            _raise('runtime_binding_invalid')
        return roots[1:]

    def fetch_snapshot_evidence(self) -> VerifiedSemanticSnapshotEvidence:
        """Fetch, detach, and enforce the configured device/map tuple."""
        roots = _BOUND_SEMANTIC_ATTEST_UNBOUND(self)
        fetch = roots[0]
        evidence = fetch()
        if type(evidence) is not VerifiedSemanticSnapshotEvidence:
            _raise('runtime_binding_invalid')
        try:
            canonical = _VERIFIED_SEMANTIC_CANONICAL_COPY_UNBOUND(
                evidence
            )
            if type(canonical) is not VerifiedSemanticSnapshotEvidence:
                raise TypeError
            snapshot = canonical.snapshot
        except BaseException:
            _raise('runtime_binding_invalid')
        if (
            snapshot.device_id != roots[1]
            or snapshot.map_id != roots[2]
            or snapshot.map_revision != roots[3]
        ):
            _raise('runtime_binding_invalid')
        if _BOUND_SEMANTIC_ATTEST_UNBOUND(self) != roots:
            _raise('runtime_binding_invalid')
        return canonical


_BOUND_SEMANTIC_OBJECT_STATE_UNBOUND = (
    _BoundSemanticEvidenceSource._object_state
)
_BOUND_SEMANTIC_RESOLVER_STATE_UNBOUND = (
    _BoundSemanticEvidenceSource._resolver_state
)
_BOUND_SEMANTIC_SEAL_VALUE_UNBOUND = (
    _BoundSemanticEvidenceSource._seal_value
)
_BOUND_SEMANTIC_ATTEST_UNBOUND = _BoundSemanticEvidenceSource._attest


@dataclass(frozen=True, repr=False)
class _RuntimeComponents:
    store: Any
    prepare_server: Any
    active_map_resolver: Any
    semantic_evidence_source: Any
    live_ros_facade: Any
    live_evidence_source: Any
    live_validator: Any
    nav2_port: Any
    controller: Any
    replay_store: Any
    gateway_server: Any


@dataclass(frozen=True, repr=False)
class _WorkerHandle:
    thread: Any
    thread_type: type
    start: Any
    join: Any
    is_alive: Any
    initial_fields: frozenset[str]


def _capture_worker(thread: Any) -> _WorkerHandle:
    invalid = False
    handle = None
    try:
        storage = object.__getattribute__(thread, '__dict__')
        thread_type = type(thread)
        start = inspect.getattr_static(thread_type, 'start')
        join = inspect.getattr_static(thread_type, 'join')
        is_alive = inspect.getattr_static(thread_type, 'is_alive')
        if (
            type(storage) is not dict
            or any(name in storage for name in ('start', 'join', 'is_alive'))
            or not all(callable(value) for value in (start, join, is_alive))
        ):
            raise TypeError
        handle = _WorkerHandle(
            thread=thread,
            thread_type=thread_type,
            start=start,
            join=join,
            is_alive=is_alive,
            initial_fields=frozenset(storage),
        )
    except Exception:
        invalid = True
    if invalid or handle is None:
        _raise('runtime_binding_invalid')
    return handle


def _worker_method(handle: _WorkerHandle, name: str) -> Any:
    invalid = False
    method = None
    try:
        storage = object.__getattribute__(handle.thread, '__dict__')
        method = getattr(handle, name)
        if (
            type(handle) is not _WorkerHandle
            or type(handle.thread) is not handle.thread_type
            or type(storage) is not dict
            or not frozenset(storage).issubset(handle.initial_fields)
            or name in storage
            or inspect.getattr_static(handle.thread_type, name) is not method
            or not callable(method)
        ):
            raise TypeError
    except Exception:
        invalid = True
    if invalid or method is None:
        _raise('runtime_binding_invalid')
    return method


class GazeboMonitorRoomRuntime:
    """Own the composed stores, ROS entities, and two server workers."""

    def __getattribute__(self, name: str) -> Any:
        """Bypass instance shadows for public lifecycle boundaries."""
        methods = {
            'start': '_RUNTIME_START_UNBOUND',
            'assert_healthy': '_RUNTIME_HEALTH_UNBOUND',
            'close': '_RUNTIME_CLOSE_UNBOUND',
        }
        root_name = methods.get(name)
        if root_name is not None:
            method = globals().get(root_name)
            if callable(method):
                return method.__get__(self, type(self))
        return object.__getattribute__(self, name)

    def __init__(
        self,
        config: GazeboMonitorRoomRuntimeConfig,
        components: _RuntimeComponents,
    ) -> None:
        """Retain a completed composition without starting either server."""
        if (
            type(config) is not GazeboMonitorRoomRuntimeConfig
            or not config.enabled
            or type(components) is not _RuntimeComponents
        ):
            _raise('runtime_binding_invalid')
        _CONFIG_ASSERT_CURRENT_UNBOUND(config)
        prepare_server = components.prepare_server
        gateway_server = components.gateway_server
        nav2_port = components.nav2_port
        replay_store = components.replay_store
        store = components.store
        _register_trusted_target(
            prepare_server,
            ('start', 'serve_forever', 'close'),
            mutable_fields=_SERVER_MUTABLE_FIELDS,
        )
        _register_trusted_target(
            gateway_server,
            ('start', 'serve_forever', 'close'),
            mutable_fields=_SERVER_MUTABLE_FIELDS,
        )
        for resource in (nav2_port, replay_store, store):
            _register_trusted_target(
                resource,
                ('close',),
                mutable_fields=_RESOURCE_MUTABLE_FIELDS,
            )
        self._config = config
        self._components = components
        self._lock = RLock()
        self._lifecycle_lock = RLock()
        self._failure = Event()
        self._failure_code: Optional[str] = None
        self._threads: tuple[_WorkerHandle, ...] = ()
        self._threads_started = 0
        self._starting = False
        self._started = False
        self._closed = False
        self._server_close_index = 0
        self._resource_close_index = 0
        self._token = object()
        with _RUNTIME_SEAL_LOCK:
            _RUNTIME_SEALS[self] = (
                config,
                components,
                self._lock,
                self._failure,
                self._token,
                tuple(
                    getattr(components, name)
                    for name in components.__dataclass_fields__
                ),
                self._lifecycle_lock,
            )

    def _trusted_roots(self) -> tuple:
        invalid = False
        seal = None
        storage = None
        try:
            with _RUNTIME_SEAL_LOCK:
                seal = _RUNTIME_SEALS.get(self)
            storage = object.__getattribute__(self, '__dict__')
            if (
                type(storage) is not dict
                or set(storage) != {
                    '_config',
                    '_components',
                    '_lock',
                    '_lifecycle_lock',
                    '_failure',
                    '_failure_code',
                    '_threads',
                    '_threads_started',
                    '_starting',
                    '_started',
                    '_closed',
                    '_server_close_index',
                    '_resource_close_index',
                    '_token',
                }
                or type(seal) is not tuple
                or len(seal) != 7
                or storage['_config'] is not seal[0]
                or storage['_components'] is not seal[1]
                or storage['_lock'] is not seal[2]
                or storage['_failure'] is not seal[3]
                or storage['_token'] is not seal[4]
                or storage['_lifecycle_lock'] is not seal[6]
                or type(storage['_threads']) is not tuple
                or any(
                    type(worker) is not _WorkerHandle
                    for worker in storage['_threads']
                )
                or type(storage['_threads_started']) is not int
                or not 0 <= storage['_threads_started'] <= len(
                    storage['_threads']
                )
                or type(storage['_starting']) is not bool
                or type(storage['_started']) is not bool
                or type(storage['_closed']) is not bool
                or type(storage['_server_close_index']) is not int
                or not 0 <= storage['_server_close_index'] <= 2
                or type(storage['_resource_close_index']) is not int
                or not 0 <= storage['_resource_close_index'] <= 3
                or (
                    storage['_failure_code'] is not None
                    and storage['_failure_code'] != 'runtime_worker_failed'
                )
                or any(
                    current is not expected
                    for current, expected in zip(
                        (
                            getattr(seal[1], name)
                            for name in seal[1].__dataclass_fields__
                        ),
                        seal[5],
                    )
                )
            ):
                invalid = True
        except Exception:
            invalid = True
        if invalid or seal is None:
            _raise('runtime_binding_invalid')
        return seal

    @staticmethod
    def _component(seal: tuple, name: str) -> Any:
        names = tuple(_RuntimeComponents.__dataclass_fields__)
        try:
            index = names.index(name)
            return seal[5][index]
        except Exception:
            _raise('runtime_binding_invalid')

    @property
    def enabled(self) -> bool:
        """Report the only supported constructed state."""
        GazeboMonitorRoomRuntime._trusted_roots(self)
        return True

    @property
    def prepare_socket_path(self) -> str:
        """Return the fixed protected prepare endpoint."""
        seal = GazeboMonitorRoomRuntime._trusted_roots(self)
        server = GazeboMonitorRoomRuntime._component(
            seal, 'prepare_server'
        )
        _trusted_target_method(server, 'close')
        return server.socket_path

    @property
    def gateway_socket_path(self) -> str:
        """Return the fixed coordinate-free command endpoint."""
        seal = GazeboMonitorRoomRuntime._trusted_roots(self)
        server = GazeboMonitorRoomRuntime._component(
            seal, 'gateway_server'
        )
        _trusted_target_method(server, 'close')
        return server.socket_path

    def _worker(self, server: Any) -> None:
        try:
            seal = _RUNTIME_TRUSTED_ROOTS_UNBOUND(self)
            lock = seal[2]
            _call_trusted_target(server, 'serve_forever')
            with lock:
                expected_close = self._closed
            if not expected_close:
                raise RuntimeError
        except BaseException:
            try:
                with _RUNTIME_SEAL_LOCK:
                    roots = _RUNTIME_SEALS.get(self)
                if type(roots) is tuple and len(roots) == 7:
                    with roots[2]:
                        if not self._closed:
                            self._failure_code = 'runtime_worker_failed'
                            roots[3].set()
            except Exception:
                return

    def start(self) -> None:
        """Bind both UDS listeners and start non-executor serve loops only."""
        seal = _RUNTIME_TRUSTED_ROOTS_UNBOUND(self)
        lifecycle_lock = seal[6]
        with lifecycle_lock:
            seal = _RUNTIME_TRUSTED_ROOTS_UNBOUND(self)
            config = seal[0]
            lock = seal[2]
            _CONFIG_ASSERT_CURRENT_UNBOUND(config)
            semantic_source = _RUNTIME_COMPONENT_UNBOUND(
                seal, 'semantic_evidence_source'
            )
            _BOUND_SEMANTIC_ATTEST_UNBOUND(semantic_source)
            with lock:
                if self._closed:
                    _raise('runtime_closed')
                if self._started or self._starting:
                    _raise('runtime_already_started')
                self._starting = True
            prepare = _RUNTIME_COMPONENT_UNBOUND(
                seal, 'prepare_server'
            )
            gateway = _RUNTIME_COMPONENT_UNBOUND(
                seal, 'gateway_server'
            )
            started_count = 0
            try:
                _call_trusted_target(prepare, 'start')
                _call_trusted_target(gateway, 'start')
                workers = (
                    _capture_worker(Thread(
                        target=_RUNTIME_WORKER_UNBOUND,
                        args=(self, prepare),
                        name='malbut-monitor-room-prepare',
                        daemon=True,
                    )),
                    _capture_worker(Thread(
                        target=_RUNTIME_WORKER_UNBOUND,
                        args=(self, gateway),
                        name='malbut-monitor-room-command',
                        daemon=True,
                    )),
                )
                with lock:
                    self._threads = workers
                for worker in workers:
                    _worker_method(worker, 'start')(worker.thread)
                    started_count += 1
                    with lock:
                        self._threads_started = started_count
                if any(
                    not _worker_method(worker, 'is_alive')(
                        worker.thread
                    )
                    for worker in workers
                ):
                    raise RuntimeError
                with lock:
                    if self._closed:
                        raise RuntimeError
                    self._starting = False
                    self._threads_started = started_count
                    self._started = True
                return
            except BaseException:
                with lock:
                    self._starting = False
                    self._started = False
                    self._threads_started = len(self._threads)
                    self._closed = True
                    self._failure_code = 'runtime_worker_failed'
                    self._failure.set()
                try:
                    _RUNTIME_FINISH_SHUTDOWN_UNBOUND(self, seal)
                except Exception:
                    pass
                _raise('runtime_worker_failed')

    def assert_healthy(self) -> None:
        """Fail closed if a UDS worker terminated unexpectedly."""
        seal = _RUNTIME_TRUSTED_ROOTS_UNBOUND(self)
        lock = seal[2]
        failure_event = seal[3]
        with lock:
            if self._closed:
                _raise('runtime_closed')
            if not self._started:
                _raise('runtime_unavailable')
            workers = self._threads[:self._threads_started]
            failed = failure_event.is_set() or any(
                not _worker_method(worker, 'is_alive')(worker.thread)
                for worker in workers
            )
        if failed:
            _raise('runtime_worker_failed')

    def _finish_shutdown(self, seal: tuple) -> None:
        """Close listeners, prove workers stopped, then close dependencies."""
        lock = seal[2]
        servers = (
            _RUNTIME_COMPONENT_UNBOUND(seal, 'gateway_server'),
            _RUNTIME_COMPONENT_UNBOUND(seal, 'prepare_server'),
        )
        while True:
            with lock:
                index = self._server_close_index
            if index >= len(servers):
                break
            try:
                _call_trusted_target(servers[index], 'close')
            except Exception:
                _raise('runtime_worker_failed')
            with lock:
                if self._server_close_index == index:
                    self._server_close_index += 1
        with lock:
            workers = self._threads[:self._threads_started]
        join_failed = False
        for worker in workers:
            if worker.thread is current_thread():
                join_failed = True
                continue
            try:
                if not _worker_method(worker, 'is_alive')(
                    worker.thread
                ):
                    continue
                _worker_method(worker, 'join')(
                    worker.thread, timeout=2.0
                )
            except Exception:
                join_failed = True
        for worker in workers:
            try:
                if _worker_method(worker, 'is_alive')(worker.thread):
                    join_failed = True
            except Exception:
                join_failed = True
        if join_failed:
            _raise('runtime_worker_failed')
        resources = (
            _RUNTIME_COMPONENT_UNBOUND(seal, 'nav2_port'),
            _RUNTIME_COMPONENT_UNBOUND(seal, 'replay_store'),
            _RUNTIME_COMPONENT_UNBOUND(seal, 'store'),
        )
        while True:
            with lock:
                index = self._resource_close_index
            if index >= len(resources):
                break
            try:
                _call_trusted_target(resources[index], 'close')
            except Exception:
                _raise('runtime_worker_failed')
            with lock:
                if self._resource_close_index == index:
                    self._resource_close_index += 1

    def close(self) -> None:
        """Stop workers before closing their Nav2 and database dependencies."""
        seal = _RUNTIME_TRUSTED_ROOTS_UNBOUND(self)
        lifecycle_lock = seal[6]
        with lifecycle_lock:
            seal = _RUNTIME_TRUSTED_ROOTS_UNBOUND(self)
            lock = seal[2]
            with lock:
                if self._resource_close_index >= 3:
                    return
                self._closed = True
                self._starting = False
                self._started = False
            _RUNTIME_FINISH_SHUTDOWN_UNBOUND(self, seal)


_RUNTIME_TRUSTED_ROOTS_UNBOUND = GazeboMonitorRoomRuntime._trusted_roots
_RUNTIME_COMPONENT_UNBOUND = GazeboMonitorRoomRuntime._component
_RUNTIME_WORKER_UNBOUND = GazeboMonitorRoomRuntime._worker
_RUNTIME_START_UNBOUND = GazeboMonitorRoomRuntime.start
_RUNTIME_HEALTH_UNBOUND = GazeboMonitorRoomRuntime.assert_healthy
_RUNTIME_FINISH_SHUTDOWN_UNBOUND = (
    GazeboMonitorRoomRuntime._finish_shutdown
)
_RUNTIME_CLOSE_UNBOUND = GazeboMonitorRoomRuntime.close


def _build_enabled_runtime(
    config: GazeboMonitorRoomRuntimeConfig,
    node: Node,
) -> GazeboMonitorRoomRuntime:
    """Compose production collaborators without starting or dispatching."""
    if type(config) is not GazeboMonitorRoomRuntimeConfig:
        _raise('runtime_config_invalid')
    _CONFIG_ASSERT_CURRENT_UNBOUND(config)
    if config.enabled is not True:
        _raise('runtime_disabled')
    resources = []
    try:
        store = GazeboMonitorRoomStore(
            config.core_database_path,
            boot_id_reader=lambda: config.host_boot_id,
        )
        _register_trusted_target(
            store,
            ('close',),
            mutable_fields=_RESOURCE_MUTABLE_FIELDS,
        )
        resources.append(store)
        prepare_processor = GazeboMonitorRoomPrepareProcessor(
            store,
            expected_robot_id=config.robot_id,
            local_boot_id=config.host_boot_id,
        )
        prepare_server = GazeboMonitorRoomPrepareServer(
            prepare_processor,
            config.prepare_socket_path,
            expected_agent_uid=config.expected_agent_uid,
            timeout_seconds=config.socket_timeout_seconds,
        )
        active_map = ActiveMapEvidenceResolver(
            ActiveMapResolverConfig(
                map_store_path=config.map_store_path,
                owner_uid=config.map_store_owner_uid,
            )
        )
        projection = (
            ActiveMapEvidenceResolver.resolve_static_navigation_projection(
                active_map
            )
        )
        if type(projection) is not ActiveMapStaticNavigationProjection:
            _raise('runtime_binding_invalid')
        active_evidence = projection.active_map_evidence
        if (
            active_evidence.map_id != config.expected_map_id
            or active_evidence.map_revision
            != config.expected_map_revision
        ):
            _raise('runtime_binding_invalid')
        semantic_config = HomecamSemanticConfig(
            origin=config.homecam_origin,
            service_token=config.homecam_service_token,
            envelope_signing_secret=(
                config.homecam_envelope_signing_secret
            ),
            agent_user_id=config.homecam_agent_user_id,
            principal_subject_digest=(
                config.homecam_principal_subject_digest
            ),
            device_id=config.homecam_device_id,
            timeout_seconds=config.homecam_timeout_seconds,
        )
        semantic_resolver = AuthenticatedHomecamSemanticResolver(
            semantic_config
        )
        semantic_source = _BoundSemanticEvidenceSource(
            semantic_resolver,
            expected_device_id=config.homecam_device_id,
            expected_map_id=config.expected_map_id,
            expected_map_revision=config.expected_map_revision,
        )
        live_facade = GazeboMonitorRoomRclpyLiveRosFacade(node)
        live_source = GazeboMonitorRoomLiveRosSource(live_facade)
        validator = GazeboMonitorRoomLiveValidator(
            store,
            semantic_source,
            active_map,
            live_source,
        )
        nav2_port = GazeboMonitorRoomNav2RosPort(
            node,
            validator=validator,
            response_timeout_seconds=(
                config.nav2_response_timeout_seconds
            ),
            cancel_timeout_seconds=config.nav2_cancel_timeout_seconds,
        )
        _register_trusted_target(
            nav2_port,
            ('close',),
            mutable_fields=_RESOURCE_MUTABLE_FIELDS,
        )
        resources.append(nav2_port)
        controller = GazeboMonitorRoomNav2Controller(
            store,
            nav2_port,
            worker_id=config.worker_id,
            lease_seconds=config.lease_seconds,
        )
        replay_store = GazeboMonitorRoomGatewayReplayStore(
            config.gateway_replay_database_path,
            core_store_namespace=store.store_namespace,
        )
        _register_trusted_target(
            replay_store,
            ('close',),
            mutable_fields=_RESOURCE_MUTABLE_FIELDS,
        )
        resources.append(replay_store)
        gateway_processor = GazeboMonitorRoomGatewayProcessor(
            store,
            controller,
            replay_store,
        )
        gateway_server = GazeboMonitorRoomGatewayServer(
            gateway_processor,
            config.gateway_socket_path,
            expected_agent_uid=config.expected_agent_uid,
            timeout_seconds=config.socket_timeout_seconds,
        )
        components = _RuntimeComponents(
            store=store,
            prepare_server=prepare_server,
            active_map_resolver=active_map,
            semantic_evidence_source=semantic_source,
            live_ros_facade=live_facade,
            live_evidence_source=live_source,
            live_validator=validator,
            nav2_port=nav2_port,
            controller=controller,
            replay_store=replay_store,
            gateway_server=gateway_server,
        )
        return GazeboMonitorRoomRuntime(config, components)
    except GazeboMonitorRoomRuntimeError:
        failure = True
    except Exception:
        failure = True
    if failure:
        for resource in reversed(resources):
            try:
                _call_trusted_target(resource, 'close')
            except Exception:
                pass
        _raise('runtime_unavailable')
    raise AssertionError


def build_gazebo_monitor_room_runtime(
    config: GazeboMonitorRoomRuntimeConfig,
    node: Node,
) -> GazeboMonitorRoomRuntime:
    """Build only when a current protected configuration says enabled."""
    return _build_enabled_runtime(config, node)


def _parse_arguments(args: Optional[list[str]]) -> tuple[Any, list[str]]:
    parser = argparse.ArgumentParser(
        description='Run the default-off Gazebo monitor-room bridge.'
    )
    parser.add_argument(
        '--config',
        required=True,
        help='Absolute path to a private mode-0600 runtime JSON file.',
    )
    return parser.parse_known_args(args)


def _main(args: Optional[list[str]] = None) -> int:
    parsed, ros_arguments = _parse_arguments(args)
    config = load_gazebo_monitor_room_runtime_config(parsed.config)
    if not config.enabled:
        return 0
    rclpy.init(args=ros_arguments)
    node = None
    executor = None
    runtime = None
    try:
        node = Node(
            'gazebo_monitor_room_runtime',
            parameter_overrides=[Parameter('use_sim_time', value=True)],
            automatically_declare_parameters_from_overrides=True,
        )
        runtime = build_gazebo_monitor_room_runtime(config, node)
        _RUNTIME_START_UNBOUND(runtime)
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        while rclpy.ok():
            _RUNTIME_HEALTH_UNBOUND(runtime)
            executor.spin_once(timeout_sec=0.1)
        return 0
    finally:
        if runtime is not None:
            _RUNTIME_CLOSE_UNBOUND(runtime)
        if executor is not None and node is not None:
            try:
                executor.remove_node(node)
            except Exception:
                pass
        if node is not None:
            try:
                node.destroy_node()
            except Exception:
                pass
        if rclpy.ok():
            rclpy.shutdown()


def main(args: Optional[list[str]] = None) -> int:
    """Run with bounded diagnostics and no implicit enablement."""
    failure = None
    try:
        return _main(args)
    except GazeboMonitorRoomRuntimeError as error:
        failure = error.code
    except Exception:
        failure = 'runtime_unavailable'
    sys.stderr.write(f'{failure}\n')
    return 2


__all__ = [
    'GAZEBO_MONITOR_ROOM_RUNTIME_DEFAULT_CONFIG',
    'GAZEBO_MONITOR_ROOM_RUNTIME_SCHEMA_VERSION',
    'GazeboMonitorRoomRuntime',
    'GazeboMonitorRoomRuntimeConfig',
    'GazeboMonitorRoomRuntimeError',
    'build_gazebo_monitor_room_runtime',
    'load_gazebo_monitor_room_runtime_config',
    'main',
]
