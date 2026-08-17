"""
Strict same-host robot-state evidence for proposal-only room monitoring.

The public HTTP request's ``robot_state`` is never an authority source.  A
trusted collector may instead expose one bounded, nonce-bound snapshot over a
fixed Unix-domain socket.  This module validates that transport and preserves
unknown values as ``None`` until a tool-specific completeness check succeeds.
It does not publish ROS commands or grant physical execution authority.
"""

import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time
import unicodedata
import uuid
import weakref
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

from malbut_agent_server.homecam_semantic import (
    AuthenticatedHomecamSemanticResolver,
    HomecamSemanticConfig,
    VerifiedSemanticSnapshotEvidence,
)
from malbut_agent_server.monitor_room_target import (
    TargetBinding,
    gazebo_simulation_navigation_effects,
    resolve_monitor_room_target,
)
from malbut_agent_server.schemas import (
    RobotState,
    ValidationError,
    validate_user_id,
)


TRUSTED_ROBOT_STATE_SCHEMA_VERSION = 1
TRUSTED_ROBOT_STATE_SOURCE_KIND = 'trusted_ros2'
GAZEBO_SIMULATION_ADMISSION_PROFILE = (
    'gazebo_simulation_monitor_room_v1'
)
GAZEBO_SIMULATION_RUNTIME_MODE = 'gazebo'
GAZEBO_SIMULATION_ADMISSION_SCHEMA_VERSION = 1
MAX_ROBOT_STATE_FRAME_BYTES = 16 * 1024
MAX_ROBOT_STATE_LIFETIME_NS = 5_000_000_000
MAX_ROBOT_STATE_SEQUENCE = (1 << 64) - 1
DEFAULT_ROBOT_STATE_TIMEOUT_SECONDS = 1.0
MAX_ROBOT_STATE_TIMEOUT_SECONDS = 5.0
_LOWER_HEX_64 = re.compile(r'^[0-9a-f]{64}$')
_UNSIGNED_DECIMAL = re.compile(r'^(?:0|[1-9][0-9]{0,19})$')
_ERROR_CODE = re.compile(r'^robot_state_[a-z0-9_]{1,52}$')
_SAFE_IDENTIFIER = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'
)
_ENVELOPE_FIELDS = frozenset(
    {
        'schema_version',
        'nonce',
        'source',
        'binding',
        'assembled_at',
        'assembled_boottime_ns',
        'valid_until_boottime_ns',
        'state',
        'evidence',
    }
)
_SOURCE_FIELDS = frozenset(
    {
        'kind',
        'host_boot_id',
        'instance_id',
        'sequence',
        'physical_authority',
    }
)
_BINDING_FIELDS = frozenset(
    {'device_id', 'map_id', 'map_revision'}
)
_STATE_FIELDS = (
    'battery_percent',
    'navigation_available',
    'localization_ok',
    'emergency_stop',
    'camera_available',
    'privacy_mode',
    'docked',
    'forbidden_zones',
)
_MONITOR_ROOM_REQUIRED_FIELDS = (
    'battery_percent',
    'navigation_available',
    'localization_ok',
    'emergency_stop',
    'camera_available',
    'privacy_mode',
    'forbidden_zones',
)
_FIELD_EVIDENCE_FIELDS = frozenset(
    {'source', 'received_boottime_ns'}
)


class TrustedRobotStateError(ValidationError):
    """Fail-closed robot-state error with a stable, content-free code."""

    def __init__(
        self,
        code: str,
        _message: str = 'trusted robot state is unavailable',
    ) -> None:
        """Create an error without exposing paths, payloads, or peer data."""
        if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
            raise ValueError('trusted robot state error code is invalid')
        super().__init__('trusted robot state is unavailable')
        self.code = code


