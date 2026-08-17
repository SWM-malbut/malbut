"""
Read-only, fail-closed evidence for the currently active saved map.

The resolver in this module is intentionally narrower than the map lifecycle
helpers.  It accepts one administrator-configured absolute map-store path and
never accepts a path on an individual request.  Every returned value is built
from one bounded snapshot of ``active.json`` and the three map files named by
that manifest.  No ROS API or mutable map-management API is reachable here.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import re
import stat
from threading import RLock
from typing import Any
import weakref

import cv2
import numpy as np
import yaml

from malbut_gazebo.gazebo_monitor_room_navigation_safety import (
    MAX_GRID_CELLS,
    MAX_GRID_DIMENSION,
    NavigationSafetyInputError,
    StaticClearanceGrid,
)


ACTIVE_MAP_EVIDENCE_SCHEMA_VERSION = 1
ACTIVE_MAP_STATIC_PROJECTION_SCHEMA_VERSION = 1
MAP_STORE_FORMAT = 'malbut-map-store/v1'
USER_MAP_FORMAT = 'malbut-user-map-v1'
ACTIVE_MANIFEST_NAME = 'active.json'

MAX_ACTIVE_MANIFEST_BYTES = 64 * 1024
MAX_MAP_YAML_BYTES = 64 * 1024
MAX_MAP_IMAGE_BYTES = 128 * 1024 * 1024
MAX_USER_MAP_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_MAP_DIMENSION = 100_000
MAX_MAP_PIXELS = 128 * 1024 * 1024
MAX_STATIC_PROJECTION_DIMENSION = MAX_GRID_DIMENSION
MAX_STATIC_PROJECTION_CELLS = MAX_GRID_CELLS

_READ_CHUNK_BYTES = 64 * 1024
_MAP_ID = re.compile(r'^map-[0-9a-f]{12}$')
_MAP_REVISION = re.compile(r'^rev-[0-9a-f]{12}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_ACTIVE_REVISION = re.compile(
    r'^[0-9]{8}T[0-9]{6}Z-([0-9a-f]{10})(?:-[2-9][0-9]*)?$'
)
_PGM_DIMENSIONS = re.compile(rb'^[1-9][0-9]{0,5} [1-9][0-9]{0,5}$')
_PGM_PREFIX = b'P5\n# CREATOR: Malbut map lifecycle\n'
_PGM_MAXIMUM = b'255\n'
_EVIDENCE_CONSTRUCTION_TOKEN = object()
_PROJECTION_CONSTRUCTION_TOKEN = object()
_STATIC_PROJECTION_SCOPE = 'active-map-static-navigation-projection-v1'
_STATIC_OCCUPANCY_SEMANTICS = 'ros-map-server-trinary-v1'
_STATIC_CLEARANCE_METRIC = 'euclidean-cell-center-v1'


class ActiveMapError(RuntimeError):
    """Base class for content-free current-map failures."""

    def __init__(self, code: str, message: str) -> None:
        """Create an error with a stable public code and fixed message."""
        super().__init__(message)
        self.code = code


class ActiveMapUnavailableError(ActiveMapError):
    """Raised when no complete active map snapshot can be opened."""


class ActiveMapValidationError(ActiveMapError):
    """Raised when an active map snapshot is malformed or untrusted."""


class ActiveMapChangedError(ActiveMapError):
    """Raised when any source changes while evidence is being built."""


class ActiveMapEvidenceInvalidError(ActiveMapError):
    """Raised when an issued evidence object no longer matches its baseline."""


class ActiveMapProjectionInvalidError(ActiveMapError):
    """Raised when an issued static navigation projection was modified."""


class _MapFailure(Exception):
    """Private control-flow failure without source content."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _failure(kind: str) -> None:
    raise _MapFailure(kind)


def _public_failure(kind: str) -> ActiveMapError:
    if kind == 'unavailable':
        return ActiveMapUnavailableError(
            'active_map_unavailable',
            'Active map evidence is unavailable',
        )
    if kind == 'changed':
        return ActiveMapChangedError(
            'active_map_changed',
            'Active map changed while evidence was captured',
        )
    return ActiveMapValidationError(
        'active_map_invalid',
        'Active map evidence is invalid',
    )


def _exact_integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _failure('invalid')
    return value


def _finite_number(value: Any, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        _failure('invalid')
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized < minimum
        or normalized > maximum
    ):
        _failure('invalid')
    return normalized


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    invalid = False
    encoded = b''
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        invalid = True
    if invalid:
        _failure('invalid')
    return encoded


def _static_projection_digest(
    evidence_digest: str,
    clearance_digest: str,
) -> str:
    """Bind one static clearance result to its exact active-map evidence."""
    return _sha256(_canonical_json({
        'schema_version': ACTIVE_MAP_STATIC_PROJECTION_SCHEMA_VERSION,
        'scope': _STATIC_PROJECTION_SCOPE,
        'active_map_evidence_digest': evidence_digest,
        'static_clearance_digest': clearance_digest,
        'occupancy_semantics': _STATIC_OCCUPANCY_SEMANTICS,
        'unknown_is_obstacle': True,
        'off_map_is_obstacle': True,
        'clearance_metric': _STATIC_CLEARANCE_METRIC,
    }))


@dataclass(frozen=True, repr=False)
class ActiveMapResolverConfig:
    """Fixed, administrator-owned source configuration for one resolver."""

    map_store_path: str = field(repr=False)
    owner_uid: int = field(default_factory=os.geteuid, repr=False)

    def __post_init__(self) -> None:
        """Reject relative, normalized-by-surprise, or root paths."""
        path = self.map_store_path
        if (
            type(path) is not str
            or not path
            or '\x00' in path
            or not path.isascii()
            or not os.path.isabs(path)
            or os.path.normpath(path) != path
            or path == os.path.sep
        ):
            raise ValueError(
                'map_store_path must be a normalized absolute path'
            )
        if type(self.owner_uid) is not int or self.owner_uid < 0:
            raise ValueError('owner_uid must be a non-negative integer')

    def __repr__(self) -> str:
        """Do not put the protected source path in logs."""
        return 'ActiveMapResolverConfig(<redacted>)'


