"""Strict, side-effect-free contracts for a room monitoring mission.

This module deliberately contains no ROS, camera, network, or filesystem
adapter.  It validates a server-provided semantic map, binds one trusted
``monitor_room`` decision to one server-authenticated confirmation, and
drives an injected simulation adapter through a bounded state machine.

The execution ledger is intentionally process-local.  Until a durable
controller owns replay protection, this module rejects every adapter that
can cause physical effects and never claims that a real viewer is live.
Its single-active lease is scoped to one controller instance, not a process
or robot.  A future physical controller requires a shared device-scoped
durable lease.
Tombstones are retained up to a hard per-controller record cap; restart or
an authenticated future administrative cleanup is required to reclaim them.
Battery policy, forbidden-room policy, recording/P2P state, and physical
device identity remain requirements of that future durable controller;
this simulation milestone does not claim to enforce them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from malbut_agent_server.orchestrator import OrchestrationResult
from malbut_agent_server.schemas import validate_user_id


MAX_MAP_BYTES = 2 * 1024 * 1024
MAX_ROOMS = 128
MAX_RING_POINTS = 4096
MAX_COVERAGE_VIEWPOINTS = 32
MAX_ACTION_TTL_SECONDS = 10.0
MAX_CLOCK_SKEW_SECONDS = 1.0

MISSION_STATUSES = frozenset({
    'proposed',
    'confirmed',
    'running',
    'succeeded',
    'failed',
    'cancelled',
    'timed_out',
    'rejected',
})
MISSION_PHASES = frozenset({
    'proposal',
    'confirmation',
    'preflight',
    'navigating',
    'coverage',
    'live_ready',
    'terminal',
})
ADAPTER_STATUSES = frozenset({'succeeded', 'failed', 'timed_out'})


class RoomMissionValidationError(ValueError):
    """Report invalid trusted configuration without reflecting content."""


@dataclass(frozen=True)
class RoomPose:
    """One finite map-frame pose selected by the semantic-map owner."""

    x: float
    y: float
    yaw: float

    @classmethod
    def from_dict(cls, value: Any) -> 'RoomPose':
        """Build one strict pose from an exact three-field object."""
        if type(value) is not dict or set(value) != {'x', 'y', 'yaw'}:
            raise RoomMissionValidationError('room pose is invalid')
        coordinates = []
        for name in ('x', 'y', 'yaw'):
            item = value[name]
            if (
                type(item) not in {int, float}
                or not math.isfinite(float(item))
            ):
                raise RoomMissionValidationError('room pose is invalid')
            coordinates.append(float(item))
        x, y, yaw = coordinates
        if abs(x) > 10000 or abs(y) > 10000 or not -math.pi <= yaw <= math.pi:
            raise RoomMissionValidationError('room pose is out of range')
        return cls(x=x, y=y, yaw=yaw)

    def to_dict(self) -> Dict[str, float]:
        """Return the bounded pose object consumed by an adapter."""
        return {'x': self.x, 'y': self.y, 'yaw': self.yaw}


@dataclass(frozen=True)
class SemanticRoom:
    """One unambiguous named Room and its explicit mission poses."""

    room_id: str
    name: str
    category: str
    aliases: Tuple[str, ...]
    navigation_goal: RoomPose
    coverage_viewpoints: Tuple[RoomPose, ...]


@dataclass(frozen=True)
class RoomMissionPlan:
    """Immutable high-level plan constructed only by the resolver."""

    map_id: str
    map_revision: str
    room_id: str
    navigation_goal: RoomPose
    coverage_viewpoints: Tuple[RoomPose, ...]


class SemanticRoomResolver:
    """Resolve unique names from one fixed, already-provided User Map."""

    _PUBLIC_CONFIGURATION_NAMES = frozenset({'map_id', 'map_revision'})
    _PRIVATE_CONFIGURATION_NAMES = frozenset({
        '_map_id',
        '_map_revision',
        '_rooms',
        '_aliases',
    })

    def __setattr__(self, name: str, value: Any) -> None:
        """Keep validated map identity and indexes immutable."""
        if name in self._PUBLIC_CONFIGURATION_NAMES:
            raise AttributeError('semantic map configuration is immutable')
        if (
            name in self._PRIVATE_CONFIGURATION_NAMES
            and name in self.__dict__
        ):
            raise AttributeError('semantic map configuration is immutable')
        object.__setattr__(self, name, value)

    def __init__(self, user_map: Any, *, expected_map_id: str) -> None:
        """Validate and snapshot one map without reading any local file."""
        expected = _identifier(expected_map_id, 'expected_map_id')
        snapshot = _json_snapshot(user_map)
        if snapshot.get('type') != 'FeatureCollection':
            raise RoomMissionValidationError('semantic map type is invalid')
        if snapshot.get('frame_id') != 'map':
            raise RoomMissionValidationError('semantic map frame is invalid')
        if snapshot.get('map_id') != expected:
            raise RoomMissionValidationError('semantic map identity differs')
        features = snapshot.get('features')
        if type(features) is not list:
            raise RoomMissionValidationError(
                'semantic map features are invalid'
            )
        room_features = [
            feature
            for feature in features
            if type(feature) is dict
            and type(feature.get('properties')) is dict
            and feature['properties'].get('role') == 'room'
        ]
        if not 1 <= len(room_features) <= MAX_ROOMS:
            raise RoomMissionValidationError('semantic room count is invalid')

        rooms: Dict[str, SemanticRoom] = {}
        aliases: Dict[str, str] = {}
        for feature in room_features:
            room = self._room_from_feature(feature)
            if room.room_id in rooms:
                raise RoomMissionValidationError('room identity is duplicated')
            rooms[room.room_id] = room
            for label in (room.name, room.category, *room.aliases):
                normalized = _normalize_alias(label)
                owner = aliases.get(normalized)
                if owner is not None and owner != room.room_id:
                    raise RoomMissionValidationError('room alias is ambiguous')
                aliases[normalized] = room.room_id

        self._map_id = expected
        self._map_revision = _sha256_json(snapshot)
        self._rooms = MappingProxyType(rooms)
        self._aliases = MappingProxyType(aliases)

    @property
    def map_id(self) -> str:
        """Return the immutable configured map identity."""
        return self._map_id

    @property
    def map_revision(self) -> str:
        """Return the immutable canonical map revision."""
        return self._map_revision

    @staticmethod
    def _room_from_feature(feature: Dict[str, Any]) -> SemanticRoom:
        """Validate one Room Feature and every explicitly supplied pose."""
        if feature.get('type') != 'Feature':
            raise RoomMissionValidationError('room feature type is invalid')
        properties = feature['properties']
        room_id = _identifier(
            properties.get('room_id', feature.get('id')),
            'room_id',
        )
        if feature.get('id') is not None and feature.get('id') != room_id:
            raise RoomMissionValidationError('room feature identity differs')
        name = _label(properties.get('name'))
        category = _label(properties.get('category'))
        raw_aliases = properties.get('aliases', [])
        if (
            type(raw_aliases) is not list
            or len(raw_aliases) > 32
        ):
            raise RoomMissionValidationError('room aliases are invalid')
        alias_values = tuple(_label(value) for value in raw_aliases)
        labels = (name, category, *alias_values)
        if len({_normalize_alias(value) for value in labels}) != len(labels):
            raise RoomMissionValidationError('room labels are duplicated')

        geometry = _validate_geometry(feature.get('geometry'))
        navigation_goal = RoomPose.from_dict(
            properties.get('navigation_goal')
        )
        raw_viewpoints = properties.get('coverage_viewpoints')
        if (
            type(raw_viewpoints) is not list
            or not 1 <= len(raw_viewpoints) <= MAX_COVERAGE_VIEWPOINTS
        ):
            raise RoomMissionValidationError(
                'coverage viewpoints are required'
            )
        viewpoints = tuple(
            RoomPose.from_dict(value) for value in raw_viewpoints
        )
        if len(set(viewpoints)) != len(viewpoints):
            raise RoomMissionValidationError(
                'coverage viewpoints are duplicated'
            )
        for pose in (navigation_goal, *viewpoints):
            if not _geometry_strictly_contains(geometry, pose.x, pose.y):
                raise RoomMissionValidationError('room pose is outside Room')

        return SemanticRoom(
            room_id=room_id,
            name=name,
            category=category,
            aliases=alias_values,
            navigation_goal=navigation_goal,
            coverage_viewpoints=viewpoints,
        )

    def resolve(self, location: Any) -> SemanticRoom:
        """Resolve one unique map-owned name, category, or alias."""
        normalized = _normalize_alias(_label(location))
        room_id = self._aliases.get(normalized)
        if room_id is None:
            raise RoomMissionValidationError('semantic room is unavailable')
        return self._rooms[room_id]

    def plan(self, location: Any) -> RoomMissionPlan:
        """Construct the immutable server-owned plan for one room."""
        room = self.resolve(location)
        return RoomMissionPlan(
            map_id=self.map_id,
            map_revision=self.map_revision,
            room_id=room.room_id,
            navigation_goal=room.navigation_goal,
            coverage_viewpoints=room.coverage_viewpoints,
        )


@dataclass(frozen=True)
class TrustedMissionState:
    """Fresh local state supplied by a trusted runtime, never a model."""

    observed_at: float
    map_id: str
    map_revision: str
    navigation_available: bool
    localization_ok: bool
    camera_available: bool
    stream_available: bool
    privacy_mode: bool
    emergency_stop: bool = False

    def __post_init__(self) -> None:
        """Reject imprecise or caller-shaped state objects."""
        if (
            type(self.observed_at) not in {int, float}
            or not math.isfinite(float(self.observed_at))
        ):
            raise RoomMissionValidationError('trusted state time is invalid')
        _identifier(self.map_id, 'map_id')
        _identifier(self.map_revision, 'map_revision')
        for name in (
            'navigation_available',
            'localization_ok',
            'camera_available',
            'stream_available',
            'privacy_mode',
            'emergency_stop',
        ):
            if type(getattr(self, name)) is not bool:
                raise RoomMissionValidationError('trusted state is invalid')


@dataclass(frozen=True)
class MissionFeedback:
    """Content-free, explicitly simulated public mission state."""

    status: str
    phase: str
    code: str
    sequence: int
    tool_call_id: Optional[str] = None
    runtime_mode: str = 'simulation'
    simulated: bool = True
    physical_effects: bool = False
    viewer_live: bool = False
    durability: str = 'process_local'
    lease_scope: str = 'controller_instance'

    def __post_init__(self) -> None:
        """Keep feedback finite, honest, and free of adapter content."""
        if self.status not in MISSION_STATUSES:
            raise RoomMissionValidationError('mission status is invalid')
        if self.phase not in MISSION_PHASES:
            raise RoomMissionValidationError('mission phase is invalid')
        if (
            type(self.code) is not str
            or not 1 <= len(self.code) <= 64
            or not self.code.isascii()
            or not self.code.replace('_', '').isalnum()
        ):
            raise RoomMissionValidationError('mission code is invalid')
        if type(self.sequence) is not int or self.sequence < 0:
            raise RoomMissionValidationError('mission sequence is invalid')
        if (
            self.runtime_mode != 'simulation'
            or self.simulated is not True
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.durability != 'process_local'
            or self.lease_scope != 'controller_instance'
        ):
            raise RoomMissionValidationError('simulation marker is invalid')
        if self.tool_call_id is not None:
            _identifier(self.tool_call_id, 'tool_call_id')

    def to_dict(self) -> Dict[str, Any]:
        """Return feedback without names, arguments, positions, or text."""
        return {
            'status': self.status,
            'phase': self.phase,
            'code': self.code,
            'sequence': self.sequence,
            'tool_call_id': self.tool_call_id,
            'runtime_mode': self.runtime_mode,
            'simulated': self.simulated,
            'physical_effects': self.physical_effects,
            'viewer_live': self.viewer_live,
            'durability': self.durability,
            'lease_scope': self.lease_scope,
        }


@dataclass(frozen=True, repr=False)
class MissionAuthority:
    """Principal plus committed decision digest from a trusted resolver."""

    subject_id: str
    session_id: str
    request_id: str
    conversation_id: str
    turn_id: str
    conversation_generation: int
    conversation_revision: int
    conversation_ordinal: int
    decision_digest: str

    def __post_init__(self) -> None:
        """Validate the server-side identity and conversation binding."""
        if validate_user_id(self.subject_id) != self.subject_id:
            raise RoomMissionValidationError('authority subject is invalid')
        for name in (
            'session_id',
            'request_id',
            'conversation_id',
            'turn_id',
        ):
            _identifier(getattr(self, name), name)
        for name in (
            'conversation_generation',
            'conversation_revision',
            'conversation_ordinal',
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise RoomMissionValidationError(
                    'authority conversation state is invalid'
                )
        _sha256_digest(self.decision_digest, 'authority decision digest')

    def __repr__(self) -> str:
        """Avoid reflecting principal or conversation data."""
        return '<MissionAuthority trusted>'


@dataclass(frozen=True, repr=False)
class TrustedConfirmation:
    """Affirmative event resolved from a trusted server-side issuer."""

    confirmation_id: str
    authority: MissionAuthority
    decision_id: str
    arguments_digest: str
    issued_at: float
    expires_at: float
    decision_expires_at: float

    def __post_init__(self) -> None:
        """Validate the immutable confirmation envelope."""
        _identifier(self.confirmation_id, 'confirmation_id')
        if type(self.authority) is not MissionAuthority:
            raise RoomMissionValidationError(
                'confirmation authority is invalid'
            )
        _identifier(self.decision_id, 'decision_id')
        if (
            type(self.arguments_digest) is not str
            or len(self.arguments_digest) != 64
            or any(
                character not in '0123456789abcdef'
                for character in self.arguments_digest
            )
        ):
            raise RoomMissionValidationError(
                'confirmation arguments are invalid'
            )
        for value in (
            self.issued_at,
            self.expires_at,
            self.decision_expires_at,
        ):
            if (
                type(value) not in {int, float}
                or not math.isfinite(float(value))
            ):
                raise RoomMissionValidationError(
                    'confirmation time is invalid'
                )

    def __repr__(self) -> str:
        """Avoid reflecting the authority or bound command."""
        return '<TrustedConfirmation opaque>'


@dataclass(frozen=True, repr=False, eq=False)
class MissionProposalHandle:
    """Opaque, identity-bound capability for one proposal."""

    proposal_id: str

    def __post_init__(self) -> None:
        """Validate only the opaque server identifier."""
        _identifier(self.proposal_id, 'proposal_id')

    def __repr__(self) -> str:
        """Hide capability details from logs."""
        return '<MissionProposalHandle opaque>'

    def to_dict(self) -> Dict[str, str]:
        """Serialize only the opaque identifier, never the mission plan."""
        return {'proposal_id': self.proposal_id}


@dataclass(frozen=True, repr=False)
class ProposalResult:
    """Content-free public result containing only an opaque capability."""

    feedback: MissionFeedback
    proposal: Optional[MissionProposalHandle] = None

    def __repr__(self) -> str:
        """Avoid recursively exposing internal mission configuration."""
        state = '<opaque>' if self.proposal is not None else 'None'
        return f'ProposalResult(feedback={self.feedback!r}, proposal={state})'

    def to_dict(self) -> Dict[str, Any]:
        """Return content-free feedback and an optional opaque handle."""
        return {
            'feedback': self.feedback.to_dict(),
            'proposal': (
                self.proposal.to_dict()
                if self.proposal is not None
                else None
            ),
        }


@dataclass(frozen=True)
class AdapterStepResult:
    """One bounded adapter outcome without diagnostic or user text."""

    status: str

    def __post_init__(self) -> None:
        """Restrict adapter output to terminal step states."""
        if self.status not in ADAPTER_STATUSES:
            raise RoomMissionValidationError('adapter status is invalid')


@dataclass(frozen=True)
class MissionExecutionContext:
    """Immutable identifier passed to every idempotent adapter phase."""

    tool_call_id: str
    proposal_id: str

    def __post_init__(self) -> None:
        """Validate both server-generated identifiers."""
        _identifier(self.tool_call_id, 'tool_call_id')
        _identifier(self.proposal_id, 'proposal_id')


class RoomMissionAdapter(Protocol):
    """Injected simulation boundary; physical adapters are rejected."""

    @property
    def physical_effects(self) -> bool:
        """Return whether this adapter can affect a physical runtime."""

    @property
    def runtime_mode(self) -> str:
        """Return the explicit adapter runtime mode."""

    @property
    def simulated(self) -> bool:
        """Return whether outcomes are simulations."""

    def preflight(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
        state: TrustedMissionState,
    ) -> AdapterStepResult:
        """Validate executor availability without starting motion."""

    def navigate(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
    ) -> AdapterStepResult:
        """Reach the explicit room navigation goal."""

    def cover(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
    ) -> AdapterStepResult:
        """Visit the server-owned coverage viewpoints."""

    def wait_live_ready(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
        timeout_seconds: float,
    ) -> AdapterStepResult:
        """Return success only after bounded live-stream readiness."""

    def cancel(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
    ) -> AdapterStepResult:
        """Request cancellation without inventing successful completion."""


@dataclass(frozen=True)
class SimulationPhaseGate:
    """Safe test-only gate for one built-in simulation phase."""

    phase: str
    started: threading.Event
    release: threading.Event

    def __post_init__(self) -> None:
        """Restrict gates to known phases and exact thread events."""
        phases = {
            'preflight',
            'navigating',
            'coverage',
            'live_ready',
            'cancel',
        }
        if self.phase not in phases:
            raise RoomMissionValidationError('simulation gate is invalid')
        if (
            type(self.started) is not threading.Event
            or type(self.release) is not threading.Event
        ):
            raise RoomMissionValidationError('simulation gate is invalid')


class SimulationRoomMissionAdapter:
    """Deterministic honest fake that never performs a physical effect."""

    def __init__(
        self,
        *,
        fail_phase: Optional[str] = None,
        timeout_phase: Optional[str] = None,
        phase_gates: Tuple[SimulationPhaseGate, ...] = (),
    ) -> None:
        """Configure at most one content-free failure or timeout."""
        phases = {'preflight', 'navigating', 'coverage', 'live_ready'}
        if fail_phase is not None and fail_phase not in phases:
            raise RoomMissionValidationError('fake failure phase is invalid')
        if timeout_phase is not None and timeout_phase not in phases:
            raise RoomMissionValidationError('fake timeout phase is invalid')
        if fail_phase is not None and timeout_phase is not None:
            raise RoomMissionValidationError('fake outcomes are ambiguous')
        if (
            type(phase_gates) is not tuple
            or any(
                type(gate) is not SimulationPhaseGate
                for gate in phase_gates
            )
            or len({gate.phase for gate in phase_gates}) != len(phase_gates)
        ):
            raise RoomMissionValidationError('simulation gates are invalid')
        self._fail_phase = fail_phase
        self._timeout_phase = timeout_phase
        self._phase_gates = phase_gates
        self._calls = []
        self._calls_lock = threading.Lock()

    @property
    def physical_effects(self) -> bool:
        """Identify this adapter as strictly non-physical."""
        return False

    @property
    def runtime_mode(self) -> str:
        """Identify the adapter as simulation-only."""
        return 'simulation'

    @property
    def simulated(self) -> bool:
        """State that no outcome is a physical observation."""
        return True

    @property
    def calls(self) -> Tuple[Tuple[str, str], ...]:
        """Expose opaque Tool IDs and phase names for offline assertions."""
        with self._calls_lock:
            return tuple(self._calls)

    def _step(
        self,
        context: MissionExecutionContext,
        phase: str,
    ) -> AdapterStepResult:
        if type(context) is not MissionExecutionContext:
            return AdapterStepResult('failed')
        with self._calls_lock:
            self._calls.append((context.tool_call_id, phase))
        for gate in self._phase_gates:
            if gate.phase == phase:
                gate.started.set()
                gate.release.wait()
                break
        if phase == self._fail_phase:
            return AdapterStepResult('failed')
        if phase == self._timeout_phase:
            return AdapterStepResult('timed_out')
        return AdapterStepResult('succeeded')

    def preflight(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
        state: TrustedMissionState,
    ) -> AdapterStepResult:
        """Acknowledge a validated preflight without I/O."""
        del plan, state
        return self._step(context, 'preflight')

    def navigate(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
    ) -> AdapterStepResult:
        """Simulate navigation without publishing a Nav2 goal."""
        del plan
        return self._step(context, 'navigating')

    def cover(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
    ) -> AdapterStepResult:
        """Simulate coverage without opening a camera."""
        del plan
        return self._step(context, 'coverage')

    def wait_live_ready(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
        timeout_seconds: float,
    ) -> AdapterStepResult:
        """Simulate readiness without contacting a stream backend."""
        del plan, timeout_seconds
        return self._step(context, 'live_ready')

    def cancel(
        self,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
    ) -> AdapterStepResult:
        """Record a simulated cancellation request."""
        del plan
        return self._step(context, 'cancel')


@dataclass
class _ProposalRecord:
    proposal_id: str
    handle: MissionProposalHandle
    authority: MissionAuthority
    decision_id: str
    arguments_digest: str
    issued_at: float
    expires_at: float
    monotonic_issued_at: float
    monotonic_deadline: float
    plan: RoomMissionPlan
    state: str = 'proposed'
    tool_call_id: Optional[str] = None


@dataclass
class _ExecutionRecord:
    proposal: _ProposalRecord
    tool_call_id: str
    monotonic_deadline: float
    status: str = 'confirmed'
    phase: str = 'confirmation'
    code: str = 'mission_confirmed'
    sequence: int = 1
    cancel_requested: bool = False
    phase_call_inflight: bool = False


class RoomMonitoringMission:
    """Execute one owner-bound, simulation-only room mission at a time."""

    _PUBLIC_CONFIGURATION_NAMES = frozenset({
        'adapter',
        'resolver',
        'max_state_age_seconds',
        'adapter_timeout_seconds',
        'stream_timeout_seconds',
        'cancellation_timeout_seconds',
        'max_mission_records',
    })
    _PRIVATE_CONFIGURATION_NAMES = frozenset({
        '_resolver',
        '_resolver_plan',
        '_adapter_preflight',
        '_adapter_navigate',
        '_adapter_cover',
        '_adapter_wait_live_ready',
        '_adapter_cancel',
        '_authority_resolver',
        '_authority_validator',
        '_confirmation_resolver',
        '_state_resolver',
        '_state_validator',
        '_clock',
        '_monotonic_clock',
        '_id_factory',
        '_max_state_age_seconds',
        '_adapter_timeout_seconds',
        '_stream_timeout_seconds',
        '_cancellation_timeout_seconds',
        '_max_mission_records',
    })

    def __setattr__(self, name: str, value: Any) -> None:
        """Reject public runtime replacement of validated configuration."""
        if name in self._PUBLIC_CONFIGURATION_NAMES or name == '_adapter':
            raise AttributeError('mission configuration is immutable')
        if (
            name in self._PRIVATE_CONFIGURATION_NAMES
            and name in self.__dict__
        ):
            raise AttributeError('mission configuration is immutable')
        object.__setattr__(self, name, value)

    def __init__(
        self,
        resolver: SemanticRoomResolver,
        adapter: RoomMissionAdapter,
        *,
        authority_resolver: Callable[
            [OrchestrationResult], MissionAuthority
        ],
        authority_validator: Callable[[MissionAuthority], bool],
        confirmation_resolver: Callable[[str], TrustedConfirmation],
        state_resolver: Callable[
            [MissionAuthority, RoomMissionPlan], TrustedMissionState
        ],
        state_validator: Callable[
            [TrustedMissionState, MissionAuthority, RoomMissionPlan], bool
        ],
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        max_state_age_seconds: float = 2.0,
        adapter_timeout_seconds: float = 10.0,
        stream_timeout_seconds: float = 10.0,
        cancellation_timeout_seconds: float = 2.0,
        max_mission_records: int = 256,
    ) -> None:
        """Create a process-local controller from trusted server hooks."""
        if type(resolver) is not SemanticRoomResolver:
            raise RoomMissionValidationError('resolver is invalid')
        dependencies = (
            authority_resolver,
            authority_validator,
            confirmation_resolver,
            state_resolver,
            state_validator,
            clock,
            monotonic_clock,
            id_factory,
        )
        if not all(callable(value) for value in dependencies):
            raise RoomMissionValidationError('mission dependency is invalid')
        if type(adapter) is not SimulationRoomMissionAdapter:
            raise RoomMissionValidationError(
                'only the built-in simulation adapter is allowed'
            )
        for value in (
            max_state_age_seconds,
            adapter_timeout_seconds,
            stream_timeout_seconds,
            cancellation_timeout_seconds,
        ):
            self._positive_number(value)
        if (
            type(max_mission_records) is not int
            or not 1 <= max_mission_records <= 4096
        ):
            raise RoomMissionValidationError(
                'mission record capacity is invalid'
            )
        self._resolver = resolver
        self._resolver_plan = resolver.plan
        self._adapter_preflight = adapter.preflight
        self._adapter_navigate = adapter.navigate
        self._adapter_cover = adapter.cover
        self._adapter_wait_live_ready = adapter.wait_live_ready
        self._adapter_cancel = adapter.cancel
        self._authority_resolver = authority_resolver
        self._authority_validator = authority_validator
        self._confirmation_resolver = confirmation_resolver
        self._state_resolver = state_resolver
        self._state_validator = state_validator
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._id_factory = id_factory
        self._max_state_age_seconds = float(max_state_age_seconds)
        self._adapter_timeout_seconds = float(adapter_timeout_seconds)
        self._stream_timeout_seconds = float(stream_timeout_seconds)
        self._cancellation_timeout_seconds = float(
            cancellation_timeout_seconds
        )
        self._max_mission_records = max_mission_records
        self._proposals: Dict[str, _ProposalRecord] = {}
        self._decision_ids: Dict[str, str] = {}
        self._confirmation_ids: Dict[str, str] = {}
        self._executions: Dict[str, _ExecutionRecord] = {}
        self._active_tool_call_id: Optional[str] = None
        self._adapter_poisoned = False
        self._lock = threading.RLock()

    @staticmethod
    def _positive_number(value: Any) -> float:
        if (
            type(value) not in {int, float}
            or not math.isfinite(float(value))
            or float(value) <= 0
            or float(value) > 120
        ):
            raise RoomMissionValidationError('mission timeout is invalid')
        return float(value)

    def propose(
        self,
        result: OrchestrationResult,
    ) -> ProposalResult:
        """Accept a decision only with server-resolved current authority."""
        if type(result) is not OrchestrationResult:
            raise RoomMissionValidationError('orchestration result is invalid')
        try:
            snapshot = _orchestration_payload(result)
            snapshot_digest = _sha256_json(snapshot)
        except RoomMissionValidationError:
            return ProposalResult(self._feedback(
                'rejected', 'proposal', 'untrusted_proposal', 0, None
            ))
        now, monotonic_now = self._time_snapshot()
        try:
            decision = snapshot['decision']
            numeric_times = (
                snapshot['issued_at'],
                snapshot['expires_at'],
            )
            valid = (
                all(
                    type(value) in {int, float}
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in numeric_times
                )
                and type(decision['expires_in_ms']) is int
                and 1 <= decision['expires_in_ms'] <= 10000
                and all(
                    type(value) is int and value >= 0
                    for value in (
                        snapshot['conversation_generation'],
                        snapshot['conversation_revision'],
                        snapshot['conversation_ordinal'],
                    )
                )
                and snapshot['state_trusted'] is True
                and snapshot['safety']['allowed'] is True
                and snapshot['safety']['code'] == 'allowed'
                and decision['type'] == 'tool_call'
                and decision['tool_name'] == 'monitor_room'
                and snapshot['raw_decision'] == decision
                and snapshot['issued_at']
                <= now + MAX_CLOCK_SKEW_SECONDS
                and snapshot['issued_at'] < snapshot['expires_at']
                and now < snapshot['expires_at']
                and snapshot['expires_at'] - snapshot['issued_at']
                <= MAX_ACTION_TTL_SECONDS + 1e-9
                and snapshot['expires_at']
                <= (
                    snapshot['issued_at']
                    + decision['expires_in_ms'] / 1000.0
                    + 1e-9
                )
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            return ProposalResult(self._feedback(
                'rejected', 'proposal', 'untrusted_proposal', 0, None
            ))
        try:
            authority = self._authority_resolver(result)
        except Exception:
            authority = None
        try:
            live_unchanged = (
                orchestration_authority_digest(result) == snapshot_digest
            )
        except RoomMissionValidationError:
            live_unchanged = False
        if (
            type(authority) is not MissionAuthority
            or not self._authority_matches_snapshot(authority, snapshot)
            or not self._authority_is_current(authority)
            or not live_unchanged
        ):
            return ProposalResult(self._feedback(
                'rejected', 'proposal', 'authority_unavailable', 0, None
            ))
        try:
            arguments = _monitor_room_arguments(decision['arguments'])
            plan = self._resolver_plan(arguments['location'])
        except RoomMissionValidationError:
            return ProposalResult(self._feedback(
                'rejected', 'proposal', 'room_unavailable', 0, None
            ))
        try:
            decision_id = _identifier(
                snapshot['decision_id'], 'decision_id'
            )
        except RoomMissionValidationError:
            return ProposalResult(self._feedback(
                'rejected', 'proposal', 'untrusted_proposal', 0, None
            ))
        deadline = monotonic_now + float(snapshot['expires_at']) - now
        final_now, final_monotonic = self._time_snapshot()
        try:
            live_unchanged = (
                orchestration_authority_digest(result) == snapshot_digest
            )
        except RoomMissionValidationError:
            live_unchanged = False
        if (
            final_now
            < float(snapshot['issued_at']) - MAX_CLOCK_SKEW_SECONDS
            or final_now >= float(snapshot['expires_at'])
            or final_monotonic < monotonic_now
            or final_monotonic >= deadline
            or not self._authority_is_current(authority)
            or not live_unchanged
        ):
            return ProposalResult(self._feedback(
                'rejected', 'proposal', 'untrusted_proposal', 0, None
            ))
        arguments_digest = _sha256_json(arguments)
        for _ in range(3):
            proposal_id = self._new_id('room-proposal')
            commit_now, commit_monotonic = self._time_snapshot()
            try:
                live_unchanged = (
                    orchestration_authority_digest(result)
                    == snapshot_digest
                )
            except RoomMissionValidationError:
                live_unchanged = False
            if (
                commit_now
                < float(snapshot['issued_at']) - MAX_CLOCK_SKEW_SECONDS
                or commit_now >= float(snapshot['expires_at'])
                or commit_monotonic < monotonic_now
                or commit_monotonic >= deadline
                or not self._authority_is_current(authority)
                or not live_unchanged
            ):
                return ProposalResult(self._feedback(
                    'rejected', 'proposal', 'untrusted_proposal', 0, None
                ))
            handle = MissionProposalHandle(proposal_id)
            proposal = _ProposalRecord(
                proposal_id=proposal_id,
                handle=handle,
                authority=authority,
                decision_id=decision_id,
                arguments_digest=arguments_digest,
                issued_at=float(snapshot['issued_at']),
                expires_at=float(snapshot['expires_at']),
                monotonic_issued_at=monotonic_now,
                monotonic_deadline=deadline,
                plan=plan,
            )
            with self._lock:
                if decision_id in self._decision_ids:
                    return ProposalResult(self._feedback(
                        'rejected', 'proposal', 'decision_replay', 0, None
                    ))
                if len(self._proposals) >= self._max_mission_records:
                    return ProposalResult(self._feedback(
                        'rejected',
                        'proposal',
                        'mission_capacity_reached',
                        0,
                        None,
                    ))
                if proposal_id in self._proposals:
                    continue
                self._proposals[proposal_id] = proposal
                self._decision_ids[decision_id] = proposal_id
            return ProposalResult(
                self._feedback(
                    'proposed', 'proposal', 'mission_proposed', 0, None
                ),
                handle,
            )
        raise RoomMissionValidationError('server ID collision')

    def confirm(
        self,
        proposal: MissionProposalHandle,
        confirmation_id: str,
    ) -> MissionFeedback:
        """Consume one trusted affirmative event and mint one Tool ID."""
        record, error = self._authorized_proposal(proposal)
        if record is None:
            return self._feedback(
                'rejected', 'confirmation', error, 0, None
            )
        with self._lock:
            if record.state != 'proposed':
                return self._feedback(
                    'rejected',
                    'confirmation',
                    'confirmation_replay',
                    0,
                    record.tool_call_id,
                )
        try:
            confirmation_key = _identifier(
                confirmation_id, 'confirmation_id'
            )
        except RoomMissionValidationError:
            return self._feedback(
                'rejected', 'confirmation', 'confirmation_invalid', 0, None
            )
        now, monotonic_now = self._time_snapshot()
        with self._lock:
            expiration = self._expiration_error(
                record, now, monotonic_now
            )
            if expiration is not None:
                record.state = 'timed_out'
                return self._feedback(
                    'timed_out',
                    'confirmation',
                    expiration,
                    0,
                    None,
                )
        try:
            evidence = self._confirmation_resolver(confirmation_key)
        except Exception:
            evidence = None
        now, monotonic_now = self._time_snapshot()
        if (
            type(evidence) is not TrustedConfirmation
            or not self._confirmation_matches(
                record, evidence, confirmation_key, now
            )
        ):
            return self._feedback(
                'rejected', 'confirmation', 'confirmation_invalid', 0, None
            )
        if not self._authority_is_current(record.authority):
            return self._feedback(
                'rejected', 'confirmation', 'authority_revoked', 0, None
            )
        for _ in range(3):
            tool_call_id = self._new_id('room-tool-call')
            now, monotonic_now = self._time_snapshot()
            authority_current = self._authority_is_current(
                record.authority
            )
            with self._lock:
                if record.state != 'proposed':
                    return self._feedback(
                        'rejected',
                        'confirmation',
                        'confirmation_replay',
                        0,
                        record.tool_call_id,
                    )
                expiration = self._expiration_error(
                    record, now, monotonic_now
                )
                if expiration is not None:
                    record.state = 'timed_out'
                    return self._feedback(
                        'timed_out',
                        'confirmation',
                        expiration,
                        0,
                        None,
                    )
                if now >= evidence.expires_at:
                    record.state = 'timed_out'
                    return self._feedback(
                        'timed_out',
                        'confirmation',
                        'confirmation_expired',
                        0,
                        None,
                    )
                if not authority_current:
                    return self._feedback(
                        'rejected',
                        'confirmation',
                        'authority_revoked',
                        0,
                        None,
                    )
                if confirmation_key in self._confirmation_ids:
                    return self._feedback(
                        'rejected',
                        'confirmation',
                        'confirmation_replay',
                        0,
                        None,
                    )
                if tool_call_id in self._executions:
                    continue
                record.state = 'confirmed'
                record.tool_call_id = tool_call_id
                remaining = max(
                    0.0, float(evidence.expires_at) - now
                )
                self._executions[tool_call_id] = _ExecutionRecord(
                    proposal=record,
                    tool_call_id=tool_call_id,
                    monotonic_deadline=min(
                        record.monotonic_deadline,
                        monotonic_now + remaining,
                    ),
                )
                self._confirmation_ids[confirmation_key] = (
                    proposal.proposal_id
                )
                return self._feedback(
                    'confirmed',
                    'confirmation',
                    'mission_confirmed',
                    1,
                    tool_call_id,
                )
        raise RoomMissionValidationError('server ID collision')

    def deny(self, proposal: MissionProposalHandle) -> MissionFeedback:
        """Terminally consume a proposal after authenticated denial."""
        record, error = self._authorized_proposal(proposal)
        if record is None:
            return self._feedback(
                'rejected', 'confirmation', error, 0, None
            )
        now, monotonic_now = self._time_snapshot()
        with self._lock:
            if record.state != 'proposed':
                return self._feedback(
                    'rejected',
                    'confirmation',
                    'confirmation_replay',
                    0,
                    record.tool_call_id,
                )
            expiration = self._expiration_error(
                record, now, monotonic_now
            )
            if expiration is not None:
                record.state = 'timed_out'
                return self._feedback(
                    'timed_out',
                    'confirmation',
                    expiration,
                    0,
                    None,
                )
            record.state = 'cancelled'
            return self._feedback(
                'cancelled',
                'confirmation',
                'confirmation_denied',
                0,
                None,
            )

    def execute(
        self,
        tool_call_id: str,
        proposal: MissionProposalHandle,
    ) -> MissionFeedback:
        """Resolve trusted state and run one owner-bound fake mission."""
        record, error = self._authorized_execution(
            tool_call_id, proposal
        )
        if record is None:
            return self._feedback(
                'rejected', 'preflight', error, 0, None
            )
        try:
            state = self._state_resolver(
                record.proposal.authority,
                record.proposal.plan,
            )
        except Exception:
            state = None
        try:
            state_valid = (
                type(state) is TrustedMissionState
                and self._state_validator(
                    state,
                    record.proposal.authority,
                    record.proposal.plan,
                ) is True
            )
        except Exception:
            state_valid = False
        guard_error, now, monotonic_now = self._execution_guard(record)
        with self._lock:
            if record.status != 'confirmed':
                return self._feedback(
                    'rejected',
                    record.phase,
                    'execution_replay',
                    record.sequence,
                    record.tool_call_id,
                )
            if guard_error is not None:
                return self._authorization_terminal(
                    record, guard_error
                )
            if not state_valid:
                return self._terminal(
                    record, 'failed', 'state_unavailable'
                )
            state_error = self._validate_state(
                record.proposal.plan, state, now
            )
            if state_error is not None:
                return self._terminal(record, 'failed', state_error)
            if self._adapter_poisoned:
                return self._terminal(
                    record, 'failed', 'adapter_unavailable'
                )
            if (
                self._active_tool_call_id is not None
                and self._active_tool_call_id != record.tool_call_id
            ):
                return self._terminal(record, 'failed', 'mission_busy')
            self._active_tool_call_id = record.tool_call_id
            self._advance(record, 'preflight')

        context = MissionExecutionContext(
            tool_call_id=record.tool_call_id,
            proposal_id=record.proposal.proposal_id,
        )
        steps = (
            (
                'preflight',
                'preflight_failed',
                'preflight_timeout',
                self._adapter_preflight,
                (context, record.proposal.plan, state),
                self._adapter_timeout_seconds,
            ),
            (
                'navigating',
                'navigation_failed',
                'navigation_timeout',
                self._adapter_navigate,
                (context, record.proposal.plan),
                self._adapter_timeout_seconds,
            ),
            (
                'coverage',
                'coverage_failed',
                'coverage_timeout',
                self._adapter_cover,
                (context, record.proposal.plan),
                self._adapter_timeout_seconds,
            ),
            (
                'live_ready',
                'stream_unavailable',
                'stream_timeout',
                self._adapter_wait_live_ready,
                (
                    context,
                    record.proposal.plan,
                    self._stream_timeout_seconds,
                ),
                self._stream_timeout_seconds,
            ),
        )
        for (
            phase,
            failure_code,
            timeout_code,
            method,
            method_arguments,
            timeout,
        ) in steps:
            guard_error, _, monotonic_before = self._execution_guard(
                record
            )
            with self._lock:
                if record.status != 'running':
                    self._release_lease(record)
                    return self._record_feedback(record)
                if record.cancel_requested:
                    return self._record_feedback(record)
                if guard_error is not None:
                    return self._authorization_terminal(
                        record, guard_error
                    )
                if phase != 'preflight':
                    self._advance(record, phase)
                remaining = (
                    record.monotonic_deadline - monotonic_before
                )
                effective_timeout = min(timeout, max(0.0, remaining))
                deadline_limited = remaining <= timeout
                if effective_timeout <= 0:
                    return self._terminal(
                        record,
                        'timed_out',
                        'authorization_expired',
                    )
                record.phase_call_inflight = True
            outcome, hung = self._bounded_adapter_call(
                method, method_arguments, effective_timeout
            )
            post_guard_error, _, _ = self._execution_guard(record)
            with self._lock:
                record.phase_call_inflight = False
                if hung:
                    self._adapter_poisoned = True
                if record.status != 'running':
                    self._release_lease(record)
                    return self._record_feedback(record)
                if record.cancel_requested:
                    return self._record_feedback(record)
                if post_guard_error is not None:
                    return self._authorization_terminal(
                        record, post_guard_error
                    )
                if hung and deadline_limited:
                    return self._terminal(
                        record,
                        'timed_out',
                        'authorization_expired',
                    )
                if outcome.status == 'timed_out':
                    return self._terminal(
                        record, 'timed_out', timeout_code
                    )
                if outcome.status != 'succeeded':
                    return self._terminal(record, 'failed', failure_code)
        final_guard_error, _, _ = self._execution_guard(record)
        with self._lock:
            if record.status != 'running':
                self._release_lease(record)
                return self._record_feedback(record)
            if record.cancel_requested:
                return self._record_feedback(record)
            if final_guard_error is not None:
                return self._authorization_terminal(
                    record, final_guard_error
                )
            return self._terminal(
                record, 'succeeded', 'simulation_succeeded'
            )

    def cancel(
        self,
        tool_call_id: str,
        proposal: MissionProposalHandle,
    ) -> MissionFeedback:
        """Cancel one owner-bound mission outside the ledger lock."""
        record, error = self._authorized_execution(
            tool_call_id, proposal
        )
        if record is None:
            return self._feedback(
                'rejected', 'terminal', error, 0, None
            )
        with self._lock:
            if record.status not in {'confirmed', 'running'}:
                return self._feedback(
                    'rejected',
                    record.phase,
                    'cancellation_replay',
                    record.sequence,
                    record.tool_call_id,
                )
            if record.status == 'confirmed':
                return self._terminal(
                    record, 'cancelled', 'mission_cancelled'
                )
            if record.cancel_requested:
                return self._record_feedback(record)
            record.cancel_requested = True
            record.code = 'cancellation_started'
            record.sequence += 1
            context = MissionExecutionContext(
                tool_call_id=record.tool_call_id,
                proposal_id=record.proposal.proposal_id,
            )
            plan = record.proposal.plan
        outcome, hung = self._bounded_adapter_call(
            self._adapter_cancel,
            (context, plan),
            self._cancellation_timeout_seconds,
        )
        with self._lock:
            if hung:
                self._adapter_poisoned = True
            if record.status != 'running':
                return self._record_feedback(record)
            if outcome.status == 'succeeded':
                return self._terminal(
                    record,
                    'cancelled',
                    'mission_cancelled',
                    release_lease=not record.phase_call_inflight,
                )
            if outcome.status == 'timed_out':
                return self._terminal(
                    record,
                    'timed_out',
                    'cancellation_timeout',
                    release_lease=not record.phase_call_inflight,
                )
            return self._terminal(
                record,
                'failed',
                'cancellation_failed',
                release_lease=not record.phase_call_inflight,
            )

    def feedback(
        self,
        tool_call_id: str,
        proposal: MissionProposalHandle,
    ) -> MissionFeedback:
        """Read state only after current owner/session authorization."""
        record, error = self._authorized_execution(
            tool_call_id, proposal
        )
        if record is None:
            return self._feedback(
                'rejected', 'terminal', error, 0, None
            )
        with self._lock:
            return self._record_feedback(record)

    def _authorized_proposal(
        self,
        handle: Any,
    ) -> Tuple[Optional[_ProposalRecord], str]:
        if type(handle) is not MissionProposalHandle:
            return None, 'authority_required'
        with self._lock:
            record = self._proposals.get(handle.proposal_id)
            if record is None or record.handle is not handle:
                return None, 'authority_required'
        if not self._authority_is_current(record.authority):
            self._tombstone_revoked_proposal(record)
            return None, 'authority_revoked'
        return record, ''

    def _tombstone_revoked_proposal(
        self,
        record: _ProposalRecord,
    ) -> None:
        """Permanently fence a proposal whose authority is no longer valid."""
        with self._lock:
            if record.state == 'proposed':
                record.state = 'failed'
                return
            tool_call_id = record.tool_call_id
            execution = (
                self._executions.get(tool_call_id)
                if tool_call_id is not None
                else None
            )
            if (
                execution is not None
                and execution.status in {'confirmed', 'running'}
            ):
                execution.cancel_requested = True
                self._terminal(
                    execution,
                    'failed',
                    'authority_revoked',
                    release_lease=not execution.phase_call_inflight,
                )
            elif record.state == 'confirmed':
                record.state = 'failed'

    def _authorized_execution(
        self,
        tool_call_id: Any,
        handle: Any,
    ) -> Tuple[Optional[_ExecutionRecord], str]:
        proposal, error = self._authorized_proposal(handle)
        if proposal is None:
            return None, error
        try:
            key = _identifier(tool_call_id, 'tool_call_id')
        except RoomMissionValidationError:
            return None, 'authority_required'
        with self._lock:
            record = self._executions.get(key)
            if record is None or record.proposal is not proposal:
                return None, 'authority_required'
        return record, ''

    @staticmethod
    def _authority_matches_snapshot(
        authority: MissionAuthority,
        snapshot: Dict[str, Any],
    ) -> bool:
        return (
            authority.request_id == snapshot['request_id']
            and authority.conversation_id == snapshot['conversation_id']
            and authority.turn_id == snapshot['turn_id']
            and authority.conversation_generation
            == snapshot['conversation_generation']
            and authority.conversation_revision
            == snapshot['conversation_revision']
            and authority.conversation_ordinal
            == snapshot['conversation_ordinal']
            and authority.decision_digest == _sha256_json(snapshot)
        )

    def _authority_is_current(
        self,
        authority: MissionAuthority,
    ) -> bool:
        try:
            return self._authority_validator(authority) is True
        except Exception:
            return False

    @staticmethod
    def _confirmation_matches(
        proposal: _ProposalRecord,
        confirmation: TrustedConfirmation,
        confirmation_key: str,
        now: float,
    ) -> bool:
        return (
            confirmation.confirmation_id == confirmation_key
            and confirmation.authority is proposal.authority
            and confirmation.decision_id == proposal.decision_id
            and confirmation.arguments_digest
            == proposal.arguments_digest
            and confirmation.decision_expires_at == proposal.expires_at
            and proposal.issued_at - MAX_CLOCK_SKEW_SECONDS
            <= confirmation.issued_at
            <= now + MAX_CLOCK_SKEW_SECONDS
            and confirmation.issued_at < confirmation.expires_at
            and now < confirmation.expires_at
            and confirmation.expires_at <= proposal.expires_at
            and confirmation.expires_at - confirmation.issued_at
            <= MAX_ACTION_TTL_SECONDS + 1e-9
        )

    @staticmethod
    def _expiration_error(
        proposal: _ProposalRecord,
        now: float,
        monotonic_now: float,
    ) -> Optional[str]:
        if (
            now < proposal.issued_at - MAX_CLOCK_SKEW_SECONDS
            or monotonic_now < proposal.monotonic_issued_at
        ):
            return 'clock_invalid'
        if (
            now >= proposal.expires_at
            or monotonic_now >= proposal.monotonic_deadline
        ):
            return 'confirmation_expired'
        return None

    @staticmethod
    def _execution_expiration_error(
        record: _ExecutionRecord,
        now: float,
        monotonic_now: float,
    ) -> Optional[str]:
        proposal = record.proposal
        if (
            now < proposal.issued_at - MAX_CLOCK_SKEW_SECONDS
            or monotonic_now < proposal.monotonic_issued_at
        ):
            return 'clock_invalid'
        if (
            now >= proposal.expires_at
            or monotonic_now >= record.monotonic_deadline
        ):
            return 'authorization_expired'
        return None

    def _execution_guard(
        self,
        record: _ExecutionRecord,
    ) -> Tuple[Optional[str], float, float]:
        if not self._authority_is_current(record.proposal.authority):
            return 'authority_revoked', 0.0, 0.0
        try:
            now, monotonic_now = self._time_snapshot()
        except RoomMissionValidationError:
            return 'clock_invalid', 0.0, 0.0
        return (
            self._execution_expiration_error(
                record, now, monotonic_now
            ),
            now,
            monotonic_now,
        )

    def _authorization_terminal(
        self,
        record: _ExecutionRecord,
        code: str,
    ) -> MissionFeedback:
        status = 'failed' if code == 'authority_revoked' else 'timed_out'
        return self._terminal(record, status, code)

    def _validate_state(
        self,
        plan: RoomMissionPlan,
        state: TrustedMissionState,
        now: float,
    ) -> Optional[str]:
        age = now - float(state.observed_at)
        if (
            age < -MAX_CLOCK_SKEW_SECONDS
            or age > self._max_state_age_seconds
        ):
            return 'stale_state'
        if (
            state.map_id != plan.map_id
            or state.map_revision != plan.map_revision
        ):
            return 'map_changed'
        if state.emergency_stop:
            return 'emergency_stop'
        if state.privacy_mode:
            return 'privacy_mode'
        if not state.navigation_available:
            return 'navigation_unavailable'
        if not state.localization_ok:
            return 'localization_unavailable'
        if not state.camera_available:
            return 'camera_unavailable'
        if not state.stream_available:
            return 'stream_unavailable'
        return None

    @staticmethod
    def _bounded_adapter_call(
        method: Callable[..., AdapterStepResult],
        arguments: Tuple[Any, ...],
        timeout_seconds: float,
    ) -> Tuple[AdapterStepResult, bool]:
        completed = threading.Event()
        result_box: Dict[str, Any] = {}

        def invoke() -> None:
            try:
                result_box['result'] = method(*arguments)
            except Exception:
                result_box['result'] = AdapterStepResult('failed')
            finally:
                completed.set()

        worker = threading.Thread(
            target=invoke,
            name='room-mission-adapter',
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            return AdapterStepResult('failed'), False
        if not completed.wait(timeout_seconds):
            return AdapterStepResult('timed_out'), True
        result = result_box.get('result')
        if type(result) is not AdapterStepResult:
            return AdapterStepResult('failed'), False
        return result, False

    def _advance(self, record: _ExecutionRecord, phase: str) -> None:
        record.status = 'running'
        record.phase = phase
        record.code = f'{phase}_started'
        record.sequence += 1

    def _terminal(
        self,
        record: _ExecutionRecord,
        status: str,
        code: str,
        *,
        release_lease: bool = True,
    ) -> MissionFeedback:
        record.status = status
        record.phase = 'terminal'
        record.code = code
        record.sequence += 1
        record.proposal.state = status
        if release_lease:
            self._release_lease(record)
        return self._record_feedback(record)

    def _release_lease(self, record: _ExecutionRecord) -> None:
        if self._active_tool_call_id == record.tool_call_id:
            self._active_tool_call_id = None

    def _record_feedback(self, record: _ExecutionRecord) -> MissionFeedback:
        return self._feedback(
            record.status,
            record.phase,
            record.code,
            record.sequence,
            record.tool_call_id,
        )

    def _feedback(
        self,
        status: str,
        phase: str,
        code: str,
        sequence: int,
        tool_call_id: Optional[str],
    ) -> MissionFeedback:
        return MissionFeedback(
            status=status,
            phase=phase,
            code=code,
            sequence=sequence,
            tool_call_id=tool_call_id,
        )

    def _new_id(self, prefix: str) -> str:
        token = None
        try:
            token = self._id_factory()
        except Exception:
            pass
        if token is None:
            raise RoomMissionValidationError('server ID generation failed')
        if (
            type(token) is not str
            or not token
            or len(token) > 64
            or not token.isascii()
            or any(
                not (character.isalnum() or character in {'-', '_'})
                for character in token
            )
        ):
            raise RoomMissionValidationError('server ID is invalid')
        return f'{prefix}-{token}'

    def _time_snapshot(self) -> Tuple[float, float]:
        wall = None
        monotonic = None
        try:
            wall = self._clock()
            monotonic = self._monotonic_clock()
        except Exception:
            pass
        if wall is None or monotonic is None:
            raise RoomMissionValidationError('mission clock failed')
        for value in (wall, monotonic):
            if (
                type(value) not in {int, float}
                or not math.isfinite(float(value))
            ):
                raise RoomMissionValidationError('mission clock is invalid')
        return float(wall), float(monotonic)


def _monitor_room_arguments(value: Any) -> Dict[str, str]:
    if type(value) is not dict or set(value) != {'location'}:
        raise RoomMissionValidationError('monitor_room arguments are invalid')
    location = _label(value['location'])
    return {'location': location}


def monitor_room_arguments_digest(value: Any) -> str:
    """Return the canonical digest used by a trusted confirmation issuer."""
    return _sha256_json(_monitor_room_arguments(value))


def orchestration_authority_digest(value: OrchestrationResult) -> str:
    """Digest one committed orchestration snapshot for an authority store."""
    return _sha256_json(_orchestration_payload(value))


def _orchestration_payload(value: Any) -> Dict[str, Any]:
    if type(value) is not OrchestrationResult:
        raise RoomMissionValidationError('orchestration result is invalid')
    payload = None
    try:
        payload = {
            'request_id': value.request_id,
            'conversation_id': value.conversation_id,
            'turn_id': value.turn_id,
            'conversation_generation': value.conversation_generation,
            'conversation_revision': value.conversation_revision,
            'conversation_ordinal': value.conversation_ordinal,
            'decision_id': value.decision_id,
            'issued_at': value.issued_at,
            'expires_at': value.expires_at,
            'state_trusted': value.state_trusted,
            'memory_revision': value.memory_revision,
            'raw_decision': value.raw_decision.to_dict(),
            'decision': value.decision.to_dict(),
            'safety': value.safety.to_dict(),
        }
    except Exception:
        pass
    if payload is None:
        raise RoomMissionValidationError('orchestration result is invalid')
    return _json_snapshot(payload)


def _sha256_digest(value: Any, field_name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise RoomMissionValidationError(f'{field_name} is invalid')
    return value


def _identifier(value: Any, field_name: str) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise RoomMissionValidationError(f'{field_name} is invalid')
    return value


def _label(value: Any) -> str:
    if type(value) is not str:
        raise RoomMissionValidationError('room label is invalid')
    result = value.strip()
    if (
        not 1 <= len(result) <= 64
        or any(
            unicodedata.category(character).startswith('C')
            for character in result
        )
    ):
        raise RoomMissionValidationError('room label is invalid')
    return result


def _normalize_alias(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _json_snapshot(value: Any) -> Dict[str, Any]:
    if type(value) is not dict:
        raise RoomMissionValidationError('semantic map is invalid')
    encoded = None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError):
        pass
    if encoded is None:
        raise RoomMissionValidationError('semantic map is invalid')
    if len(encoded) > MAX_MAP_BYTES:
        raise RoomMissionValidationError('semantic map is too large')
    return copy.deepcopy(json.loads(encoded.decode('utf-8')))


def _sha256_json(value: Any) -> str:
    encoded = None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError):
        pass
    if encoded is None:
        raise RoomMissionValidationError('JSON binding is invalid')
    return hashlib.sha256(encoded).hexdigest()


def _validate_geometry(
    value: Any,
) -> Tuple[Tuple[Tuple[Tuple[float, float], ...], ...], ...]:
    if type(value) is not dict or set(value) != {'type', 'coordinates'}:
        raise RoomMissionValidationError('room geometry is invalid')
    geometry_type = value['type']
    coordinates = value['coordinates']
    if geometry_type == 'Polygon':
        polygons = [coordinates]
    elif geometry_type == 'MultiPolygon':
        polygons = coordinates
    else:
        raise RoomMissionValidationError('room geometry is invalid')
    if type(polygons) is not list or not polygons:
        raise RoomMissionValidationError('room geometry is invalid')
    result = []
    total_points = 0
    for polygon in polygons:
        if type(polygon) is not list or len(polygon) != 1:
            raise RoomMissionValidationError('room holes are unsupported')
        rings = []
        for ring in polygon:
            if type(ring) is not list or len(ring) < 4:
                raise RoomMissionValidationError('room geometry is invalid')
            points = []
            for point in ring:
                if type(point) is not list or len(point) != 2:
                    raise RoomMissionValidationError(
                        'room geometry is invalid'
                    )
                if any(
                    type(item) not in {int, float}
                    or not math.isfinite(float(item))
                    or abs(float(item)) > 10000
                    for item in point
                ):
                    raise RoomMissionValidationError(
                        'room geometry is invalid'
                    )
                points.append((float(point[0]), float(point[1])))
            if points[0] != points[-1]:
                raise RoomMissionValidationError('room ring is not closed')
            _validate_simple_ring(tuple(points))
            total_points += len(points)
            if total_points > MAX_RING_POINTS:
                raise RoomMissionValidationError('room geometry is too large')
            rings.append(tuple(points))
        result.append(tuple(rings))
    outer_rings = [polygon[0] for polygon in result]
    for index, first in enumerate(outer_rings):
        for second in outer_rings[index + 1:]:
            if _rings_overlap(first, second):
                raise RoomMissionValidationError(
                    'room multipolygon overlaps'
                )
    return tuple(result)


def _validate_simple_ring(
    ring: Tuple[Tuple[float, float], ...],
) -> None:
    vertices = ring[:-1]
    if len(set(vertices)) != len(vertices) or len(vertices) < 3:
        raise RoomMissionValidationError('room ring is degenerate')
    twice_area = sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(ring, ring[1:])
    )
    if abs(twice_area) <= 1e-9:
        raise RoomMissionValidationError('room ring has no area')
    edge_count = len(ring) - 1
    for first_index in range(edge_count):
        first = (ring[first_index], ring[first_index + 1])
        for second_index in range(first_index + 1, edge_count):
            adjacent = (
                second_index == first_index + 1
                or (
                    first_index == 0
                    and second_index == edge_count - 1
                )
            )
            if adjacent:
                continue
            second = (ring[second_index], ring[second_index + 1])
            if _segments_intersect(*first, *second):
                raise RoomMissionValidationError(
                    'room ring self-intersects'
                )


def _rings_overlap(
    first: Tuple[Tuple[float, float], ...],
    second: Tuple[Tuple[float, float], ...],
) -> bool:
    if any(
        _segments_intersect(first_a, first_b, second_a, second_b)
        for first_a, first_b in zip(first, first[1:])
        for second_a, second_b in zip(second, second[1:])
    ):
        return True
    return (
        _ring_location(first, *second[0]) != 0
        or _ring_location(second, *first[0]) != 0
    )


def _segments_intersect(
    first_a: Tuple[float, float],
    first_b: Tuple[float, float],
    second_a: Tuple[float, float],
    second_b: Tuple[float, float],
) -> bool:
    orientations = (
        _orientation(first_a, first_b, second_a),
        _orientation(first_a, first_b, second_b),
        _orientation(second_a, second_b, first_a),
        _orientation(second_a, second_b, first_b),
    )
    if orientations[0] * orientations[1] < 0 and (
        orientations[2] * orientations[3] < 0
    ):
        return True
    return (
        orientations[0] == 0
        and _on_segment(first_a, first_b, *second_a)
        or orientations[1] == 0
        and _on_segment(first_a, first_b, *second_b)
        or orientations[2] == 0
        and _on_segment(second_a, second_b, *first_a)
        or orientations[3] == 0
        and _on_segment(second_a, second_b, *first_b)
    )


def _orientation(
    first: Tuple[float, float],
    second: Tuple[float, float],
    third: Tuple[float, float],
) -> int:
    value = (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )
    if abs(value) <= 1e-9:
        return 0
    return 1 if value > 0 else -1


def _geometry_strictly_contains(
    geometry: Tuple[Tuple[Tuple[Tuple[float, float], ...], ...], ...],
    x: float,
    y: float,
) -> bool:
    for polygon in geometry:
        outer = _ring_location(polygon[0], x, y)
        if outer != 1:
            continue
        if all(_ring_location(hole, x, y) == 0 for hole in polygon[1:]):
            return True
    return False


def _ring_location(
    ring: Tuple[Tuple[float, float], ...],
    x: float,
    y: float,
) -> int:
    inside = False
    for first, second in zip(ring, ring[1:]):
        if _on_segment(first, second, x, y):
            return 2
        x1, y1 = first
        x2, y2 = second
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if crossing > x:
                inside = not inside
    return 1 if inside else 0


def _on_segment(
    first: Tuple[float, float],
    second: Tuple[float, float],
    x: float,
    y: float,
) -> bool:
    x1, y1 = first
    x2, y2 = second
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > 1e-9:
        return False
    return (
        min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9
        and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9
    )