@dataclass(frozen=True)
class RobotStateFieldEvidence:
    """Receipt evidence for one nullable state field."""

    source: str
    received_boottime_ns: int

    def __post_init__(self) -> None:
        """Validate the immutable per-field receipt shape."""
        object.__setattr__(
            self,
            'source',
            _identifier(self.source, 'field_evidence.source'),
        )
        object.__setattr__(
            self,
            'received_boottime_ns',
            _exact_integer(
                self.received_boottime_ns,
                'field_evidence.received_boottime_ns',
                minimum=1,
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the exact canonical evidence object."""
        return {
            'source': self.source,
            'received_boottime_ns': str(
                self.received_boottime_ns
            ),
        }


@dataclass(frozen=True)
class TrustedRobotStateEvidence:
    """One validated tri-state snapshot from the trusted local collector."""

    evidence_digest: str
    device_id: str
    map_id: str
    map_revision: str
    host_boot_id: str
    instance_id: str
    sequence: int
    assembled_at: str
    assembled_boottime_ns: int
    valid_until_boottime_ns: int
    battery_percent: Optional[float]
    navigation_available: Optional[bool]
    localization_ok: Optional[bool]
    emergency_stop: Optional[bool]
    camera_available: Optional[bool]
    privacy_mode: Optional[bool]
    docked: Optional[bool]
    forbidden_zones: Optional[Tuple[str, ...]]
    field_evidence: Mapping[
        str,
        Optional[RobotStateFieldEvidence],
    ]

    def __post_init__(self) -> None:
        """Validate and freeze the complete nominal trusted type."""
        if (
            not isinstance(self.evidence_digest, str)
            or not _LOWER_HEX_64.fullmatch(self.evidence_digest)
        ):
            raise ValueError('evidence_digest is invalid')
        for name in ('device_id', 'map_id', 'map_revision'):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'host_boot_id',
            _canonical_uuid(self.host_boot_id, 'host_boot_id'),
        )
        object.__setattr__(
            self,
            'instance_id',
            _canonical_uuid(self.instance_id, 'instance_id'),
        )
        sequence = _exact_integer(
            self.sequence,
            'sequence',
            minimum=0,
        )
        assembled = _exact_integer(
            self.assembled_boottime_ns,
            'assembled_boottime_ns',
            minimum=1,
        )
        valid_until = _exact_integer(
            self.valid_until_boottime_ns,
            'valid_until_boottime_ns',
            minimum=1,
        )
        if (
            valid_until <= assembled
            or valid_until - assembled > MAX_ROBOT_STATE_LIFETIME_NS
        ):
            raise ValueError('state evidence validity interval is invalid')
        object.__setattr__(self, 'sequence', sequence)
        object.__setattr__(self, 'assembled_at', _audit_timestamp(
            self.assembled_at
        ))
        object.__setattr__(self, 'assembled_boottime_ns', assembled)
        object.__setattr__(self, 'valid_until_boottime_ns', valid_until)
        object.__setattr__(
            self,
            'battery_percent',
            _battery(self.battery_percent),
        )
        for name in (
            'navigation_available',
            'localization_ok',
            'emergency_stop',
            'camera_available',
            'privacy_mode',
            'docked',
        ):
            object.__setattr__(
                self,
                name,
                _nullable_bool(getattr(self, name), name),
            )
        zones = self.forbidden_zones
        normalized_zones = _zones(
            list(zones) if zones is not None else None
        )
        object.__setattr__(self, 'forbidden_zones', normalized_zones)
        if (
            not isinstance(self.field_evidence, Mapping)
            or set(self.field_evidence) != set(_STATE_FIELDS)
        ):
            raise ValueError('field_evidence is invalid')
        frozen_evidence = dict(self.field_evidence)
        for name, item in frozen_evidence.items():
            if item is not None and not isinstance(
                item,
                RobotStateFieldEvidence,
            ):
                raise ValueError('field_evidence is invalid')
            if item is not None:
                if (
                    item.received_boottime_ns > assembled
                    or assembled - item.received_boottime_ns
                    > MAX_ROBOT_STATE_LIFETIME_NS
                    or valid_until
                    > item.received_boottime_ns
                    + MAX_ROBOT_STATE_LIFETIME_NS
                ):
                    raise ValueError('field_evidence is stale')
            if getattr(self, name) is not None and item is None:
                raise ValueError('field_evidence is missing')
        object.__setattr__(
            self,
            'field_evidence',
            MappingProxyType(frozen_evidence),
        )

    def is_current(
        self,
        now_boottime_ns: Optional[int] = None,
    ) -> bool:
        """Return whether the snapshot is current on this host boot clock."""
        try:
            now = (
                _boottime_ns()
                if now_boottime_ns is None
                else _exact_integer(
                    now_boottime_ns,
                    'now_boottime_ns',
                    minimum=0,
                )
            )
        except (
            OSError,
            OverflowError,
            RuntimeError,
            TrustedRobotStateError,
            TypeError,
            ValueError,
        ):
            return False
        return self.assembled_boottime_ns <= now < (
            self.valid_until_boottime_ns
        )

    def require_complete_for_monitor_room(
        self,
        now_boottime_ns: Optional[int] = None,
    ) -> RobotState:
        """Build a safety-only state when every required value is known."""
        if not self.is_current(now_boottime_ns):
            raise TrustedRobotStateError(
                'robot_state_stale',
            ) from None
        missing = [
            name
            for name in _MONITOR_ROOM_REQUIRED_FIELDS
            if getattr(self, name) is None
            or self.field_evidence.get(name) is None
        ]
        if missing:
            raise TrustedRobotStateError(
                'robot_state_incomplete',
            ) from None
        # The checks above narrow these values at runtime.  Keep conversion
        # explicit so a nullable field can never silently use RobotState's
        # known-safe-looking defaults.
        assert self.battery_percent is not None
        assert self.navigation_available is not None
        assert self.localization_ok is not None
        assert self.emergency_stop is not None
        assert self.camera_available is not None
        assert self.privacy_mode is not None
        assert self.forbidden_zones is not None
        return RobotState(
            battery_percent=self.battery_percent,
            navigation_available=self.navigation_available,
            localization_ok=self.localization_ok,
            emergency_stop=self.emergency_stop,
            camera_available=self.camera_available,
            privacy_mode=self.privacy_mode,
            # Dock state is not part of monitor_room safety.  Unknown is not
            # promoted into authority because this evidence is tool-scoped.
            docked=(self.docked if self.docked is not None else False),
            forbidden_zones=self.forbidden_zones,
        )

    def to_private_dict(self) -> Dict[str, Any]:
        """Return the full validated snapshot for local diagnostics only."""
        return {
            'schema_version': TRUSTED_ROBOT_STATE_SCHEMA_VERSION,
            'evidence_digest': self.evidence_digest,
            'source': {
                'kind': TRUSTED_ROBOT_STATE_SOURCE_KIND,
                'host_boot_id': self.host_boot_id,
                'instance_id': self.instance_id,
                'sequence': str(self.sequence),
                'physical_authority': True,
            },
            'binding': {
                'device_id': self.device_id,
                'map_id': self.map_id,
                'map_revision': self.map_revision,
            },
            'assembled_at': self.assembled_at,
            'assembled_boottime_ns': str(
                self.assembled_boottime_ns
            ),
            'valid_until_boottime_ns': str(
                self.valid_until_boottime_ns
            ),
            'state': {
                'battery_percent': self.battery_percent,
                'navigation_available': self.navigation_available,
                'localization_ok': self.localization_ok,
                'emergency_stop': self.emergency_stop,
                'camera_available': self.camera_available,
                'privacy_mode': self.privacy_mode,
                'docked': self.docked,
                'forbidden_zones': (
                    list(self.forbidden_zones)
                    if self.forbidden_zones is not None
                    else None
                ),
            },
            'evidence': {
                name: (
                    value.to_dict() if value is not None else None
                )
                for name, value in self.field_evidence.items()
            },
        }


@dataclass(frozen=True)
class GazeboSimulationReadiness:
    """Only the two proposal-time facts relevant to Gazebo admission."""

    navigation_available: bool
    localization_ok: bool

    def __post_init__(self) -> None:
        """Reject truthy aliases and incomplete simulated readiness."""
        if (
            type(self.navigation_available) is not bool
            or type(self.localization_ok) is not bool
        ):
            raise ValueError('Gazebo simulation readiness is invalid')


@dataclass(frozen=True)
class GazeboSimulationStateEvidence:
    """
    A non-physical ROS snapshot carrying only Gazebo readiness.

    The wire collector still sends the fixed v1 state envelope, but this
    parallel projection requires ``physical_authority=false`` and requires
    every battery, e-stop, camera, privacy, dock, and zone field to remain
    unknown.  It cannot be passed to the physical ``RobotState`` gate.
    """

    evidence_digest: str
    device_id: str
    map_id: str
    map_revision: str
    host_boot_id: str
    instance_id: str
    sequence: int
    assembled_at: str
    assembled_boottime_ns: int
    valid_until_boottime_ns: int
    navigation_available: bool
    localization_ok: bool
    field_evidence: Mapping[str, RobotStateFieldEvidence]
    physical_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Validate the complete non-physical projection."""
        if (
            type(self.evidence_digest) is not str
            or not _LOWER_HEX_64.fullmatch(self.evidence_digest)
        ):
            raise ValueError('evidence_digest is invalid')
        for name in ('device_id', 'map_id', 'map_revision'):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'host_boot_id',
            _canonical_uuid(self.host_boot_id, 'host_boot_id'),
        )
        object.__setattr__(
            self,
            'instance_id',
            _canonical_uuid(self.instance_id, 'instance_id'),
        )
        sequence = _exact_integer(self.sequence, 'sequence', minimum=0)
        assembled = _exact_integer(
            self.assembled_boottime_ns,
            'assembled_boottime_ns',
            minimum=1,
        )
        valid_until = _exact_integer(
            self.valid_until_boottime_ns,
            'valid_until_boottime_ns',
            minimum=1,
        )
        if (
            valid_until <= assembled
            or valid_until - assembled > MAX_ROBOT_STATE_LIFETIME_NS
        ):
            raise ValueError('state evidence validity interval is invalid')
        object.__setattr__(self, 'sequence', sequence)
        object.__setattr__(self, 'assembled_at', _audit_timestamp(
            self.assembled_at
        ))
        object.__setattr__(self, 'assembled_boottime_ns', assembled)
        object.__setattr__(self, 'valid_until_boottime_ns', valid_until)
        if (
            type(self.navigation_available) is not bool
            or type(self.localization_ok) is not bool
            or self.physical_authority is not False
        ):
            raise ValueError('Gazebo simulation readiness is invalid')
        if (
            not isinstance(self.field_evidence, Mapping)
            or set(self.field_evidence)
            != {'navigation_available', 'localization_ok'}
        ):
            raise ValueError('Gazebo simulation field evidence is invalid')
        frozen = dict(self.field_evidence)
        for item in frozen.values():
            if not isinstance(item, RobotStateFieldEvidence):
                raise ValueError(
                    'Gazebo simulation field evidence is invalid'
                )
            if (
                item.received_boottime_ns > assembled
                or assembled - item.received_boottime_ns
                > MAX_ROBOT_STATE_LIFETIME_NS
                or valid_until
                > item.received_boottime_ns
                + MAX_ROBOT_STATE_LIFETIME_NS
            ):
                raise ValueError('Gazebo simulation field evidence is stale')
        object.__setattr__(
            self,
            'field_evidence',
            MappingProxyType(frozen),
        )

    def is_current(self, now_boottime_ns: Optional[int] = None) -> bool:
        """Return whether this snapshot is current on the host boot clock."""
        try:
            now = (
                _boottime_ns()
                if now_boottime_ns is None
                else _exact_integer(
                    now_boottime_ns,
                    'now_boottime_ns',
                    minimum=0,
                )
            )
        except (
            OSError,
            OverflowError,
            RuntimeError,
            TrustedRobotStateError,
            TypeError,
            ValueError,
        ):
            return False
        return self.assembled_boottime_ns <= now < (
            self.valid_until_boottime_ns
        )

    def require_ready(
        self,
        now_boottime_ns: Optional[int] = None,
    ) -> GazeboSimulationReadiness:
        """Return only current Nav2/localization readiness."""
        if not self.is_current(now_boottime_ns):
            raise TrustedRobotStateError('robot_state_stale') from None
        return GazeboSimulationReadiness(
            navigation_available=self.navigation_available,
            localization_ok=self.localization_ok,
        )

    def to_private_dict(self) -> Dict[str, Any]:
        """Return the exact non-physical collector projection."""
        evidence = {
            name: None for name in _STATE_FIELDS
        }
        evidence.update({
            name: value.to_dict()
            for name, value in self.field_evidence.items()
        })
        return {
            'schema_version': TRUSTED_ROBOT_STATE_SCHEMA_VERSION,
            'evidence_digest': self.evidence_digest,
            'source': {
                'kind': TRUSTED_ROBOT_STATE_SOURCE_KIND,
                'host_boot_id': self.host_boot_id,
                'instance_id': self.instance_id,
                'sequence': str(self.sequence),
                'physical_authority': False,
            },
            'binding': {
                'device_id': self.device_id,
                'map_id': self.map_id,
                'map_revision': self.map_revision,
            },
            'assembled_at': self.assembled_at,
            'assembled_boottime_ns': str(self.assembled_boottime_ns),
            'valid_until_boottime_ns': str(
                self.valid_until_boottime_ns
            ),
            'state': {
                'battery_percent': None,
                'navigation_available': self.navigation_available,
                'localization_ok': self.localization_ok,
                'emergency_stop': None,
                'camera_available': None,
                'privacy_mode': None,
                'docked': None,
                'forbidden_zones': None,
            },
            'evidence': evidence,
        }


_GAZEBO_ADMISSION_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True)
class GazeboSimulationAdmissionEvidence:
    """Server-issued, user- and semantic-bound Gazebo admission proof."""

    evidence_digest: str
    user_id: str
    device_id: str
    map_id: str
    map_revision: str
    host_boot_id: str
    instance_id: str
    sequence: int
    assembled_boottime_ns: int
    valid_until_boottime_ns: int
    semantic_content_sha256: str
    zones_digest: str
    semantic_map_generation: int
    semantic_authorization_generation: int
    semantic_expires_at_ms: int
    room_id: str
    geometry_digest: str
    source_arguments_digest: str
    target_binding_digest: str
    effects_digest: str
    navigation_available: bool
    localization_ok: bool
    _semantic_evidence: VerifiedSemanticSnapshotEvidence = field(
        repr=False,
        compare=False,
    )
    _robot_state_evidence: GazeboSimulationStateEvidence = field(
        repr=False,
        compare=False,
    )
    _construction_token: InitVar[object] = None
    schema_version: int = GAZEBO_SIMULATION_ADMISSION_SCHEMA_VERSION
    scope: str = 'monitor_room'
    profile: str = GAZEBO_SIMULATION_ADMISSION_PROFILE
    runtime_mode: str = GAZEBO_SIMULATION_RUNTIME_MODE
    simulation: bool = field(default=True, init=False)
    physical_authority: bool = field(default=False, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)

    def __post_init__(self, _construction_token: object) -> None:
        """Reject public construction and any stronger authority claim."""
        if _construction_token is not _GAZEBO_ADMISSION_CONSTRUCTION_TOKEN:
            raise TypeError(
                'Gazebo admission evidence must be server-issued'
            )
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != GAZEBO_SIMULATION_ADMISSION_SCHEMA_VERSION
            or self.scope != 'monitor_room'
            or self.profile != GAZEBO_SIMULATION_ADMISSION_PROFILE
            or self.runtime_mode != GAZEBO_SIMULATION_RUNTIME_MODE
            or self.simulation is not True
            or self.physical_authority is not False
            or self.physical_authorized is not False
            or self.physical_effects is not False
        ):
            raise ValueError('Gazebo admission profile is invalid')
        if (
            type(self.evidence_digest) is not str
            or not _LOWER_HEX_64.fullmatch(self.evidence_digest)
        ):
            raise ValueError('evidence_digest is invalid')
        object.__setattr__(self, 'user_id', validate_user_id(self.user_id))
        for name in (
            'device_id', 'map_id', 'map_revision', 'room_id'
        ):
            object.__setattr__(
                self, name, _identifier(getattr(self, name), name)
            )
        object.__setattr__(
            self,
            'host_boot_id',
            _canonical_uuid(self.host_boot_id, 'host_boot_id'),
        )
        object.__setattr__(
            self,
            'instance_id',
            _canonical_uuid(self.instance_id, 'instance_id'),
        )
        for name in (
            'semantic_content_sha256',
            'zones_digest',
            'geometry_digest',
            'source_arguments_digest',
            'target_binding_digest',
            'effects_digest',
        ):
            value = getattr(self, name)
            if type(value) is not str or not _LOWER_HEX_64.fullmatch(value):
                raise ValueError(f'{name} is invalid')
        for name, minimum in (
            ('sequence', 0),
            ('assembled_boottime_ns', 1),
            ('valid_until_boottime_ns', 1),
            ('semantic_map_generation', 1),
            ('semantic_authorization_generation', 1),
            ('semantic_expires_at_ms', 1),
        ):
            object.__setattr__(
                self,
                name,
                _exact_integer(getattr(self, name), name, minimum=minimum),
            )
        if (
            self.valid_until_boottime_ns
            <= self.assembled_boottime_ns
            or self.valid_until_boottime_ns
            - self.assembled_boottime_ns
            > MAX_ROBOT_STATE_LIFETIME_NS
            or type(self.navigation_available) is not bool
            or type(self.localization_ok) is not bool
        ):
            raise ValueError('Gazebo admission lifetime is invalid')
        if (
            type(self._semantic_evidence)
            is not VerifiedSemanticSnapshotEvidence
            or type(self._robot_state_evidence)
            is not GazeboSimulationStateEvidence
            or self._semantic_evidence.content_sha256
            != self.semantic_content_sha256
            or self._semantic_evidence.snapshot.zones_digest
            != self.zones_digest
            or self._semantic_evidence.map_generation
            != self.semantic_map_generation
            or self._semantic_evidence.authorization_generation
            != self.semantic_authorization_generation
            or self._semantic_evidence.expires_at_ms
            != self.semantic_expires_at_ms
            or self._robot_state_evidence.device_id != self.device_id
            or self._robot_state_evidence.map_id != self.map_id
            or self._robot_state_evidence.map_revision != self.map_revision
            or self._robot_state_evidence.host_boot_id != self.host_boot_id
            or self._robot_state_evidence.instance_id != self.instance_id
            or self._robot_state_evidence.sequence != self.sequence
            or self._robot_state_evidence.navigation_available
            != self.navigation_available
            or self._robot_state_evidence.localization_ok
            != self.localization_ok
        ):
            raise ValueError('Gazebo admission source evidence is invalid')

    @property
    def semantic_evidence(self) -> VerifiedSemanticSnapshotEvidence:
        """Return the authenticated snapshot retained for atomic enqueue."""
        return self._semantic_evidence

    @property
    def robot_state_evidence(self) -> GazeboSimulationStateEvidence:
        """Return the non-physical ROS evidence retained for enqueue."""
        return self._robot_state_evidence

    def is_current(self, now_boottime_ns: Optional[int] = None) -> bool:
        """Return whether this combined admission is still current."""
        try:
            now = (
                _boottime_ns()
                if now_boottime_ns is None
                else _exact_integer(
                    now_boottime_ns,
                    'now_boottime_ns',
                    minimum=0,
                )
            )
        except (
            OSError,
            OverflowError,
            RuntimeError,
            TrustedRobotStateError,
            TypeError,
            ValueError,
        ):
            return False
        return self.assembled_boottime_ns <= now < (
            self.valid_until_boottime_ns
        )

    def require_ready(
        self,
        now_boottime_ns: Optional[int] = None,
    ) -> GazeboSimulationReadiness:
        """Return current readiness without manufacturing other facts."""
        if not self.is_current(now_boottime_ns):
            raise TrustedRobotStateError('robot_state_stale') from None
        return GazeboSimulationReadiness(
            navigation_available=self.navigation_available,
            localization_ok=self.localization_ok,
        )

    def matches_target(self, target: TargetBinding) -> bool:
        """Return whether one freshly resolved target is exactly bound."""
        return (
            type(target) is TargetBinding
            and target.device_id == self.device_id
            and target.map_id == self.map_id
            and target.map_revision == self.map_revision
            and target.room_id == self.room_id
            and target.geometry_digest == self.geometry_digest
            and target.source_arguments_digest
            == self.source_arguments_digest
            and target.binding_digest == self.target_binding_digest
            and target.effects_digest == self.effects_digest
            and target.effects.gazebo_simulation_navigation
        )

    def to_private_dict(self) -> Dict[str, Any]:
        """Return content-minimized evidence for local integrity hashing."""
        return {
            'schema_version': self.schema_version,
            'scope': self.scope,
            'profile': self.profile,
            'runtime_mode': self.runtime_mode,
            'evidence_digest': self.evidence_digest,
            'user_id': self.user_id,
            'device_id': self.device_id,
            'map_id': self.map_id,
            'map_revision': self.map_revision,
            'host_boot_id': self.host_boot_id,
            'instance_id': self.instance_id,
            'sequence': self.sequence,
            'assembled_boottime_ns': self.assembled_boottime_ns,
            'valid_until_boottime_ns': self.valid_until_boottime_ns,
            'semantic_content_sha256': self.semantic_content_sha256,
            'zones_digest': self.zones_digest,
            'semantic_map_generation': self.semantic_map_generation,
            'semantic_authorization_generation': (
                self.semantic_authorization_generation
            ),
            'semantic_expires_at_ms': self.semantic_expires_at_ms,
            'room_id': self.room_id,
            'geometry_digest': self.geometry_digest,
            'source_arguments_digest': self.source_arguments_digest,
            'target_binding_digest': self.target_binding_digest,
            'effects_digest': self.effects_digest,
            'navigation_available': self.navigation_available,
            'localization_ok': self.localization_ok,
            'simulation': True,
            'physical_authority': False,
            'physical_authorized': False,
            'physical_effects': False,
        }