@dataclass(frozen=True)
class _FileState:
    """Security-relevant state of one open regular file."""

    device: int
    inode: int
    mode: int
    owner_uid: int
    owner_gid: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _DirectoryState:
    """Security-relevant state of one open directory."""

    device: int
    inode: int
    mode: int
    owner_uid: int
    owner_gid: int
    links: int
    modified_ns: int
    changed_ns: int


@dataclass
class _OpenedFile:
    """One still-open file and its anchored directory entry."""

    descriptor: int
    parent_descriptor: int
    name: str
    state: _FileState
    value: bytes


@dataclass(frozen=True)
class _ParsedMap:
    """Validated projection derived only from snapped bytes."""

    map_id: str
    map_revision: str
    manifest_revision: str
    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    negate: int
    occupied_thresh: float
    free_thresh: float
    pixels: bytes = field(repr=False)


@dataclass(frozen=True)
class _EvidenceBaseline:
    """Private issuance baseline kept outside the evidence instance."""

    public_values: tuple[Any, ...]
    manifest_bytes: bytes
    map_yaml_bytes: bytes
    map_image_bytes: bytes
    user_map_bytes: bytes


_EVIDENCE_BASELINES: weakref.WeakKeyDictionary[Any, _EvidenceBaseline] = (
    weakref.WeakKeyDictionary()
)
_EVIDENCE_BASELINES_LOCK = RLock()


def _evidence_public_values(evidence: 'ActiveMapEvidence') -> tuple[Any, ...]:
    return (
        evidence.schema_version,
        evidence.map_id,
        evidence.map_revision,
        evidence.frame_id,
        evidence.active_manifest_revision,
        evidence.manifest_sha256,
        evidence.map_yaml_sha256,
        evidence.map_image_sha256,
        evidence.user_map_sha256,
        evidence.evidence_digest,
        evidence.width,
        evidence.height,
        evidence.resolution,
        evidence.origin_x,
        evidence.origin_y,
        evidence.origin_yaw,
    )