class TrustedRobotStateSource(Protocol):
    """A fixed, server-owned source of current local robot evidence."""

    def read(self) -> TrustedRobotStateEvidence:
        """Return one current, replay-checked snapshot."""
        ...


class GazeboSimulationStateSource(Protocol):
    """A server-owned source of non-physical Gazebo readiness."""

    def read(self) -> GazeboSimulationStateEvidence:
        """Return one current, replay-checked non-physical snapshot."""
        ...


class GazeboSemanticEvidenceSource(Protocol):
    """Read-only source of one signed current semantic snapshot."""

    def fetch_snapshot_evidence(
        self,
    ) -> VerifiedSemanticSnapshotEvidence:
        """Return one current server-verified semantic projection."""
        ...


def _boottime_ns() -> int:
    """Read the same-host suspend-aware monotonic clock or fail closed."""
    clock_id = getattr(time, 'CLOCK_BOOTTIME', None)
    unavailable = clock_id is None
    value = 0
    if not unavailable:
        try:
            value = time.clock_gettime_ns(clock_id)
        except (OSError, OverflowError, RuntimeError, ValueError):
            unavailable = True
    if unavailable:
        raise _error('robot_state_clock_unavailable')
    invalid = False
    try:
        return _exact_integer(value, 'boottime_ns', minimum=0)
    except (TypeError, ValueError):
        invalid = True
    if invalid:
        raise _error('robot_state_clock_unavailable')
    raise AssertionError('boottime validation did not return or fail')


def trusted_boottime_ns() -> int:
    """Return strict Linux CLOCK_BOOTTIME for trusted integrations."""
    return _boottime_ns()


def _error(code: str) -> TrustedRobotStateError:
    return TrustedRobotStateError(code)