@dataclass(frozen=True, eq=False, repr=False)
class ActiveMapEvidence:
    """Frozen, redacted proof of one coherent active map snapshot."""

    map_id: str = field(repr=False)
    map_revision: str = field(repr=False)
    frame_id: str = field(repr=False)
    active_manifest_revision: str = field(repr=False)
    manifest_sha256: str = field(repr=False)
    map_yaml_sha256: str = field(repr=False)
    map_image_sha256: str = field(repr=False)
    user_map_sha256: str = field(repr=False)
    evidence_digest: str = field(repr=False)
    width: int = field(repr=False)
    height: int = field(repr=False)
    resolution: float = field(repr=False)
    origin_x: float = field(repr=False)
    origin_y: float = field(repr=False)
    origin_yaw: float = field(repr=False)
    _manifest_bytes: bytes = field(repr=False, compare=False, hash=False)
    _map_yaml_bytes: bytes = field(repr=False, compare=False, hash=False)
    _map_image_bytes: bytes = field(repr=False, compare=False, hash=False)
    _user_map_bytes: bytes = field(repr=False, compare=False, hash=False)
    _construction_token: InitVar[object] = None
    schema_version: int = field(
        default=ACTIVE_MAP_EVIDENCE_SCHEMA_VERSION,
        init=False,
        repr=False,
    )

    def __post_init__(self, _construction_token: object) -> None:
        """Validate resolver provenance and register an immutable baseline."""
        if _construction_token is not _EVIDENCE_CONSTRUCTION_TOKEN:
            raise TypeError('active map evidence must come from its resolver')
        if not _valid_evidence_current_values(self):
            raise ValueError('active map evidence is invalid')
        baseline = _EvidenceBaseline(
            public_values=_evidence_public_values(self),
            manifest_bytes=self._manifest_bytes,
            map_yaml_bytes=self._map_yaml_bytes,
            map_image_bytes=self._map_image_bytes,
            user_map_bytes=self._user_map_bytes,
        )
        with _EVIDENCE_BASELINES_LOCK:
            _EVIDENCE_BASELINES[self] = baseline

    def __repr__(self) -> str:
        """Keep paths, map contents, identifiers, and geometry out of logs."""
        return 'ActiveMapEvidence(<redacted>)'

    @property
    def manifest_revision(self) -> str:
        """Return the active manifest revision name."""
        return self.active_manifest_revision

    def canonical_copy(self) -> 'ActiveMapEvidence':
        """Detect mutation and return a detached, resolver-issued copy."""
        baseline = None
        invalid = False
        current_public: tuple[Any, ...] = ()
        current_private: tuple[Any, ...] = ()
        try:
            with _EVIDENCE_BASELINES_LOCK:
                baseline = _EVIDENCE_BASELINES.get(self)
            current_public = _evidence_public_values(self)
            current_private = (
                self._manifest_bytes,
                self._map_yaml_bytes,
                self._map_image_bytes,
                self._user_map_bytes,
            )
        except (
            AttributeError,
            OverflowError,
            RecursionError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            invalid = True
        if (
            invalid
            or type(baseline) is not _EvidenceBaseline
            or current_public != baseline.public_values
            or current_private != (
                baseline.manifest_bytes,
                baseline.map_yaml_bytes,
                baseline.map_image_bytes,
                baseline.user_map_bytes,
            )
            or not _valid_evidence_current_values(self)
        ):
            raise ActiveMapEvidenceInvalidError(
                'active_map_evidence_invalid',
                'Active map evidence object is invalid',
            )
        parsed = None
        failure = None
        try:
            parsed = _parse_snapshots(
                baseline.manifest_bytes,
                baseline.map_yaml_bytes,
                baseline.map_image_bytes,
                baseline.user_map_bytes,
            )
        except _MapFailure as error:
            failure = error.kind
        except (
            MemoryError,
            OverflowError,
            RecursionError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            yaml.YAMLError,
        ):
            failure = 'invalid'
        if failure is not None or parsed is None:
            raise ActiveMapEvidenceInvalidError(
                'active_map_evidence_invalid',
                'Active map evidence object is invalid',
            )
        return _build_evidence(
            parsed,
            baseline.manifest_bytes,
            baseline.map_yaml_bytes,
            baseline.map_image_bytes,
            baseline.user_map_bytes,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ActiveMapStaticNavigationProjection:
    """
    Bind active-map evidence to same-snapshot static clearance.

    This bundle contains no live Nav2 costmap and grants no authority to
    start or cancel motion.  Unknown occupancy and the area outside the saved
    map are treated as obstacles by the fixed projection algorithm.
    """

    active_map_evidence: ActiveMapEvidence = field(repr=False)
    static_clearance_grid: StaticClearanceGrid = field(repr=False)
    projection_digest: str = field(repr=False)
    _construction_token: InitVar[object] = None
    schema_version: int = field(
        default=ACTIVE_MAP_STATIC_PROJECTION_SCHEMA_VERSION,
        init=False,
        repr=False,
    )
    scope: str = field(
        default=_STATIC_PROJECTION_SCOPE,
        init=False,
        repr=False,
    )

    def __post_init__(self, _construction_token: object) -> None:
        """Accept only a complete bundle issued inside this resolver."""
        if _construction_token is not _PROJECTION_CONSTRUCTION_TOKEN:
            raise TypeError(
                'active map projection must come from its resolver'
            )
        if not _valid_static_projection_current_values(self):
            raise ValueError('active map projection is invalid')

    def __repr__(self) -> str:
        """Hide map identity, geometry, occupancy, and clearance content."""
        return 'ActiveMapStaticNavigationProjection(<redacted>)'

    def canonical_copy(self) -> 'ActiveMapStaticNavigationProjection':
        """Detect mutation and deterministically rebuild a detached bundle."""
        rebuilt = None
        invalid = False
        try:
            if not _valid_static_projection_current_values(self):
                invalid = True
            else:
                evidence = self.active_map_evidence.canonical_copy()
                current_clearance = _canonical_static_clearance(
                    self.static_clearance_grid
                )
                parsed = _parse_snapshots(
                    evidence._manifest_bytes,
                    evidence._map_yaml_bytes,
                    evidence._map_image_bytes,
                    evidence._user_map_bytes,
                )
                clearance = _build_static_clearance_grid(parsed)
                if clearance.digest != current_clearance.digest:
                    invalid = True
                else:
                    rebuilt = _build_static_projection(evidence, clearance)
        except (
            ActiveMapError,
            MemoryError,
            NavigationSafetyInputError,
            _MapFailure,
            OSError,
            OverflowError,
            RecursionError,
            RuntimeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            cv2.error,
            yaml.YAMLError,
        ):
            invalid = True
        if invalid or rebuilt is None:
            raise ActiveMapProjectionInvalidError(
                'active_map_projection_invalid',
                'Active map projection object is invalid',
            )
        return rebuilt


def _canonical_static_clearance(
    value: StaticClearanceGrid,
) -> StaticClearanceGrid:
    """Rebuild one safety DTO so object-level mutation cannot hide."""
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


def _valid_static_projection_current_values(
    projection: ActiveMapStaticNavigationProjection,
) -> bool:
    try:
        if (
            type(projection.schema_version) is not int
            or projection.schema_version
            != ACTIVE_MAP_STATIC_PROJECTION_SCHEMA_VERSION
            or type(projection.scope) is not str
            or projection.scope != _STATIC_PROJECTION_SCOPE
            or type(projection.active_map_evidence) is not ActiveMapEvidence
            or type(projection.static_clearance_grid)
            is not StaticClearanceGrid
            or type(projection.projection_digest) is not str
            or _SHA256.fullmatch(projection.projection_digest) is None
            or not _valid_evidence_current_values(
                projection.active_map_evidence
            )
        ):
            return False
        clearance = _canonical_static_clearance(
            projection.static_clearance_grid
        )
        return projection.projection_digest == _static_projection_digest(
            projection.active_map_evidence.evidence_digest,
            clearance.digest,
        )
    except (
        AttributeError,
        NavigationSafetyInputError,
        OverflowError,
        RecursionError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return False


def _valid_evidence_current_values(evidence: ActiveMapEvidence) -> bool:
    try:
        if (
            type(evidence.schema_version) is not int
            or evidence.schema_version != ACTIVE_MAP_EVIDENCE_SCHEMA_VERSION
            or type(evidence.map_id) is not str
            or _MAP_ID.fullmatch(evidence.map_id) is None
            or type(evidence.map_revision) is not str
            or _MAP_REVISION.fullmatch(evidence.map_revision) is None
            or type(evidence.frame_id) is not str
            or evidence.frame_id != 'map'
            or type(evidence.active_manifest_revision) is not str
            or _ACTIVE_REVISION.fullmatch(
                evidence.active_manifest_revision
            ) is None
            or type(evidence.width) is not int
            or not 1 <= evidence.width <= MAX_MAP_DIMENSION
            or type(evidence.height) is not int
            or not 1 <= evidence.height <= MAX_MAP_DIMENSION
        ):
            return False
        for value in (
            evidence.manifest_sha256,
            evidence.map_yaml_sha256,
            evidence.map_image_sha256,
            evidence.user_map_sha256,
            evidence.evidence_digest,
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                return False
        if (
            type(evidence.resolution) is not float
            or type(evidence.origin_x) is not float
            or type(evidence.origin_y) is not float
            or type(evidence.origin_yaw) is not float
            or not math.isfinite(evidence.resolution)
            or not math.isfinite(evidence.origin_x)
            or not math.isfinite(evidence.origin_y)
            or not math.isfinite(evidence.origin_yaw)
            or evidence.resolution <= 0.0
        ):
            return False
        for value in (
            evidence._manifest_bytes,
            evidence._map_yaml_bytes,
            evidence._map_image_bytes,
            evidence._user_map_bytes,
        ):
            if type(value) is not bytes:
                return False
    except (
        AttributeError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return False
    return True


def _directory_state(value: os.stat_result) -> _DirectoryState:
    return _DirectoryState(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        owner_uid=value.st_uid,
        owner_gid=value.st_gid,
        links=value.st_nlink,
        modified_ns=value.st_mtime_ns,
        changed_ns=value.st_ctime_ns,
    )


def _file_state(value: os.stat_result) -> _FileState:
    return _FileState(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        owner_uid=value.st_uid,
        owner_gid=value.st_gid,
        links=value.st_nlink,
        size=value.st_size,
        modified_ns=value.st_mtime_ns,
        changed_ns=value.st_ctime_ns,
    )


def _validate_directory(value: os.stat_result, owner_uid: int) -> None:
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != owner_uid
        or value.st_mode & 0o022
    ):
        _failure('invalid')


def _validate_regular_file(
    value: os.stat_result,
    owner_uid: int,
    maximum_bytes: int,
) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != owner_uid
        or value.st_nlink != 1
        or value.st_mode & 0o022
        or value.st_size < 1
        or value.st_size > maximum_bytes
    ):
        _failure('invalid')


def _open_root_directory(
    path: str,
    owner_uid: int,
) -> tuple[int, _DirectoryState]:
    flags = os.O_RDONLY
    flags |= getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_DIRECTORY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = -1
    current = -1
    try:
        current = os.open(os.path.sep, flags)
        for component in PurePosixPath(path).parts[1:]:
            descriptor = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = descriptor
            descriptor = -1
        value = os.fstat(current)
        _validate_directory(value, owner_uid)
        return current, _directory_state(value)
    except FileNotFoundError:
        if descriptor >= 0:
            os.close(descriptor)
        if current >= 0:
            os.close(current)
        _failure('unavailable')
    except _MapFailure:
        if descriptor >= 0:
            os.close(descriptor)
        if current >= 0:
            os.close(current)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        if current >= 0:
            os.close(current)
        _failure('invalid')
    raise AssertionError('unreachable')


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    owner_uid: int,
) -> tuple[int, _DirectoryState]:
    flags = os.O_RDONLY
    flags |= getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_DIRECTORY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        _failure('unavailable')
    except OSError:
        _failure('invalid')
    try:
        value = os.fstat(descriptor)
        _validate_directory(value, owner_uid)
        return descriptor, _directory_state(value)
    except _MapFailure:
        os.close(descriptor)
        raise
    except OSError:
        os.close(descriptor)
        _failure('invalid')
    raise AssertionError('unreachable')


def _open_file_at(
    parent_descriptor: int,
    name: str,
    owner_uid: int,
    maximum_bytes: int,
) -> tuple[int, _FileState]:
    flags = os.O_RDONLY
    flags |= getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        _failure('unavailable')
    except OSError:
        _failure('invalid')
    try:
        value = os.fstat(descriptor)
        _validate_regular_file(value, owner_uid, maximum_bytes)
        return descriptor, _file_state(value)
    except _MapFailure:
        os.close(descriptor)
        raise
    except OSError:
        os.close(descriptor)
        _failure('invalid')
    raise AssertionError('unreachable')


def _read_open_file(
    descriptor: int,
    before: _FileState,
    owner_uid: int,
    maximum_bytes: int,
) -> tuple[bytes, _FileState]:
    chunks = []
    remaining = before.size
    try:
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                _failure('changed')
            chunks.append(chunk)
            remaining -= len(chunk)
        value = os.fstat(descriptor)
        after = _file_state(value)
    except _MapFailure:
        raise
    except OSError:
        _failure('changed')
    if after != before:
        _failure('changed')
    _validate_regular_file(value, owner_uid, maximum_bytes)
    return b''.join(chunks), after


def _entry_state(parent_descriptor: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        _failure('changed')
    except OSError:
        _failure('changed')
    raise AssertionError('unreachable')


def _revalidate_file(value: _OpenedFile, owner_uid: int) -> None:
    try:
        opened = os.fstat(value.descriptor)
    except OSError:
        _failure('changed')
    entry = _entry_state(value.parent_descriptor, value.name)
    if _file_state(opened) != value.state or _file_state(entry) != value.state:
        _failure('changed')
    _validate_regular_file(entry, owner_uid, value.state.size)


def _revalidate_directory(
    descriptor: int,
    parent_descriptor: int | None,
    name: str | None,
    expected: _DirectoryState,
    owner_uid: int,
) -> None:
    try:
        opened = os.fstat(descriptor)
    except OSError:
        _failure('changed')
    if _directory_state(opened) != expected:
        _failure('changed')
    _validate_directory(opened, owner_uid)
    if parent_descriptor is not None and name is not None:
        entry = _entry_state(parent_descriptor, name)
        if _directory_state(entry) != expected:
            _failure('changed')
        _validate_directory(entry, owner_uid)


def _strict_json(value: bytes) -> Any:
    invalid = False
    parsed: Any = None

    def reject_constant(_value: str) -> None:
        raise ValueError('constant')

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if type(key) is not str or key in result:
                raise ValueError('duplicate')
            result[key] = item
        return result

    try:
        text = value.decode('utf-8')
        parsed = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ):
        invalid = True
    if invalid:
        _failure('invalid')
    return parsed


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str or key in result:
            raise yaml.constructor.ConstructorError(
                'while constructing a mapping',
                node.start_mark,
                'duplicate or invalid key',
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _strict_yaml(value: bytes) -> Any:
    invalid = False
    parsed: Any = None
    try:
        text = value.decode('utf-8')
        parsed = yaml.load(text, Loader=_UniqueKeyLoader)
    except (
        RecursionError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        yaml.YAMLError,
    ):
        invalid = True
    if invalid:
        _failure('invalid')
    return parsed


def _validate_json_tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _failure('invalid')
    if value is None or type(value) in (bool, int, str):
        if type(value) is str:
            try:
                value.encode('utf-8')
            except UnicodeEncodeError:
                _failure('invalid')
        return
    if type(value) is float:
        if not math.isfinite(value):
            _failure('invalid')
        return
    if type(value) is list:
        for item in value:
            _validate_json_tree(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _failure('invalid')
            _validate_json_tree(key, depth + 1)
            _validate_json_tree(item, depth + 1)
        return
    _failure('invalid')


def _relative_parts(value: Any) -> tuple[str, ...]:
    if (
        type(value) is not str
        or not value
        or len(value) > 512
        or '\x00' in value
        or '\\' in value
        or not value.isascii()
    ):
        _failure('invalid')
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in ('', '.', '..') for part in path.parts
    ):
        _failure('invalid')
    if str(path) != value:
        _failure('invalid')
    return path.parts


def _parse_manifest(value: bytes) -> tuple[dict[str, Any], str]:
    manifest = _strict_json(value)
    if type(manifest) is not dict:
        _failure('invalid')
    _validate_json_tree(manifest)
    revision = manifest.get('revision')
    revision_match = (
        _ACTIVE_REVISION.fullmatch(revision)
        if type(revision) is str
        else None
    )
    if (
        type(manifest.get('format')) is not str
        or manifest.get('format') != MAP_STORE_FORMAT
        or revision_match is None
        or type(manifest.get('map_id')) is not str
        or _MAP_ID.fullmatch(manifest['map_id']) is None
        or type(manifest.get('map_revision')) is not str
        or _MAP_REVISION.fullmatch(manifest['map_revision']) is None
    ):
        _failure('invalid')
    expected = {
        'map_yaml': ('versions', revision, 'map.yaml'),
        'map_image': ('versions', revision, 'map.pgm'),
        'user_map': ('versions', revision, 'user-map.geojson'),
    }
    for field_name, expected_parts in expected.items():
        if _relative_parts(manifest.get(field_name)) != expected_parts:
            _failure('invalid')
    return manifest, revision_match.group(1)


def _parse_map_yaml(value: bytes) -> dict[str, Any]:
    metadata = _strict_yaml(value)
    expected_keys = {
        'image',
        'mode',
        'resolution',
        'origin',
        'negate',
        'occupied_thresh',
        'free_thresh',
    }
    if type(metadata) is not dict or set(metadata) != expected_keys:
        _failure('invalid')
    if (
        type(metadata['image']) is not str
        or _relative_parts(metadata['image']) != ('map.pgm',)
        or type(metadata['mode']) is not str
        or metadata['mode'] != 'trinary'
        or type(metadata['origin']) is not list
        or len(metadata['origin']) != 3
        or type(metadata['negate']) is not int
        or metadata['negate'] not in (0, 1)
    ):
        _failure('invalid')
    resolution = _finite_number(metadata['resolution'], 1e-9, 1_000.0)
    origin = [
        _finite_number(item, -1_000_000_000.0, 1_000_000_000.0)
        for item in metadata['origin']
    ]
    occupied = _finite_number(metadata['occupied_thresh'], 0.0, 1.0)
    free = _finite_number(metadata['free_thresh'], 0.0, 1.0)
    if not free < occupied:
        _failure('invalid')
    return {
        'image': 'map.pgm',
        'mode': 'trinary',
        'resolution': resolution,
        'origin': origin,
        'negate': metadata['negate'],
        'occupied_thresh': occupied,
        'free_thresh': free,
    }


def _parse_pgm(value: bytes) -> tuple[int, int, bytes]:
    if not value.startswith(_PGM_PREFIX):
        _failure('invalid')
    dimension_start = len(_PGM_PREFIX)
    dimension_end = value.find(b'\n', dimension_start)
    if dimension_end < 0 or dimension_end - dimension_start > 13:
        _failure('invalid')
    dimensions = value[dimension_start:dimension_end]
    if _PGM_DIMENSIONS.fullmatch(dimensions) is None:
        _failure('invalid')
    width_text, height_text = dimensions.split(b' ', 1)
    width = int(width_text)
    height = int(height_text)
    _exact_integer(width, 1, MAX_MAP_DIMENSION)
    _exact_integer(height, 1, MAX_MAP_DIMENSION)
    pixels_count = width * height
    if pixels_count > MAX_MAP_PIXELS:
        _failure('invalid')
    maximum_start = dimension_end + 1
    maximum_end = maximum_start + len(_PGM_MAXIMUM)
    if value[maximum_start:maximum_end] != _PGM_MAXIMUM:
        _failure('invalid')
    pixels = value[maximum_end:]
    if len(pixels) != pixels_count:
        _failure('invalid')
    return width, height, pixels


def _coordinate(value: Any) -> None:
    if type(value) is not list or len(value) < 2:
        _failure('invalid')
    for item in value:
        _finite_number(item, -1_000_000_000.0, 1_000_000_000.0)


def _line(value: Any, minimum_points: int) -> None:
    if type(value) is not list or len(value) < minimum_points:
        _failure('invalid')
    for point in value:
        _coordinate(point)


def _ring(value: Any) -> None:
    _line(value, 4)
    if value[0] != value[-1]:
        _failure('invalid')


def _polygon(value: Any) -> None:
    if type(value) is not list or not value:
        _failure('invalid')
    for ring in value:
        _ring(ring)


def _validate_geometry(value: Any) -> None:
    if type(value) is not dict or type(value.get('type')) is not str:
        _failure('invalid')
    geometry_type = value['type']
    coordinates = value.get('coordinates')
    if geometry_type == 'Point':
        _coordinate(coordinates)
    elif geometry_type == 'MultiPoint':
        _line(coordinates, 1)
    elif geometry_type == 'LineString':
        _line(coordinates, 2)
    elif geometry_type == 'MultiLineString':
        if type(coordinates) is not list or not coordinates:
            _failure('invalid')
        for line in coordinates:
            _line(line, 2)
    elif geometry_type == 'Polygon':
        _polygon(coordinates)
    elif geometry_type == 'MultiPolygon':
        if type(coordinates) is not list or not coordinates:
            _failure('invalid')
        for polygon in coordinates:
            _polygon(polygon)
    else:
        _failure('invalid')


def _parse_user_map(
    value: bytes,
    metadata: dict[str, Any],
    map_id: str,
    map_revision: str,
    width: int,
    height: int,
) -> None:
    user_map = _strict_json(value)
    if type(user_map) is not dict:
        _failure('invalid')
    _validate_json_tree(user_map)
    if (
        type(user_map.get('type')) is not str
        or user_map.get('type') != 'FeatureCollection'
        or type(user_map.get('format')) is not str
        or user_map.get('format') != USER_MAP_FORMAT
        or type(user_map.get('map_id')) is not str
        or user_map.get('map_id') != map_id
        or type(user_map.get('map_revision')) is not str
        or user_map.get('map_revision') != map_revision
        or type(user_map.get('frame_id')) is not str
        or user_map.get('frame_id') != 'map'
        or type(user_map.get('features')) is not list
        or not user_map['features']
        or type(user_map.get('source')) is not dict
    ):
        _failure('invalid')
    source = user_map['source']
    if (
        type(source.get('type')) is not str
        or source.get('type') != 'slam_occupancy_grid'
        or type(source.get('map_yaml')) is not str
        or source.get('map_yaml') != 'map.yaml'
        or type(source.get('map_image')) is not str
        or source.get('map_image') != 'map.pgm'
        or type(source.get('mode')) is not str
        or source.get('mode') != 'trinary'
        or type(source.get('width')) is not int
        or source.get('width') != width
        or type(source.get('height')) is not int
        or source.get('height') != height
        or _finite_number(source.get('resolution'), 1e-9, 1_000.0)
        != metadata['resolution']
        or _finite_number(source.get('occupied_thresh'), 0.0, 1.0)
        != metadata['occupied_thresh']
        or _finite_number(source.get('free_thresh'), 0.0, 1.0)
        != metadata['free_thresh']
    ):
        _failure('invalid')
    for feature in user_map['features']:
        if (
            type(feature) is not dict
            or type(feature.get('type')) is not str
            or feature.get('type') != 'Feature'
            or type(feature.get('id')) is not str
            or not feature['id']
            or len(feature['id']) > 128
            or type(feature.get('properties')) is not dict
        ):
            _failure('invalid')
        _validate_geometry(feature.get('geometry'))


def _identities(
    width: int,
    height: int,
    pixels: bytes,
    metadata: dict[str, Any],
) -> tuple[str, str]:
    stable_metadata = {
        'shape': [height, width],
        'resolution': metadata['resolution'],
        'origin': [float(item) for item in metadata['origin']],
    }
    stable = hashlib.sha256()
    stable.update(_canonical_json(stable_metadata))
    stable.update(pixels)
    revision_metadata = {
        **stable_metadata,
        'negate': bool(metadata['negate']),
        'occupied_thresh': metadata['occupied_thresh'],
        'free_thresh': metadata['free_thresh'],
        'mode': metadata['mode'],
    }
    revision = hashlib.sha256()
    revision.update(_canonical_json(revision_metadata))
    revision.update(pixels)
    return (
        f'map-{stable.hexdigest()[:12]}',
        f'rev-{revision.hexdigest()[:12]}',
    )


def _parse_snapshots(
    manifest_bytes: bytes,
    map_yaml_bytes: bytes,
    map_image_bytes: bytes,
    user_map_bytes: bytes,
) -> _ParsedMap:
    manifest, revision_digest = _parse_manifest(manifest_bytes)
    metadata = _parse_map_yaml(map_yaml_bytes)
    width, height, pixels = _parse_pgm(map_image_bytes)
    map_id, map_revision = _identities(
        width,
        height,
        pixels,
        metadata,
    )
    if (
        manifest['map_id'] != map_id
        or manifest['map_revision'] != map_revision
        or revision_digest != _sha256(
            map_image_bytes + map_revision.encode('ascii')
        )[:10]
    ):
        _failure('invalid')
    _parse_user_map(
        user_map_bytes,
        metadata,
        map_id,
        map_revision,
        width,
        height,
    )
    return _ParsedMap(
        map_id=map_id,
        map_revision=map_revision,
        manifest_revision=manifest['revision'],
        width=width,
        height=height,
        resolution=metadata['resolution'],
        origin_x=metadata['origin'][0],
        origin_y=metadata['origin'][1],
        origin_yaw=metadata['origin'][2],
        negate=metadata['negate'],
        occupied_thresh=metadata['occupied_thresh'],
        free_thresh=metadata['free_thresh'],
        pixels=pixels,
    )


def _evidence_digest_payload(
    parsed: _ParsedMap,
    manifest_sha256: str,
    map_yaml_sha256: str,
    map_image_sha256: str,
    user_map_sha256: str,
) -> dict[str, Any]:
    return {
        'schema_version': ACTIVE_MAP_EVIDENCE_SCHEMA_VERSION,
        'map_id': parsed.map_id,
        'map_revision': parsed.map_revision,
        'frame_id': 'map',
        'active_manifest_revision': parsed.manifest_revision,
        'manifest_sha256': manifest_sha256,
        'map_yaml_sha256': map_yaml_sha256,
        'map_image_sha256': map_image_sha256,
        'user_map_sha256': user_map_sha256,
        'width': parsed.width,
        'height': parsed.height,
        'resolution': parsed.resolution,
        'origin': [
            parsed.origin_x,
            parsed.origin_y,
            parsed.origin_yaw,
        ],
    }


def _build_evidence(
    parsed: _ParsedMap,
    manifest_bytes: bytes,
    map_yaml_bytes: bytes,
    map_image_bytes: bytes,
    user_map_bytes: bytes,
) -> ActiveMapEvidence:
    manifest_digest = _sha256(manifest_bytes)
    yaml_digest = _sha256(map_yaml_bytes)
    image_digest = _sha256(map_image_bytes)
    user_digest = _sha256(user_map_bytes)
    evidence_digest = _sha256(_canonical_json(_evidence_digest_payload(
        parsed,
        manifest_digest,
        yaml_digest,
        image_digest,
        user_digest,
    )))
    return ActiveMapEvidence(
        map_id=parsed.map_id,
        map_revision=parsed.map_revision,
        frame_id='map',
        active_manifest_revision=parsed.manifest_revision,
        manifest_sha256=manifest_digest,
        map_yaml_sha256=yaml_digest,
        map_image_sha256=image_digest,
        user_map_sha256=user_digest,
        evidence_digest=evidence_digest,
        width=parsed.width,
        height=parsed.height,
        resolution=parsed.resolution,
        origin_x=parsed.origin_x,
        origin_y=parsed.origin_y,
        origin_yaw=parsed.origin_yaw,
        _manifest_bytes=manifest_bytes,
        _map_yaml_bytes=map_yaml_bytes,
        _map_image_bytes=map_image_bytes,
        _user_map_bytes=user_map_bytes,
        _construction_token=_EVIDENCE_CONSTRUCTION_TOKEN,
    )


def _build_static_clearance_grid(parsed: _ParsedMap) -> StaticClearanceGrid:
    """Project snapped PGM bytes with ROS trinary occupancy semantics."""
    if (
        parsed.origin_yaw != 0.0
        or parsed.width > MAX_STATIC_PROJECTION_DIMENSION
        or parsed.height > MAX_STATIC_PROJECTION_DIMENSION
        or parsed.width * parsed.height > MAX_STATIC_PROJECTION_CELLS
    ):
        _failure('invalid')
    failed = False
    clearance_values: tuple[float, ...] = ()
    try:
        image = np.frombuffer(parsed.pixels, dtype=np.uint8).reshape(
            parsed.height,
            parsed.width,
        )
        pixel_values = image.astype(np.float64)
        if parsed.negate == 0:
            occupancy = (255.0 - pixel_values) / 255.0
        else:
            occupancy = pixel_values / 255.0
        occupied = occupancy > parsed.occupied_thresh
        free = occupancy < parsed.free_thresh
        unknown = np.logical_not(np.logical_or(occupied, free))
        obstacles = np.logical_or(occupied, unknown)

        # PGM row zero is the top of the image.  ROS OccupancyGrid row zero is
        # the bottom of the map, so flip before producing row-major grid data.
        ros_free = np.ascontiguousarray(
            np.flipud(np.logical_not(obstacles)).astype(np.uint8)
        )
        padded = np.zeros(
            (parsed.height + 2, parsed.width + 2),
            dtype=np.uint8,
        )
        padded[1:-1, 1:-1] = ros_free
        padded_clearance = cv2.distanceTransform(
            padded,
            cv2.DIST_L2,
            cv2.DIST_MASK_PRECISE,
        )
        if (
            type(padded_clearance) is not np.ndarray
            or padded_clearance.shape != padded.shape
            or padded_clearance.dtype != np.float32
        ):
            failed = True
        else:
            clearance_pixels = padded_clearance[1:-1, 1:-1]
            if (
                not bool(np.all(np.isfinite(clearance_pixels)))
                or bool(np.any(clearance_pixels < 0.0))
                or bool(np.any(clearance_pixels[ros_free == 0] != 0.0))
            ):
                failed = True
            else:
                clearance_metres = (
                    clearance_pixels.astype(np.float64)
                    * parsed.resolution
                )
                if (
                    not bool(np.all(np.isfinite(clearance_metres)))
                    or bool(np.any(clearance_metres < 0.0))
                ):
                    failed = True
                else:
                    clearance_values = tuple(
                        float(item)
                        for item in clearance_metres.ravel(order='C')
                    )
    except (
        MemoryError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
        cv2.error,
    ):
        failed = True
    if failed or len(clearance_values) != parsed.width * parsed.height:
        _failure('invalid')
    try:
        return StaticClearanceGrid(
            frame_id='map',
            width=parsed.width,
            height=parsed.height,
            resolution_m=parsed.resolution,
            origin_x_m=parsed.origin_x,
            origin_y_m=parsed.origin_y,
            origin_yaw_rad=parsed.origin_yaw,
            clearances_m=clearance_values,
        )
    except NavigationSafetyInputError:
        _failure('invalid')
    raise AssertionError('unreachable')


def _build_static_projection(
    evidence: ActiveMapEvidence,
    clearance: StaticClearanceGrid,
) -> ActiveMapStaticNavigationProjection:
    """Create the resolver-issued binding for one captured snapshot."""
    return ActiveMapStaticNavigationProjection(
        active_map_evidence=evidence,
        static_clearance_grid=clearance,
        projection_digest=_static_projection_digest(
            evidence.evidence_digest,
            clearance.digest,
        ),
        _construction_token=_PROJECTION_CONSTRUCTION_TOKEN,
    )


class ActiveMapEvidenceResolver:
    """Resolve one current active map from a fixed protected store."""

    __slots__ = ('_map_store_path', '_owner_uid')

    def __init__(self, config: ActiveMapResolverConfig) -> None:
        """Snapshot fixed configuration without touching the filesystem."""
        if type(config) is not ActiveMapResolverConfig:
            raise TypeError('config must be ActiveMapResolverConfig')
        self._map_store_path = config.map_store_path
        self._owner_uid = config.owner_uid

    def resolve(self) -> ActiveMapEvidence:
        """Return evidence derived from one coherent, bounded file snapshot."""
        result = self._resolve_public(include_static_projection=False)
        if type(result) is not ActiveMapEvidence:
            raise ActiveMapValidationError(
                'active_map_invalid',
                'Active map evidence is invalid',
            )
        return result

    def resolve_static_navigation_projection(
        self,
    ) -> ActiveMapStaticNavigationProjection:
        """
        Return evidence and static clearance from one file-open cycle.

        The projection implements ROS map_server trinary occupancy for the
        snapped PGM/YAML bytes.  It deliberately does not read a live Nav2
        costmap and does not derive restricted zones from User Map features.
        """
        result = self._resolve_public(include_static_projection=True)
        if type(result) is not ActiveMapStaticNavigationProjection:
            raise ActiveMapValidationError(
                'active_map_invalid',
                'Active map evidence is invalid',
            )
        return result

    def _resolve_public(
        self,
        *,
        include_static_projection: bool,
    ) -> ActiveMapEvidence | ActiveMapStaticNavigationProjection:
        """Convert private capture/projection failures at one boundary."""
        result = None
        failure = None
        try:
            result = self._resolve(
                include_static_projection=include_static_projection,
            )
        except _MapFailure as error:
            failure = error.kind
        except (
            MemoryError,
            OSError,
            OverflowError,
            RecursionError,
            RuntimeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            cv2.error,
            yaml.YAMLError,
        ):
            failure = 'invalid'
        if failure is not None or result is None:
            raise _public_failure(failure or 'invalid')
        return result

    def _resolve(
        self,
        *,
        include_static_projection: bool,
    ) -> ActiveMapEvidence | ActiveMapStaticNavigationProjection:
        store_descriptor = -1
        versions_descriptor = -1
        revision_descriptor = -1
        opened_files: list[_OpenedFile] = []
        try:
            store_descriptor, store_state = _open_root_directory(
                self._map_store_path,
                self._owner_uid,
            )
            manifest_descriptor, manifest_before = _open_file_at(
                store_descriptor,
                ACTIVE_MANIFEST_NAME,
                self._owner_uid,
                MAX_ACTIVE_MANIFEST_BYTES,
            )
            try:
                manifest_bytes, manifest_state = _read_open_file(
                    manifest_descriptor,
                    manifest_before,
                    self._owner_uid,
                    MAX_ACTIVE_MANIFEST_BYTES,
                )
            except BaseException:
                os.close(manifest_descriptor)
                raise
            manifest_file = _OpenedFile(
                descriptor=manifest_descriptor,
                parent_descriptor=store_descriptor,
                name=ACTIVE_MANIFEST_NAME,
                state=manifest_state,
                value=manifest_bytes,
            )
            opened_files.append(manifest_file)
            manifest, _revision_digest = _parse_manifest(manifest_bytes)

            versions_descriptor, versions_state = _open_directory_at(
                store_descriptor,
                'versions',
                self._owner_uid,
            )
            revision_descriptor, revision_state = _open_directory_at(
                versions_descriptor,
                manifest['revision'],
                self._owner_uid,
            )
            specifications = (
                ('map.yaml', MAX_MAP_YAML_BYTES),
                ('map.pgm', MAX_MAP_IMAGE_BYTES),
                ('user-map.geojson', MAX_USER_MAP_BYTES),
            )
            for name, maximum_bytes in specifications:
                descriptor, before = _open_file_at(
                    revision_descriptor,
                    name,
                    self._owner_uid,
                    maximum_bytes,
                )
                try:
                    value, after = _read_open_file(
                        descriptor,
                        before,
                        self._owner_uid,
                        maximum_bytes,
                    )
                except BaseException:
                    os.close(descriptor)
                    raise
                opened_files.append(_OpenedFile(
                    descriptor=descriptor,
                    parent_descriptor=revision_descriptor,
                    name=name,
                    state=after,
                    value=value,
                ))
            parsed = _parse_snapshots(
                opened_files[0].value,
                opened_files[1].value,
                opened_files[2].value,
                opened_files[3].value,
            )
            evidence = _build_evidence(
                parsed,
                opened_files[0].value,
                opened_files[1].value,
                opened_files[2].value,
                opened_files[3].value,
            )
            result: (
                ActiveMapEvidence | ActiveMapStaticNavigationProjection
            ) = evidence
            if include_static_projection:
                clearance = _build_static_clearance_grid(parsed)
                result = _build_static_projection(evidence, clearance)
            for opened_file in opened_files:
                _revalidate_file(opened_file, self._owner_uid)
            _revalidate_directory(
                revision_descriptor,
                versions_descriptor,
                manifest['revision'],
                revision_state,
                self._owner_uid,
            )
            _revalidate_directory(
                versions_descriptor,
                store_descriptor,
                'versions',
                versions_state,
                self._owner_uid,
            )
            _revalidate_directory(
                store_descriptor,
                None,
                None,
                store_state,
                self._owner_uid,
            )
            root_entry = os.stat(
                self._map_store_path,
                follow_symlinks=False,
            )
            if _directory_state(root_entry) != store_state:
                _failure('changed')
            _validate_directory(root_entry, self._owner_uid)
            return result
        finally:
            for opened_file in opened_files:
                try:
                    os.close(opened_file.descriptor)
                except OSError:
                    pass
            for descriptor in (
                revision_descriptor,
                versions_descriptor,
                store_descriptor,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass


__all__ = (
    'ACTIVE_MAP_EVIDENCE_SCHEMA_VERSION',
    'ACTIVE_MAP_STATIC_PROJECTION_SCHEMA_VERSION',
    'ActiveMapChangedError',
    'ActiveMapError',
    'ActiveMapEvidence',
    'ActiveMapEvidenceInvalidError',
    'ActiveMapEvidenceResolver',
    'ActiveMapProjectionInvalidError',
    'ActiveMapResolverConfig',
    'ActiveMapStaticNavigationProjection',
    'ActiveMapUnavailableError',
    'ActiveMapValidationError',
    'MAX_STATIC_PROJECTION_CELLS',
    'MAX_STATIC_PROJECTION_DIMENSION',
)