def _exact_integer(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int = MAX_ROBOT_STATE_SEQUENCE,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(f'{field_name} is invalid')
    return value


def _decimal_integer(
    value: Any,
    field_name: str,
    *,
    minimum: int,
) -> int:
    if (
        not isinstance(value, str)
        or not _UNSIGNED_DECIMAL.fullmatch(value)
    ):
        raise ValueError(f'{field_name} is invalid')
    result = int(value)
    if result < minimum or result > MAX_ROBOT_STATE_SEQUENCE:
        raise ValueError(f'{field_name} is invalid')
    return result


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f'{field_name} is invalid')
    return value


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{field_name} is invalid')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f'{field_name} is invalid') from None
    if str(parsed) != value:
        raise ValueError(f'{field_name} is invalid')
    return value


def _audit_timestamp(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.isascii()
    ):
        raise ValueError('assembled_at is invalid')
    normalized = value.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError('assembled_at is invalid') from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('assembled_at is invalid')
    return value


def _nullable_bool(value: Any, field_name: str) -> Optional[bool]:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f'{field_name} is invalid')
    return value


def _battery(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('battery_percent is invalid')
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 100:
        raise ValueError('battery_percent is invalid')
    return result


def _zones(value: Any) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError('forbidden_zones is invalid')
    result = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 128
        ):
            raise ValueError('forbidden_zones is invalid')
        normalized = unicodedata.normalize('NFKC', item)
        normalized = ' '.join(normalized.split()).casefold()
        if (
            not normalized
            or len(normalized) > 128
            or any(
                unicodedata.category(character).startswith('C')
                for character in normalized
            )
        ):
            raise ValueError('forbidden_zones is invalid')
        result.append(normalized)
    if len(set(result)) != len(result):
        raise ValueError('forbidden_zones is invalid')
    return tuple(result)


def _field_evidence(
    value: Any,
    field_name: str,
    assembled_boottime_ns: int,
    valid_until_boottime_ns: int,
) -> Optional[RobotStateFieldEvidence]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != (
        _FIELD_EVIDENCE_FIELDS
    ):
        raise ValueError(f'{field_name} evidence is invalid')
    source = _identifier(value['source'], f'{field_name}.source')
    received = _decimal_integer(
        value['received_boottime_ns'],
        f'{field_name}.received_boottime_ns',
        minimum=1,
    )
    if received > assembled_boottime_ns:
        raise ValueError(f'{field_name} evidence is invalid')
    if assembled_boottime_ns - received > MAX_ROBOT_STATE_LIFETIME_NS:
        raise ValueError(f'{field_name} evidence is stale')
    if valid_until_boottime_ns > (
        received + MAX_ROBOT_STATE_LIFETIME_NS
    ):
        raise ValueError(f'{field_name} evidence validity is invalid')
    return RobotStateFieldEvidence(
        source=source,
        received_boottime_ns=received,
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')


def _parse_trusted_robot_state_envelope_impl(
    value: Any,
    *,
    expected_nonce: str,
    expected_device_id: str,
    expected_host_boot_id: str,
    now_boottime_ns: Optional[int] = None,
) -> TrustedRobotStateEvidence:
    """Validate one strict nonce-, host-, device-, and time-bound envelope."""
    try:
        nonce = _nonce(expected_nonce)
        device_id = _identifier(expected_device_id, 'expected_device_id')
        host_boot_id = _canonical_uuid(
            expected_host_boot_id,
            'expected_host_boot_id',
        )
        now = (
            _boottime_ns()
            if now_boottime_ns is None
            else _exact_integer(
                now_boottime_ns,
                'now_boottime_ns',
                minimum=0,
            )
        )
        if not isinstance(value, dict) or set(value) != _ENVELOPE_FIELDS:
            raise ValueError('snapshot fields are invalid')
        if (
            type(value['schema_version']) is not int
            or value['schema_version']
            != TRUSTED_ROBOT_STATE_SCHEMA_VERSION
        ):
            raise ValueError('snapshot schema is unsupported')
        if value['nonce'] != nonce:
            raise TrustedRobotStateError('robot_state_nonce_mismatch')

        source = value['source']
        if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
            raise ValueError('snapshot source is invalid')
        if source['kind'] != TRUSTED_ROBOT_STATE_SOURCE_KIND:
            raise ValueError('snapshot source is invalid')
        if source['physical_authority'] is not True:
            raise TrustedRobotStateError(
                'robot_state_physical_authority_missing'
            )
        snapshot_boot_id = _canonical_uuid(
            source['host_boot_id'],
            'host_boot_id',
        )
        if snapshot_boot_id != host_boot_id:
            raise TrustedRobotStateError(
                'robot_state_boot_mismatch'
            )
        instance_id = _canonical_uuid(
            source['instance_id'],
            'instance_id',
        )
        sequence = _decimal_integer(
            source['sequence'],
            'sequence',
            minimum=0,
        )

        binding = value['binding']
        if not isinstance(binding, dict) or set(binding) != (
            _BINDING_FIELDS
        ):
            raise ValueError('snapshot binding is invalid')
        snapshot_device_id = _identifier(
            binding['device_id'],
            'device_id',
        )
        if snapshot_device_id != device_id:
            raise TrustedRobotStateError(
                'robot_state_binding_mismatch'
            )
        map_id = _identifier(binding['map_id'], 'map_id')
        map_revision = _identifier(
            binding['map_revision'],
            'map_revision',
        )
        assembled_at = _audit_timestamp(value['assembled_at'])
        assembled = _decimal_integer(
            value['assembled_boottime_ns'],
            'assembled_boottime_ns',
            minimum=1,
        )
        valid_until = _decimal_integer(
            value['valid_until_boottime_ns'],
            'valid_until_boottime_ns',
            minimum=1,
        )
        if (
            assembled > now
            or valid_until <= assembled
            or valid_until - assembled > MAX_ROBOT_STATE_LIFETIME_NS
        ):
            raise ValueError('snapshot validity interval is invalid')
        if now >= valid_until:
            raise TrustedRobotStateError('robot_state_stale')

        state = value['state']
        if not isinstance(state, dict) or tuple(sorted(state)) != (
            tuple(sorted(_STATE_FIELDS))
        ):
            raise ValueError('snapshot state is invalid')
        battery_percent = _battery(state['battery_percent'])
        navigation_available = _nullable_bool(
            state['navigation_available'],
            'navigation_available',
        )
        localization_ok = _nullable_bool(
            state['localization_ok'],
            'localization_ok',
        )
        emergency_stop = _nullable_bool(
            state['emergency_stop'],
            'emergency_stop',
        )
        camera_available = _nullable_bool(
            state['camera_available'],
            'camera_available',
        )
        privacy_mode = _nullable_bool(
            state['privacy_mode'],
            'privacy_mode',
        )
        docked = _nullable_bool(state['docked'], 'docked')
        forbidden_zones = _zones(state['forbidden_zones'])
        normalized_state = {
            'battery_percent': battery_percent,
            'navigation_available': navigation_available,
            'localization_ok': localization_ok,
            'emergency_stop': emergency_stop,
            'camera_available': camera_available,
            'privacy_mode': privacy_mode,
            'docked': docked,
            'forbidden_zones': forbidden_zones,
        }

        evidence_value = value['evidence']
        if not isinstance(evidence_value, dict) or set(
            evidence_value
        ) != set(_STATE_FIELDS):
            raise ValueError('snapshot field evidence is invalid')
        field_evidence = {
            name: _field_evidence(
                evidence_value[name],
                name,
                assembled,
                valid_until,
            )
            for name in _STATE_FIELDS
        }
        for name, state_value in normalized_state.items():
            if state_value is not None and field_evidence[name] is None:
                raise ValueError(f'{name} evidence is missing')

        # The nonce proves this response belongs to the current request but
        # is deliberately excluded from the stable evidence digest.  UTC is
        # audit-only and is also excluded because safety uses boottime.
        digest_body = {
            'schema_version': TRUSTED_ROBOT_STATE_SCHEMA_VERSION,
            'source': source,
            'binding': binding,
            'assembled_boottime_ns': value['assembled_boottime_ns'],
            'valid_until_boottime_ns': (
                value['valid_until_boottime_ns']
            ),
            # Values are already strict and bounded.  Hash their canonical
            # JSON wire representation rather than the normalized runtime
            # projection so one sequence cannot change serialization/body.
            'state': state,
            'evidence': evidence_value,
        }
        evidence_digest = hashlib.sha256(
            _canonical_json(digest_body)
        ).hexdigest()
        return TrustedRobotStateEvidence(
            evidence_digest=evidence_digest,
            device_id=snapshot_device_id,
            map_id=map_id,
            map_revision=map_revision,
            host_boot_id=snapshot_boot_id,
            instance_id=instance_id,
            sequence=sequence,
            assembled_at=assembled_at,
            assembled_boottime_ns=assembled,
            valid_until_boottime_ns=valid_until,
            battery_percent=battery_percent,
            navigation_available=navigation_available,
            localization_ok=localization_ok,
            emergency_stop=emergency_stop,
            camera_available=camera_available,
            privacy_mode=privacy_mode,
            docked=docked,
            forbidden_zones=forbidden_zones,
            field_evidence=field_evidence,
        )
    except TrustedRobotStateError:
        raise
    except (OverflowError, TypeError, ValueError):
        raise _error('robot_state_invalid_snapshot') from None


def parse_trusted_robot_state_envelope(
    value: Any,
    *,
    expected_nonce: str,
    expected_device_id: str,
    expected_host_boot_id: str,
    now_boottime_ns: Optional[int] = None,
) -> TrustedRobotStateEvidence:
    """Validate one envelope and discard all private exception context."""
    failure = None
    try:
        return _parse_trusted_robot_state_envelope_impl(
            value,
            expected_nonce=expected_nonce,
            expected_device_id=expected_device_id,
            expected_host_boot_id=expected_host_boot_id,
            now_boottime_ns=now_boottime_ns,
        )
    except TrustedRobotStateError as error:
        failure = error
    assert failure is not None
    failure.__cause__ = None
    failure.__context__ = None
    failure.__suppress_context__ = True
    raise failure.with_traceback(None)


def _parse_gazebo_simulation_state_envelope_impl(
    value: Any,
    *,
    expected_nonce: str,
    expected_device_id: str,
    expected_host_boot_id: str,
    now_boottime_ns: Optional[int] = None,
) -> GazeboSimulationStateEvidence:
    """Validate the parallel physical-authority=false collector profile."""
    try:
        if type(value) is not dict or set(value) != _ENVELOPE_FIELDS:
            raise ValueError('snapshot fields are invalid')
        source = value.get('source')
        if type(source) is not dict or set(source) != _SOURCE_FIELDS:
            raise ValueError('snapshot source is invalid')
        if source.get('kind') != TRUSTED_ROBOT_STATE_SOURCE_KIND:
            raise ValueError('snapshot source is invalid')
        if source.get('physical_authority') is not False:
            raise TrustedRobotStateError(
                'robot_state_simulation_authority_invalid'
            )

        # Serialize once into plain JSON containers before changing the local
        # validation copy.  This prevents a dict subclass from returning one
        # value for the non-physical check and another to the strict parser.
        wire = _strict_json_loads(_canonical_json(value))
        normalized = _strict_json_loads(_canonical_json(wire))
        original_source = dict(wire['source'])
        normalized['source']['physical_authority'] = True
        parsed = _parse_trusted_robot_state_envelope_impl(
            normalized,
            expected_nonce=expected_nonce,
            expected_device_id=expected_device_id,
            expected_host_boot_id=expected_host_boot_id,
            now_boottime_ns=now_boottime_ns,
        )
        state = wire['state']
        evidence = wire['evidence']
        if type(state) is not dict or type(evidence) is not dict:
            raise ValueError('snapshot state is invalid')
        unknown_fields = (
            'battery_percent',
            'emergency_stop',
            'camera_available',
            'privacy_mode',
            'docked',
            'forbidden_zones',
        )
        if any(
            state.get(name) is not None or evidence.get(name) is not None
            for name in unknown_fields
        ):
            raise TrustedRobotStateError(
                'robot_state_simulation_physical_fact_present'
            )
        if (
            type(parsed.navigation_available) is not bool
            or type(parsed.localization_ok) is not bool
            or parsed.field_evidence['navigation_available'] is None
            or parsed.field_evidence['localization_ok'] is None
        ):
            raise TrustedRobotStateError(
                'robot_state_simulation_incomplete'
            )
        digest_body = {
            'schema_version': TRUSTED_ROBOT_STATE_SCHEMA_VERSION,
            'source': original_source,
            'binding': wire['binding'],
            'assembled_boottime_ns': wire['assembled_boottime_ns'],
            'valid_until_boottime_ns': wire[
                'valid_until_boottime_ns'
            ],
            'state': wire['state'],
            'evidence': wire['evidence'],
        }
        digest = hashlib.sha256(
            _canonical_json(digest_body)
        ).hexdigest()
        return GazeboSimulationStateEvidence(
            evidence_digest=digest,
            device_id=parsed.device_id,
            map_id=parsed.map_id,
            map_revision=parsed.map_revision,
            host_boot_id=parsed.host_boot_id,
            instance_id=parsed.instance_id,
            sequence=parsed.sequence,
            assembled_at=parsed.assembled_at,
            assembled_boottime_ns=parsed.assembled_boottime_ns,
            valid_until_boottime_ns=parsed.valid_until_boottime_ns,
            navigation_available=parsed.navigation_available,
            localization_ok=parsed.localization_ok,
            field_evidence={
                'navigation_available': parsed.field_evidence[
                    'navigation_available'
                ],
                'localization_ok': parsed.field_evidence[
                    'localization_ok'
                ],
            },
        )
    except TrustedRobotStateError:
        raise
    except (
        KeyError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        raise _error('robot_state_invalid_snapshot') from None


def parse_gazebo_simulation_state_envelope(
    value: Any,
    *,
    expected_nonce: str,
    expected_device_id: str,
    expected_host_boot_id: str,
    now_boottime_ns: Optional[int] = None,
) -> GazeboSimulationStateEvidence:
    """Validate non-physical Gazebo readiness and strip error context."""
    failure = None
    try:
        return _parse_gazebo_simulation_state_envelope_impl(
            value,
            expected_nonce=expected_nonce,
            expected_device_id=expected_device_id,
            expected_host_boot_id=expected_host_boot_id,
            now_boottime_ns=now_boottime_ns,
        )
    except TrustedRobotStateError as error:
        failure = error
    assert failure is not None
    failure.__cause__ = None
    failure.__context__ = None
    failure.__suppress_context__ = True
    raise failure.with_traceback(None)


def _nonce(value: Any) -> str:
    if not isinstance(value, str) or not _LOWER_HEX_64.fullmatch(value):
        raise ValueError('nonce is invalid')
    return value


def _strict_json_loads(payload: bytes) -> Any:
    def reject_duplicate(pairs: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError('non-finite JSON number')

    invalid_utf8 = False
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        invalid_utf8 = True
        text = ''
    if invalid_utf8:
        raise _error('robot_state_invalid_utf8')
    invalid_json = False
    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        invalid_json = True
    if invalid_json:
        raise _error('robot_state_invalid_json')
    raise AssertionError('JSON decoding did not return or fail')


def _read_local_boot_id() -> str:
    unavailable = False
    try:
        with open(
            '/proc/sys/kernel/random/boot_id',
            'r',
            encoding='ascii',
        ) as stream:
            value = stream.read(64).strip()
    except (OSError, UnicodeError):
        unavailable = True
        value = ''
    if unavailable:
        raise _error('robot_state_boot_unavailable')
    invalid = False
    try:
        return _canonical_uuid(value, 'host_boot_id')
    except ValueError:
        invalid = True
    if invalid:
        raise _error('robot_state_boot_unavailable')
    raise AssertionError('boot ID validation did not return or fail')


class UnixSocketTrustedRobotStateSource:
    """Read one strict evidence frame from a fixed local Unix socket."""

    def __init__(
        self,
        socket_path: str,
        expected_uid: int,
        expected_device_id: str,
        timeout_seconds: float = DEFAULT_ROBOT_STATE_TIMEOUT_SECONDS,
    ) -> None:
        """Bind to one path, peer UID, device, boot, and bounded clock."""
        self._initialize(
            socket_path,
            expected_uid,
            expected_device_id,
            timeout_seconds,
            boottime_ns=_boottime_ns,
            nonce_factory=lambda: secrets.token_hex(32),
            expected_host_boot_id=_read_local_boot_id(),
        )

    @classmethod
    def _for_test(
        cls,
        socket_path: str,
        expected_uid: int,
        expected_device_id: str,
        timeout_seconds: float = DEFAULT_ROBOT_STATE_TIMEOUT_SECONDS,
        *,
        boottime_ns: Callable[[], int],
        nonce_factory: Callable[[], str],
        expected_host_boot_id: str,
    ) -> 'UnixSocketTrustedRobotStateSource':
        """Build deterministic test evidence without widening production."""
        instance = cls.__new__(cls)
        instance._initialize(
            socket_path,
            expected_uid,
            expected_device_id,
            timeout_seconds,
            boottime_ns=boottime_ns,
            nonce_factory=nonce_factory,
            expected_host_boot_id=expected_host_boot_id,
        )
        return instance

    def _initialize(
        self,
        socket_path: str,
        expected_uid: int,
        expected_device_id: str,
        timeout_seconds: float,
        *,
        boottime_ns: Callable[[], int],
        nonce_factory: Callable[[], str],
        expected_host_boot_id: str,
    ) -> None:
        """Initialize either the production or private deterministic seam."""
        invalid_socket_path = (
            not isinstance(socket_path, str)
            or not socket_path
            or '\x00' in socket_path
            or not os.path.isabs(socket_path)
            or os.path.normpath(socket_path) != socket_path
            or not Path(socket_path).name
        )
        encoded_socket_path = b''
        if not invalid_socket_path:
            try:
                encoded_socket_path = os.fsencode(socket_path)
            except (UnicodeEncodeError, ValueError):
                invalid_socket_path = True
        if invalid_socket_path or len(encoded_socket_path) > 103:
            raise ValueError('robot-state socket path is invalid')
        self._socket_path = socket_path
        self._expected_uid = _exact_integer(
            expected_uid,
            'expected_uid',
            minimum=0,
            maximum=(1 << 31) - 1,
        )
        self._expected_device_id = _identifier(
            expected_device_id,
            'expected_device_id',
        )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
        ):
            raise ValueError('robot-state timeout is invalid')
        timeout = float(timeout_seconds)
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or timeout > MAX_ROBOT_STATE_TIMEOUT_SECONDS
        ):
            raise ValueError('robot-state timeout is invalid')
        if not callable(boottime_ns) or not callable(nonce_factory):
            raise TypeError('robot-state clock and nonce source are required')
        self._timeout_seconds = timeout
        self._boottime_ns = boottime_ns
        self._nonce_factory = nonce_factory
        self._expected_host_boot_id = _canonical_uuid(
            expected_host_boot_id,
            'expected_host_boot_id',
        )
        self._lock = threading.RLock()
        self._active_instance_id: Optional[str] = None
        self._highest_sequence: Optional[int] = None
        self._last_digest: Optional[str] = None
        self._retired_instances = set()

    @property
    def socket_path(self) -> str:
        """Return the fixed collector path."""
        return self._socket_path

    @property
    def expected_uid(self) -> int:
        """Return the fixed Linux peer UID."""
        return self._expected_uid

    @property
    def expected_device_id(self) -> str:
        """Return the fixed deployment device identity."""
        return self._expected_device_id

    @property
    def timeout_seconds(self) -> float:
        """Return the fixed total exchange timeout."""
        return self._timeout_seconds

    @property
    def expected_host_boot_id(self) -> str:
        """Return the boot identity captured at construction."""
        return self._expected_host_boot_id

    def read(self) -> TrustedRobotStateEvidence:
        """Read, validate, and replay-fence one current snapshot."""
        failure = None
        with self._lock:
            try:
                return self._read_locked()
            except TrustedRobotStateError as error:
                failure = error
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        raise failure.with_traceback(None)

    def _read_locked(self) -> TrustedRobotStateEvidence:
        """Perform one read while the anti-replay state is locked."""
        nonce = self._new_nonce()
        payload = self._read_payload(nonce)
        invalid_clock = False
        try:
            now = self._boottime_ns()
            now = _exact_integer(
                now,
                'now_boottime_ns',
                minimum=0,
            )
        except (
            OSError,
            OverflowError,
            RuntimeError,
            TrustedRobotStateError,
            TypeError,
            ValueError,
        ):
            invalid_clock = True
            now = 0
        if invalid_clock:
            raise _error('robot_state_clock_unavailable')
        evidence = parse_trusted_robot_state_envelope(
            payload,
            expected_nonce=nonce,
            expected_device_id=self.expected_device_id,
            expected_host_boot_id=self.expected_host_boot_id,
            now_boottime_ns=now,
        )
        self._accept_replay_state(evidence)
        return evidence

    def _new_nonce(self) -> str:
        unavailable = False
        try:
            return _nonce(self._nonce_factory())
        except Exception:
            unavailable = True
        if unavailable:
            raise _error('robot_state_nonce_unavailable')
        raise AssertionError('nonce generation did not return or fail')

    def _check_socket_path(
        self,
    ) -> Tuple[Tuple[str, int, int, int, int, int], ...]:
        """Validate and snapshot every component of the fixed path."""
        current = Path(self.socket_path).anchor
        snapshot = []
        for component in Path(self.socket_path).parts[1:]:
            current = os.path.join(current, component)
            component_unavailable = False
            try:
                component_metadata = os.lstat(current)
            except OSError:
                component_unavailable = True
                component_metadata = None
            if component_unavailable or component_metadata is None:
                raise _error('robot_state_socket_unavailable')
            if stat.S_ISLNK(component_metadata.st_mode):
                raise _error('robot_state_socket_path_invalid')
            snapshot.append(
                (
                    current,
                    component_metadata.st_dev,
                    component_metadata.st_ino,
                    component_metadata.st_mode,
                    component_metadata.st_uid,
                    component_metadata.st_gid,
                )
            )
        metadata = component_metadata
        if not stat.S_ISSOCK(metadata.st_mode):
            raise _error('robot_state_socket_not_socket') from None
        if metadata.st_uid != self.expected_uid:
            raise _error('robot_state_socket_owner_mismatch') from None
        # Group access is permitted for a dedicated Agent group, but a
        # world-writable collector socket is never a trusted endpoint.
        if metadata.st_mode & stat.S_IWOTH:
            raise _error('robot_state_socket_mode_insecure') from None
        return tuple(snapshot)

    def _read_payload(self, nonce: str) -> Any:
        failure = None
        try:
            return self._read_payload_impl(nonce)
        except TrustedRobotStateError as error:
            failure = error
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        raise failure.with_traceback(None)

    def _read_payload_impl(self, nonce: str) -> Any:
        """Exchange one frame; the wrapper strips transport context."""
        path_snapshot = self._check_socket_path()
        request = _canonical_json(
            {
                'schema_version': TRUSTED_ROBOT_STATE_SCHEMA_VERSION,
                'nonce': nonce,
            }
        )
        frame = struct.pack('!I', len(request)) + request
        try:
            deadline = self._transport_monotonic() + self.timeout_seconds
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self._set_remaining_timeout(connection, deadline)
                connection.connect(self.socket_path)
                self._check_peer(connection)
                if self._check_socket_path() != path_snapshot:
                    raise _error('robot_state_socket_path_changed')
                self._set_remaining_timeout(connection, deadline)
                connection.sendall(frame)
                connection.shutdown(socket.SHUT_WR)
                header = self._recv_exact(connection, 4, deadline)
                size = struct.unpack('!I', header)[0]
                if size < 1:
                    raise _error('robot_state_response_truncated')
                if size > MAX_ROBOT_STATE_FRAME_BYTES:
                    raise _error('robot_state_response_too_large')
                payload = self._recv_exact(
                    connection,
                    size,
                    deadline,
                )
                connection.setblocking(False)
                try:
                    trailing = connection.recv(
                        1,
                        getattr(socket, 'MSG_PEEK', 0),
                    )
                except BlockingIOError:
                    trailing = b''
                if trailing:
                    raise _error('robot_state_response_extra_data')
            finally:
                connection.close()
        except TrustedRobotStateError:
            raise
        except socket.timeout:
            raise _error('robot_state_response_timeout') from None
        except (OSError, RuntimeError):
            raise _error('robot_state_transport_unavailable') from None
        return _strict_json_loads(payload)

    @staticmethod
    def _set_remaining_timeout(
        connection: socket.socket,
        deadline: float,
    ) -> None:
        remaining = (
            deadline
            - UnixSocketTrustedRobotStateSource._transport_monotonic()
        )
        if not math.isfinite(remaining) or remaining <= 0:
            raise socket.timeout()
        connection.settimeout(remaining)

    @staticmethod
    def _transport_monotonic() -> float:
        """Read a finite transport clock without exposing raw failures."""
        unavailable = False
        try:
            value = time.monotonic()
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                unavailable = True
        except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
            unavailable = True
            value = 0.0
        if unavailable:
            raise _error('robot_state_clock_unavailable')
        return float(value)

    def _check_peer(self, connection: socket.socket) -> None:
        peer_option = getattr(socket, 'SO_PEERCRED', None)
        if peer_option is None:
            raise _error('robot_state_peer_unavailable')
        unavailable = False
        try:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET,
                peer_option,
                struct.calcsize('3i'),
            )
            _pid, uid, _gid = struct.unpack('3i', credentials)
        except (OSError, struct.error):
            unavailable = True
            uid = -1
        if unavailable:
            raise _error('robot_state_peer_unavailable')
        if uid != self.expected_uid:
            raise _error('robot_state_peer_uid_mismatch')

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
                raise _error('robot_state_response_truncated')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def _accept_replay_state(
        self,
        evidence: TrustedRobotStateEvidence,
    ) -> None:
        instance = evidence.instance_id
        if instance in self._retired_instances:
            raise _error('robot_state_retired_instance')
        if self._active_instance_id is None:
            self._active_instance_id = instance
            self._highest_sequence = evidence.sequence
            self._last_digest = evidence.evidence_digest
            return
        if instance != self._active_instance_id:
            self._retired_instances.add(self._active_instance_id)
            # Keep bounded process-local history.  This is defense in depth;
            # the fixed peer UID and short boottime TTL remain authoritative.
            if len(self._retired_instances) > 64:
                raise _error('robot_state_instance_churn')
            self._active_instance_id = instance
            self._highest_sequence = evidence.sequence
            self._last_digest = evidence.evidence_digest
            return
        assert self._highest_sequence is not None
        if evidence.sequence < self._highest_sequence:
            raise _error('robot_state_replay_regression')
        if (
            evidence.sequence == self._highest_sequence
            and evidence.evidence_digest != self._last_digest
        ):
            raise _error('robot_state_replay_conflict')
        if evidence.sequence > self._highest_sequence:
            self._highest_sequence = evidence.sequence
            self._last_digest = evidence.evidence_digest


class UnixSocketGazeboSimulationStateSource(
    UnixSocketTrustedRobotStateSource
):
    """Read only ``physical_authority=false`` Gazebo state envelopes."""

    def _read_locked(self) -> GazeboSimulationStateEvidence:
        """Perform one non-physical read under the inherited replay lock."""
        nonce = self._new_nonce()
        payload = self._read_payload(nonce)
        invalid_clock = False
        try:
            now = self._boottime_ns()
            now = _exact_integer(
                now,
                'now_boottime_ns',
                minimum=0,
            )
        except (
            OSError,
            OverflowError,
            RuntimeError,
            TrustedRobotStateError,
            TypeError,
            ValueError,
        ):
            invalid_clock = True
            now = 0
        if invalid_clock:
            raise _error('robot_state_clock_unavailable')
        evidence = parse_gazebo_simulation_state_envelope(
            payload,
            expected_nonce=nonce,
            expected_device_id=self.expected_device_id,
            expected_host_boot_id=self.expected_host_boot_id,
            now_boottime_ns=now,
        )
        self._accept_replay_state(evidence)
        return evidence

    def read(self) -> GazeboSimulationStateEvidence:
        """Return one replay-fenced non-physical Gazebo snapshot."""
        result = super().read()
        if type(result) is not GazeboSimulationStateEvidence:
            raise _error('robot_state_invalid_snapshot')
        return result


_GAZEBO_ADMISSION_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)
_GAZEBO_ADMISSION_SEAL_LOCK = threading.RLock()


class ServerGazeboSimulationAdmissionSource:
    """Mint fixed-user Gazebo admission from semantic and ROS evidence."""

    __slots__ = (
        '_expected_user_id',
        '_expected_device_id',
        '_expected_map_id',
        '_expected_map_revision',
        '_expected_host_boot_id',
        '_semantic_source',
        '_simulation_state_source',
        '_boottime_ns',
        '_wall_clock',
        '_boot_id_source',
        '_production_sources',
        '_seal_token',
        '__weakref__',
    )

    def __init__(
        self,
        *,
        expected_user_id: str,
        expected_device_id: str,
        expected_map_id: str,
        expected_map_revision: str,
        semantic_evidence_source: AuthenticatedHomecamSemanticResolver,
        simulation_state_source: UnixSocketGazeboSimulationStateSource,
    ) -> None:
        """Bind production sources and protected host roots once."""
        if type(semantic_evidence_source) is not (
            AuthenticatedHomecamSemanticResolver
        ):
            raise TypeError('semantic_evidence_source is invalid')
        if type(simulation_state_source) is not (
            UnixSocketGazeboSimulationStateSource
        ):
            raise TypeError('simulation_state_source is invalid')
        semantic_config = semantic_evidence_source._config
        if (
            type(semantic_config) is not HomecamSemanticConfig
            or semantic_config.agent_user_id
            != validate_user_id(expected_user_id)
            or semantic_config.device_id != expected_device_id
        ):
            raise ValueError(
                'semantic principal/device does not match admission'
            )
        self._initialize(
            expected_user_id=expected_user_id,
            expected_device_id=expected_device_id,
            expected_map_id=expected_map_id,
            expected_map_revision=expected_map_revision,
            semantic_evidence_source=semantic_evidence_source,
            simulation_state_source=simulation_state_source,
            expected_host_boot_id=_read_local_boot_id(),
            boottime_ns=_boottime_ns,
            wall_clock=time.time,
            boot_id_source=_read_local_boot_id,
            production_sources=True,
        )

    @classmethod
    def _for_test(
        cls,
        *,
        expected_user_id: str,
        expected_device_id: str,
        expected_map_id: str,
        expected_map_revision: str,
        semantic_evidence_source: GazeboSemanticEvidenceSource,
        simulation_state_source: GazeboSimulationStateSource,
        expected_host_boot_id: str,
        boottime_ns: Callable[[], int],
        wall_clock: Callable[[], float],
    ) -> 'ServerGazeboSimulationAdmissionSource':
        """Construct deterministic sources without weakening production."""
        instance = cls.__new__(cls)
        instance._initialize(
            expected_user_id=expected_user_id,
            expected_device_id=expected_device_id,
            expected_map_id=expected_map_id,
            expected_map_revision=expected_map_revision,
            semantic_evidence_source=semantic_evidence_source,
            simulation_state_source=simulation_state_source,
            expected_host_boot_id=expected_host_boot_id,
            boottime_ns=boottime_ns,
            wall_clock=wall_clock,
            boot_id_source=lambda: expected_host_boot_id,
            production_sources=False,
        )
        return instance

    def _initialize(
        self,
        *,
        expected_user_id: str,
        expected_device_id: str,
        expected_map_id: str,
        expected_map_revision: str,
        semantic_evidence_source: GazeboSemanticEvidenceSource,
        simulation_state_source: GazeboSimulationStateSource,
        expected_host_boot_id: str,
        boottime_ns: Callable[[], int],
        wall_clock: Callable[[], float],
        boot_id_source: Callable[[], str],
        production_sources: bool,
    ) -> None:
        self._expected_user_id = validate_user_id(expected_user_id)
        self._expected_device_id = _identifier(
            expected_device_id, 'expected_device_id'
        )
        self._expected_map_id = _identifier(
            expected_map_id, 'expected_map_id'
        )
        self._expected_map_revision = _identifier(
            expected_map_revision, 'expected_map_revision'
        )
        self._expected_host_boot_id = _canonical_uuid(
            expected_host_boot_id, 'expected_host_boot_id'
        )
        if not callable(
            getattr(semantic_evidence_source, 'fetch_snapshot_evidence', None)
        ):
            raise TypeError('semantic_evidence_source is invalid')
        if not callable(getattr(simulation_state_source, 'read', None)):
            raise TypeError('simulation_state_source is invalid')
        if (
            not callable(boottime_ns)
            or not callable(wall_clock)
            or not callable(boot_id_source)
            or type(production_sources) is not bool
        ):
            raise TypeError('Gazebo admission trust roots are invalid')
        if production_sources:
            if (
                simulation_state_source.expected_device_id
                != self._expected_device_id
                or simulation_state_source.expected_host_boot_id
                != self._expected_host_boot_id
            ):
                raise ValueError(
                    'Gazebo state source binding does not match admission'
                )
        self._semantic_source = semantic_evidence_source
        self._simulation_state_source = simulation_state_source
        self._boottime_ns = boottime_ns
        self._wall_clock = wall_clock
        self._boot_id_source = boot_id_source
        self._production_sources = production_sources
        self._seal_token = object()
        with _GAZEBO_ADMISSION_SEAL_LOCK:
            _GAZEBO_ADMISSION_SEALS[self] = self._seal_value()

    @property
    def expected_user_id(self) -> str:
        """Return the fixed Agent principal."""
        return self._expected_user_id

    @property
    def expected_device_id(self) -> str:
        """Return the fixed Gazebo robot/device identity."""
        return self._expected_device_id

    @property
    def expected_map_id(self) -> str:
        """Return the fixed active map identity."""
        return self._expected_map_id

    @property
    def expected_map_revision(self) -> str:
        """Return the fixed active map revision."""
        return self._expected_map_revision

    @property
    def expected_host_boot_id(self) -> str:
        """Return the fixed host boot identity."""
        return self._expected_host_boot_id

    def _seal_value(self) -> Tuple[Any, ...]:
        return (
            self._expected_user_id,
            self._expected_device_id,
            self._expected_map_id,
            self._expected_map_revision,
            self._expected_host_boot_id,
            id(self._semantic_source),
            (
                id(getattr(self._semantic_source, '_config', None))
                if self._production_sources
                else None
            ),
            id(self._simulation_state_source),
            id(self._boottime_ns),
            id(self._wall_clock),
            id(self._boot_id_source),
            self._production_sources,
            id(self._seal_token),
        )

    def _require_sealed(self) -> None:
        invalid = False
        try:
            current = _GAZEBO_ADMISSION_SEAL_VALUE(self)
            with _GAZEBO_ADMISSION_SEAL_LOCK:
                expected = _GAZEBO_ADMISSION_SEALS.get(self)
            invalid = (
                type(self) is not ServerGazeboSimulationAdmissionSource
                or expected is None
                or current != expected
            )
        except Exception:
            invalid = True
        if invalid:
            raise _error('robot_state_simulation_admission_invalid')

    def _current_boottime_ns(self) -> int:
        try:
            value = self._boottime_ns()
            return _exact_integer(value, 'boottime_ns', minimum=0)
        except Exception:
            raise _error('robot_state_clock_unavailable') from None

    def _current_wall(self) -> float:
        try:
            value = self._wall_clock()
            if type(value) not in {int, float}:
                raise ValueError('invalid wall clock')
            result = float(value)
            if not math.isfinite(result) or result < 0:
                raise ValueError('invalid wall clock')
            return result
        except Exception:
            raise _error('robot_state_clock_unavailable') from None

    def _current_boot_id(self) -> str:
        try:
            value = _canonical_uuid(
                self._boot_id_source(), 'host_boot_id'
            )
        except Exception:
            raise _error('robot_state_boot_unavailable') from None
        if value != self._expected_host_boot_id:
            raise _error('robot_state_boot_mismatch')
        return value

    def issue(
        self,
        *,
        user_id: str,
        location: str,
    ) -> GazeboSimulationAdmissionEvidence:
        """Issue one exact fixed-user, fixed-map monitor_room admission."""
        failure = None
        try:
            return _GAZEBO_ADMISSION_ISSUE_IMPL(
                self,
                user_id=user_id,
                location=location,
                requested_target=None,
            )
        except TrustedRobotStateError as error:
            failure = error
        except Exception:
            failure = _error(
                'robot_state_simulation_admission_unavailable'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        raise failure.with_traceback(None)

    def issue_for_target(
        self,
        *,
        user_id: str,
        target: TargetBinding,
    ) -> GazeboSimulationAdmissionEvidence:
        """Freshly re-admit one already confirmed canonical target."""
        failure = None
        try:
            return _GAZEBO_ADMISSION_ISSUE_IMPL(
                self,
                user_id=user_id,
                location=None,
                requested_target=target,
            )
        except TrustedRobotStateError as error:
            failure = error
        except Exception:
            failure = _error(
                'robot_state_simulation_admission_unavailable'
            )
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__suppress_context__ = True
        raise failure.with_traceback(None)

    def _issue_impl(
        self,
        *,
        user_id: str,
        location: Optional[str],
        requested_target: Optional[TargetBinding],
    ) -> GazeboSimulationAdmissionEvidence:
        _GAZEBO_ADMISSION_REQUIRE_SEALED(self)
        if validate_user_id(user_id) != self._expected_user_id:
            raise _error('robot_state_simulation_principal_mismatch')
        if (location is None) == (requested_target is None):
            raise _error('robot_state_simulation_admission_invalid')
        if location is not None and type(location) is not str:
            raise _error('robot_state_simulation_admission_invalid')
        if (
            requested_target is not None
            and type(requested_target) is not TargetBinding
        ):
            raise _error('robot_state_simulation_admission_invalid')
        _GAZEBO_ADMISSION_CURRENT_BOOT_ID(self)
        start = _GAZEBO_ADMISSION_CURRENT_BOOTTIME(self)
        wall_before = _GAZEBO_ADMISSION_CURRENT_WALL(self)
        try:
            if self._production_sources:
                semantic = _PRODUCTION_SEMANTIC_FETCH(
                    self._semantic_source
                )
            else:
                semantic = self._semantic_source.fetch_snapshot_evidence()
        except Exception:
            raise _error(
                'robot_state_simulation_semantic_unavailable'
            ) from None
        if type(semantic) is not VerifiedSemanticSnapshotEvidence:
            raise _error('robot_state_simulation_semantic_invalid')
        try:
            semantic = _SEMANTIC_CANONICAL_COPY(semantic)
            if requested_target is None:
                target = resolve_monitor_room_target(
                    semantic.snapshot,
                    location,
                    gazebo_simulation_navigation_effects(),
                )
            else:
                target = requested_target
        except Exception:
            raise _error('robot_state_simulation_semantic_invalid') from None
        wall_after = _GAZEBO_ADMISSION_CURRENT_WALL(self)
        if (
            wall_after < wall_before
            or wall_after * 1000.0 >= semantic.expires_at_ms
        ):
            raise _error('robot_state_simulation_semantic_stale')
        snapshot = semantic.snapshot
        rooms = tuple(
            room for room in snapshot.rooms
            if room.room_id == target.room_id
        )
        room_matches = (
            len(rooms) == 1
            and rooms[0].name == target.room_name
            and rooms[0].category == target.room_category
            and rooms[0].geometry_json == target.geometry_json
            and rooms[0].geometry_digest == target.geometry_digest
            and rooms[0].representative_point
            == target.representative_point
            and rooms[0].clearance_m == target.clearance_m
            and rooms[0].area_m2 == target.area_m2
        )
        if (
            snapshot.device_id != self._expected_device_id
            or snapshot.map_id != self._expected_map_id
            or snapshot.map_revision != self._expected_map_revision
            or target.device_id != self._expected_device_id
            or target.map_id != self._expected_map_id
            or target.map_revision != self._expected_map_revision
            or not target.matches_snapshot(snapshot)
            or not room_matches
            or not target.effects.gazebo_simulation_navigation
        ):
            raise _error('robot_state_simulation_binding_mismatch')
        try:
            if self._production_sources:
                robot = _PRODUCTION_SIMULATION_STATE_READ(
                    self._simulation_state_source
                )
            else:
                robot = self._simulation_state_source.read()
        except Exception:
            raise _error('robot_state_simulation_state_unavailable') from None
        if type(robot) is not GazeboSimulationStateEvidence:
            raise _error('robot_state_simulation_state_invalid')
        end = _GAZEBO_ADMISSION_CURRENT_BOOTTIME(self)
        _GAZEBO_ADMISSION_CURRENT_BOOT_ID(self)
        if end < start:
            raise _error('robot_state_clock_unavailable')
        if (
            robot.device_id != self._expected_device_id
            or robot.map_id != self._expected_map_id
            or robot.map_revision != self._expected_map_revision
            or robot.host_boot_id != self._expected_host_boot_id
        ):
            raise _error('robot_state_simulation_binding_mismatch')
        readiness = robot.require_ready(end)
        remaining_semantic_ns = int(
            (semantic.expires_at_ms - wall_after * 1000.0) * 1_000_000
        )
        if remaining_semantic_ns < 1:
            raise _error('robot_state_simulation_semantic_stale')
        valid_until = min(
            robot.valid_until_boottime_ns,
            end + remaining_semantic_ns,
        )
        if (
            valid_until <= end
            or valid_until - end > MAX_ROBOT_STATE_LIFETIME_NS
        ):
            raise _error('robot_state_simulation_admission_invalid')
        body = {
            'schema_version': GAZEBO_SIMULATION_ADMISSION_SCHEMA_VERSION,
            'scope': 'monitor_room',
            'profile': GAZEBO_SIMULATION_ADMISSION_PROFILE,
            'runtime_mode': GAZEBO_SIMULATION_RUNTIME_MODE,
            'user_id': self._expected_user_id,
            'device_id': self._expected_device_id,
            'map_id': self._expected_map_id,
            'map_revision': self._expected_map_revision,
            'host_boot_id': self._expected_host_boot_id,
            'robot_evidence_digest': robot.evidence_digest,
            'instance_id': robot.instance_id,
            'sequence': robot.sequence,
            'assembled_boottime_ns': end,
            'valid_until_boottime_ns': valid_until,
            'semantic_content_sha256': semantic.content_sha256,
            'zones_digest': snapshot.zones_digest,
            'semantic_map_generation': semantic.map_generation,
            'semantic_authorization_generation': (
                semantic.authorization_generation
            ),
            'semantic_expires_at_ms': semantic.expires_at_ms,
            'room_id': target.room_id,
            'geometry_digest': target.geometry_digest,
            'source_arguments_digest': target.source_arguments_digest,
            'target_binding_digest': target.binding_digest,
            'effects_digest': target.effects_digest,
            'navigation_available': readiness.navigation_available,
            'localization_ok': readiness.localization_ok,
            'simulation': True,
            'physical_authority': False,
            'physical_authorized': False,
            'physical_effects': False,
        }
        digest = hashlib.sha256(_canonical_json(body)).hexdigest()
        return GazeboSimulationAdmissionEvidence(
            evidence_digest=digest,
            user_id=self._expected_user_id,
            device_id=self._expected_device_id,
            map_id=self._expected_map_id,
            map_revision=self._expected_map_revision,
            host_boot_id=self._expected_host_boot_id,
            instance_id=robot.instance_id,
            sequence=robot.sequence,
            assembled_boottime_ns=end,
            valid_until_boottime_ns=valid_until,
            semantic_content_sha256=semantic.content_sha256,
            zones_digest=snapshot.zones_digest,
            semantic_map_generation=semantic.map_generation,
            semantic_authorization_generation=(
                semantic.authorization_generation
            ),
            semantic_expires_at_ms=semantic.expires_at_ms,
            room_id=target.room_id,
            geometry_digest=target.geometry_digest,
            source_arguments_digest=target.source_arguments_digest,
            target_binding_digest=target.binding_digest,
            effects_digest=target.effects_digest,
            navigation_available=readiness.navigation_available,
            localization_ok=readiness.localization_ok,
            _semantic_evidence=semantic,
            _robot_state_evidence=robot,
            _construction_token=_GAZEBO_ADMISSION_CONSTRUCTION_TOKEN,
        )


# Capture every trusted method before an instance or class can be shadowed.
_GAZEBO_ADMISSION_SEAL_VALUE = (
    ServerGazeboSimulationAdmissionSource._seal_value
)
_GAZEBO_ADMISSION_REQUIRE_SEALED = (
    ServerGazeboSimulationAdmissionSource._require_sealed
)
_GAZEBO_ADMISSION_CURRENT_BOOTTIME = (
    ServerGazeboSimulationAdmissionSource._current_boottime_ns
)
_GAZEBO_ADMISSION_CURRENT_WALL = (
    ServerGazeboSimulationAdmissionSource._current_wall
)
_GAZEBO_ADMISSION_CURRENT_BOOT_ID = (
    ServerGazeboSimulationAdmissionSource._current_boot_id
)
_GAZEBO_ADMISSION_ISSUE_IMPL = (
    ServerGazeboSimulationAdmissionSource._issue_impl
)
_PRODUCTION_SEMANTIC_FETCH = (
    AuthenticatedHomecamSemanticResolver.fetch_snapshot_evidence
)
_SEMANTIC_CANONICAL_COPY = (
    VerifiedSemanticSnapshotEvidence.canonical_copy
)
_PRODUCTION_SIMULATION_STATE_READ = (
    UnixSocketGazeboSimulationStateSource.read
)


__all__ = [
    'DEFAULT_ROBOT_STATE_TIMEOUT_SECONDS',
    'MAX_ROBOT_STATE_FRAME_BYTES',
    'MAX_ROBOT_STATE_LIFETIME_NS',
    'RobotStateFieldEvidence',
    'GazeboSimulationAdmissionEvidence',
    'GazeboSimulationReadiness',
    'GazeboSimulationStateEvidence',
    'GazeboSimulationStateSource',
    'ServerGazeboSimulationAdmissionSource',
    'TrustedRobotStateError',
    'TrustedRobotStateEvidence',
    'TrustedRobotStateSource',
    'UnixSocketTrustedRobotStateSource',
    'UnixSocketGazeboSimulationStateSource',
    'parse_gazebo_simulation_state_envelope',
    'parse_trusted_robot_state_envelope',
    'trusted_boottime_ns',
]
