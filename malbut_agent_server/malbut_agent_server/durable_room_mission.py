"""Restart-safe controller for the built-in simulated room mission.

This module is a parallel durability path.  It accepts only the exact
built-in simulation adapter and writes every adapter intent to the SQLite
room-mission ledger before dispatch.  It performs no ROS, camera, network,
microphone, or physical-device operation.

Pre-confirm authority, source, map, and device invalidations use the typed
durable ledger transition and never masquerade as ``user_denied``.
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
import weakref
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Optional, Tuple

from malbut_agent_server.orchestrator import OrchestrationResult
from malbut_agent_server.room_mission import (
    MAX_ACTION_TTL_SECONDS,
    MAX_CLOCK_SKEW_SECONDS,
    AdapterStepResult,
    MissionExecutionContext,
    RoomMissionPlan,
    RoomPose,
    SemanticRoom,
    RoomMissionValidationError,
    SemanticRoomResolver,
    SimulationRoomMissionAdapter,
    TrustedMissionState,
    monitor_room_arguments_digest,
    orchestration_authority_digest,
)
from malbut_agent_server.room_mission_ledger import (
    CancelIntent,
    DurableMissionAuthority,
    DurableMissionConfirmation,
    DurableMissionProposal,
    ExecutionLease,
    PhaseIntent,
    RoomMissionLedgerAuthorityError,
    RoomMissionLedgerBusyError,
    RoomMissionLedgerConflictError,
    RoomMissionLedgerError,
    RoomMissionLedgerValidationError,
    SQLiteRoomMissionStore,
    StoredMissionAuthorization,
    StoredMissionExecution,
)


_EXECUTABLE_PHASES = ('preflight', 'navigating', 'coverage', 'live_ready')
_TERMINAL_STATUSES = frozenset({
    'succeeded', 'failed', 'cancelled', 'timed_out',
})
_PUBLIC_STATUSES = frozenset({
    'proposed', 'confirmed', 'denied', 'pending', 'leased', 'running',
    'cancelling', 'reconcile_required', 'succeeded', 'failed',
    'cancelled', 'timed_out', 'rejected',
})
_PUBLIC_PHASES = frozenset({
    'proposal', 'confirmation', 'preflight', 'navigating', 'coverage',
    'live_ready', 'terminal',
})
_ADAPTER_SHADOW_NAMES = frozenset({
    'preflight', 'navigate', 'cover', 'wait_live_ready', 'cancel', '_step',
})
_RESOLVER_SHADOW_NAMES = frozenset({'plan', 'resolve'})
_BUILTIN_SIMULATION_STEP = SimulationRoomMissionAdapter.__dict__['_step']
_BUILTIN_SIMULATION_INIT = (
    SimulationRoomMissionAdapter.__dict__['__init__']
)
_BUILTIN_MAP_ID_GETTER = SemanticRoomResolver.__dict__['map_id'].fget
_BUILTIN_MAP_REVISION_GETTER = (
    SemanticRoomResolver.__dict__['map_revision'].fget
)


@dataclass(frozen=True, repr=False)
class SimulationDeviceBinding:
    """Opaque stable identity for one simulated executor slot."""

    device_id: str
    device_binding_digest: str

    def __post_init__(self) -> None:
        """Validate the content-free stable binding."""
        _identifier(self.device_id)
        _digest(self.device_binding_digest)

    def __repr__(self) -> str:
        """Avoid reflecting device identity in logs."""
        return '<SimulationDeviceBinding opaque>'


@dataclass(frozen=True, repr=False, eq=False)
class DurableMissionProposalHandle:
    """Opaque process-identity-bound locator for one durable proposal."""

    proposal_id: str

    def __post_init__(self) -> None:
        """Validate only the opaque durable identifier."""
        _identifier(self.proposal_id)

    def __repr__(self) -> str:
        """Hide the durable locator from ordinary logs."""
        return '<DurableMissionProposalHandle opaque>'

    def to_dict(self) -> Dict[str, str]:
        """Return only the opaque proposal locator."""
        return {'proposal_id': self.proposal_id}


@dataclass(frozen=True)
class DurableMissionFeedback:
    """Content-free, explicitly simulated durable mission feedback."""

    status: str
    phase: str
    code: str
    sequence: int
    tool_call_id: Optional[str] = None
    terminal_source: Optional[str] = None
    runtime_mode: str = 'simulation'
    simulated: bool = True
    physical_effects: bool = False
    viewer_live: bool = False
    durability: str = 'sqlite_local'
    lease_scope: str = 'database_device'

    def __post_init__(self) -> None:
        """Keep the public receipt bounded and honest."""
        if self.status not in _PUBLIC_STATUSES:
            raise RoomMissionValidationError('mission status is invalid')
        if self.phase not in _PUBLIC_PHASES:
            raise RoomMissionValidationError('mission phase is invalid')
        _code(self.code)
        if type(self.sequence) is not int or self.sequence < 0:
            raise RoomMissionValidationError('mission sequence is invalid')
        if self.tool_call_id is not None:
            _identifier(self.tool_call_id)
        if self.terminal_source not in {
            None, 'controller', 'simulation_adapter', 'recovery',
        }:
            raise RoomMissionValidationError('mission source is invalid')
        if (
            self.runtime_mode != 'simulation'
            or self.simulated is not True
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.durability != 'sqlite_local'
            or self.lease_scope != 'database_device'
        ):
            raise RoomMissionValidationError('simulation marker is invalid')

    def to_dict(self) -> Dict[str, Any]:
        """Return no room label, pose, transcript, or secret."""
        return {
            'status': self.status,
            'phase': self.phase,
            'code': self.code,
            'sequence': self.sequence,
            'tool_call_id': self.tool_call_id,
            'terminal_source': self.terminal_source,
            'runtime_mode': self.runtime_mode,
            'simulated': self.simulated,
            'physical_effects': self.physical_effects,
            'viewer_live': self.viewer_live,
            'durability': self.durability,
            'lease_scope': self.lease_scope,
        }


@dataclass(frozen=True, repr=False)
class DurableProposalResult:
    """Content-free proposal response with an optional opaque handle."""

    feedback: DurableMissionFeedback
    proposal: Optional[DurableMissionProposalHandle] = None

    def __repr__(self) -> str:
        """Avoid recursively reflecting proposal identifiers."""
        state = '<opaque>' if self.proposal is not None else 'None'
        return (
            f'DurableProposalResult(feedback={self.feedback!r}, '
            f'proposal={state})'
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the content-free public contract."""
        return {
            'feedback': self.feedback.to_dict(),
            'proposal': (
                self.proposal.to_dict()
                if self.proposal is not None
                else None
            ),
        }


@dataclass(frozen=True, repr=False)
class _ProposalRecord:
    """Private snapshotted proposal material for one controller process."""

    handle: DurableMissionProposalHandle
    authority: DurableMissionAuthority
    decision_id: str
    arguments_digest: str
    location: str
    plan_digest: str
    device_id: str
    device_binding_digest: str
    source: OrchestrationResult
    source_digest: str
    issued_at: float
    expires_at: float


def _identifier(value: Any) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or any(ord(character) < 32 or ord(character) == 127
               for character in value)
    ):
        raise RoomMissionValidationError('mission identifier is invalid')
    return value


def _digest(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise RoomMissionValidationError('mission digest is invalid')
    return value


def _code(value: Any) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or not value.isascii()
        or not value.replace('_', '').isalnum()
    ):
        raise RoomMissionValidationError('mission code is invalid')
    return value


def _json_digest(value: Any) -> str:
    failed = False
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError):
        failed = True
        encoded = b''
    if failed:
        raise RoomMissionValidationError('mission binding is invalid')
    return hashlib.sha256(encoded).hexdigest()


def _plan_digest(plan: RoomMissionPlan) -> str:
    """Digest the one canonical server-owned plan projection."""
    if type(plan) is not RoomMissionPlan:
        raise RoomMissionValidationError('mission plan is invalid')
    return _json_digest({
        'map_id': plan.map_id,
        'map_revision': plan.map_revision,
        'room_id': plan.room_id,
        'navigation_goal': plan.navigation_goal.to_dict(),
        'coverage_viewpoints': [
            pose.to_dict() for pose in plan.coverage_viewpoints
        ],
    })


def _snapshot_resolver(
    resolver: SemanticRoomResolver,
) -> Tuple[str, str, MappingProxyType]:
    """Deep-snapshot validated plans without shadowable method calls."""
    if (
        type(resolver) is not SemanticRoomResolver
        or type(resolver._aliases) is not MappingProxyType
        or type(resolver._rooms) is not MappingProxyType
    ):
        raise RoomMissionValidationError('resolver is invalid')
    map_id = _BUILTIN_MAP_ID_GETTER(resolver)
    map_revision = _BUILTIN_MAP_REVISION_GETTER(resolver)
    _identifier(map_id)
    _digest(map_revision)
    rooms = dict(resolver._rooms)
    plans: Dict[str, RoomMissionPlan] = {}
    for alias, room_id in dict(resolver._aliases).items():
        if (
            type(alias) is not str
            or _normalize_location(alias) != alias
            or type(room_id) is not str
        ):
            raise RoomMissionValidationError('resolver is invalid')
        room = rooms.get(room_id)
        if type(room) is not SemanticRoom:
            raise RoomMissionValidationError('resolver is invalid')
        navigation_goal = RoomPose(
            x=room.navigation_goal.x,
            y=room.navigation_goal.y,
            yaw=room.navigation_goal.yaw,
        )
        coverage_viewpoints = tuple(
            RoomPose(x=pose.x, y=pose.y, yaw=pose.yaw)
            for pose in room.coverage_viewpoints
        )
        plan = RoomMissionPlan(
            map_id=map_id,
            map_revision=map_revision,
            room_id=room.room_id,
            navigation_goal=navigation_goal,
            coverage_viewpoints=coverage_viewpoints,
        )
        _plan_digest(plan)
        plans[alias] = plan
    if not plans:
        raise RoomMissionValidationError('resolver is invalid')
    return map_id, map_revision, MappingProxyType(plans)


def _sealed_simulation_step(
    context: MissionExecutionContext,
    phase: str,
    fail_phase: Optional[str],
    timeout_phase: Optional[str],
) -> AdapterStepResult:
    """Run one original fake step on an unreachable fresh exact instance."""
    adapter = object.__new__(SimulationRoomMissionAdapter)
    _BUILTIN_SIMULATION_INIT(
        adapter,
        fail_phase=fail_phase,
        timeout_phase=timeout_phase,
        phase_gates=(),
    )
    return _BUILTIN_SIMULATION_STEP(adapter, context, phase)


_CONTROLLER_FIELDS_GUARD = threading.Lock()
_CONTROLLER_FIELDS = weakref.WeakKeyDictionary()
_CONTROLLER_LAYOUT_TOKEN = object()


def _register_controller_fields(
    controller: Any,
    fields: Dict[str, Any],
) -> None:
    """Bind controller state outside its directly mutable instance."""
    with _CONTROLLER_FIELDS_GUARD:
        if controller in _CONTROLLER_FIELDS:
            raise RoomMissionValidationError(
                'mission controller is invalid'
            )
        _CONTROLLER_FIELDS[controller] = fields


def _controller_field(controller: Any, name: str) -> Any:
    """Read one externally bound controller field."""
    with _CONTROLLER_FIELDS_GUARD:
        fields = _CONTROLLER_FIELDS.get(controller)
        if fields is None or name not in fields:
            raise AttributeError('mission configuration is immutable')
        return fields[name]


def _set_controller_poisoned(controller: Any) -> None:
    """Set the sole mutable scalar without exposing an instance slot."""
    with _CONTROLLER_FIELDS_GUARD:
        fields = _CONTROLLER_FIELDS.get(controller)
        if fields is None:
            raise AttributeError('mission configuration is immutable')
        fields['_adapter_poisoned'] = True


class _ControllerField:
    """Data descriptor immune to direct instance field replacement."""

    __slots__ = ('_name',)

    def __init__(self, name: str) -> None:
        self._name = name

    def __get__(self, instance: Any, owner: Any) -> Any:
        if instance is None:
            return self
        return _controller_field(instance, self._name)

    def __set__(self, instance: Any, value: Any) -> None:
        del instance, value
        raise AttributeError('mission configuration is immutable')

    def __delete__(self, instance: Any) -> None:
        del instance
        raise AttributeError('mission configuration is immutable')


def _normalize_location(value: Any) -> str:
    if type(value) is not str:
        raise RoomMissionValidationError('room label is invalid')
    normalized = ' '.join(
        unicodedata.normalize('NFKC', value).casefold().split()
    )
    if not 1 <= len(normalized) <= 64:
        raise RoomMissionValidationError('room label is invalid')
    return normalized


class DurableSimulationRoomMission:
    """Drive the exact fake adapter through the durable SQLite ledger."""

    __slots__ = ('__weakref__', '_layout_token')

    _store = _ControllerField('_store')
    _resolver_map_id = _ControllerField('_resolver_map_id')
    _resolver_map_revision = _ControllerField('_resolver_map_revision')
    _resolver_plans = _ControllerField('_resolver_plans')
    _adapter_fail_phase = _ControllerField('_adapter_fail_phase')
    _adapter_timeout_phase = _ControllerField('_adapter_timeout_phase')
    _device_binding = _ControllerField('_device_binding')
    _device_id = _ControllerField('_device_id')
    _device_binding_digest = _ControllerField('_device_binding_digest')
    _authority_resolver = _ControllerField('_authority_resolver')
    _authority_validator = _ControllerField('_authority_validator')
    _confirmation_resolver = _ControllerField('_confirmation_resolver')
    _state_resolver = _ControllerField('_state_resolver')
    _state_validator = _ControllerField('_state_validator')
    _clock = _ControllerField('_clock')
    _monotonic_clock = _ControllerField('_monotonic_clock')
    _worker_id = _ControllerField('_worker_id')
    _max_state_age_seconds = _ControllerField(
        '_max_state_age_seconds'
    )
    _adapter_timeout_seconds = _ControllerField(
        '_adapter_timeout_seconds'
    )
    _stream_timeout_seconds = _ControllerField(
        '_stream_timeout_seconds'
    )
    _cancellation_timeout_seconds = _ControllerField(
        '_cancellation_timeout_seconds'
    )
    _durability = _ControllerField('_durability')
    _lease_scope = _ControllerField('_lease_scope')
    _proposals = _ControllerField('_proposals')
    _revoked_proposals = _ControllerField('_revoked_proposals')
    _executing = _ControllerField('_executing')
    _cancelling = _ControllerField('_cancelling')
    _leases = _ControllerField('_leases')
    _adapter_poisoned = _ControllerField('_adapter_poisoned')
    _simulation_calls = _ControllerField('_simulation_calls')
    _simulation_calls_lock = _ControllerField(
        '_simulation_calls_lock'
    )
    _lock = _ControllerField('_lock')

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Keep the security-bound instance layout final."""
        del cls, kwargs
        raise TypeError('mission controller type is final')

    _PUBLIC_CONFIGURATION_NAMES = frozenset({
        'store',
        'resolver',
        'adapter',
        'device_binding',
        'max_state_age_seconds',
        'adapter_timeout_seconds',
        'stream_timeout_seconds',
        'cancellation_timeout_seconds',
    })
    _PRIVATE_CONFIGURATION_NAMES = frozenset({
        '_store',
        '_resolver_map_id',
        '_resolver_map_revision',
        '_resolver_plans',
        '_adapter_fail_phase',
        '_adapter_timeout_phase',
        '_device_binding',
        '_device_id',
        '_device_binding_digest',
        '_authority_resolver',
        '_authority_validator',
        '_confirmation_resolver',
        '_state_resolver',
        '_state_validator',
        '_clock',
        '_monotonic_clock',
        '_worker_id',
        '_max_state_age_seconds',
        '_adapter_timeout_seconds',
        '_stream_timeout_seconds',
        '_cancellation_timeout_seconds',
        '_durability',
        '_lease_scope',
    })

    def __setattr__(self, name: str, value: Any) -> None:
        """Reject shadows and replacement without shadowable guard sets."""
        if name in {
            '__class__', '__dict__', '__setattr__', '__delattr__',
            '_PUBLIC_CONFIGURATION_NAMES',
            '_PRIVATE_CONFIGURATION_NAMES',
        }:
            raise AttributeError('mission configuration is immutable')
        try:
            object.__getattribute__(self, name)
        except AttributeError:
            pass
        else:
            raise AttributeError('mission configuration is immutable')
        try:
            object.__setattr__(self, name, value)
        except AttributeError:
            raise AttributeError(
                'mission configuration is immutable'
            ) from None

    def __delattr__(self, name: str) -> None:
        """Prevent deletion of any controller state or method shadow."""
        del name
        raise AttributeError('mission configuration is immutable')

    def __init__(
        self,
        store: SQLiteRoomMissionStore,
        resolver: SemanticRoomResolver,
        adapter: SimulationRoomMissionAdapter,
        device_binding: SimulationDeviceBinding,
        *,
        authority_resolver: Callable[
            [OrchestrationResult], DurableMissionAuthority
        ],
        authority_validator: Callable[[DurableMissionAuthority], bool],
        confirmation_resolver: Callable[
            [str], DurableMissionConfirmation
        ],
        state_resolver: Callable[
            [DurableMissionAuthority, RoomMissionPlan], TrustedMissionState
        ],
        state_validator: Callable[
            [TrustedMissionState, DurableMissionAuthority, RoomMissionPlan],
            bool,
        ],
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        worker_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        max_state_age_seconds: float = 2.0,
        adapter_timeout_seconds: float = 10.0,
        stream_timeout_seconds: float = 10.0,
        cancellation_timeout_seconds: float = 2.0,
    ) -> None:
        """Create an exact simulation controller from trusted hooks."""
        if type(self) is not DurableSimulationRoomMission:
            raise RoomMissionValidationError('mission controller is invalid')
        object.__setattr__(self, '_layout_token', _CONTROLLER_LAYOUT_TOKEN)
        if type(store) is not SQLiteRoomMissionStore:
            raise RoomMissionValidationError('mission store is invalid')
        identity_valid = False
        try:
            identity_valid = (
                store.assert_durable_identity() is None
                and store.durability == 'sqlite_local'
                and store.lease_scope == 'database_device'
            )
        except Exception:
            identity_valid = False
        if (
            not identity_valid
        ):
            raise RoomMissionValidationError(
                'durable mission store must be file-backed'
            )
        if type(resolver) is not SemanticRoomResolver:
            raise RoomMissionValidationError('resolver is invalid')
        if type(adapter) is not SimulationRoomMissionAdapter:
            raise RoomMissionValidationError(
                'only the built-in simulation adapter is allowed'
            )
        adapter_phase_gates = adapter._phase_gates
        adapter_fail_phase = adapter._fail_phase
        adapter_timeout_phase = adapter._timeout_phase
        if any(
            name in adapter.__dict__ for name in _ADAPTER_SHADOW_NAMES
        ):
            raise RoomMissionValidationError('simulation adapter is invalid')
        if (
            type(adapter_phase_gates) is not tuple
            or adapter_phase_gates
            or (
                adapter_fail_phase is not None
                and type(adapter_fail_phase) is not str
            )
            or (
                adapter_timeout_phase is not None
                and type(adapter_timeout_phase) is not str
            )
        ):
            raise RoomMissionValidationError('simulation adapter is invalid')
        if any(
            name in resolver.__dict__ for name in _RESOLVER_SHADOW_NAMES
        ):
            raise RoomMissionValidationError('resolver is invalid')
        if type(device_binding) is not SimulationDeviceBinding:
            raise RoomMissionValidationError('device binding is invalid')
        device_id = device_binding.device_id
        device_binding_digest = device_binding.device_binding_digest
        _identifier(device_id)
        _digest(device_binding_digest)
        dependencies = (
            authority_resolver,
            authority_validator,
            confirmation_resolver,
            state_resolver,
            state_validator,
            clock,
            monotonic_clock,
            worker_id_factory,
        )
        if not all(callable(value) for value in dependencies):
            raise RoomMissionValidationError('mission dependency is invalid')
        timeouts = (
            max_state_age_seconds,
            adapter_timeout_seconds,
            stream_timeout_seconds,
            cancellation_timeout_seconds,
        )
        if any(not _valid_timeout(value) for value in timeouts):
            raise RoomMissionValidationError('mission timeout is invalid')
        resolver_snapshot = None
        snapshot_failed = False
        try:
            resolver_snapshot = _snapshot_resolver(resolver)
        except Exception:
            snapshot_failed = True
        if snapshot_failed or resolver_snapshot is None:
            raise RoomMissionValidationError(
                'simulation dependency snapshot is invalid'
            )
        worker_token = None
        failed = False
        try:
            worker_token = worker_id_factory()
            _identifier(worker_token)
        except Exception:
            failed = True
        if failed or worker_token is None:
            raise RoomMissionValidationError('mission worker is invalid')
        (
            resolver_map_id,
            resolver_map_revision,
            resolver_plans,
        ) = resolver_snapshot
        worker_id = f'durable-simulation-worker-{worker_token}'
        _identifier(worker_id)
        _register_controller_fields(self, {
            '_store': store,
            '_resolver_map_id': resolver_map_id,
            '_resolver_map_revision': resolver_map_revision,
            '_resolver_plans': resolver_plans,
            '_adapter_fail_phase': adapter_fail_phase,
            '_adapter_timeout_phase': adapter_timeout_phase,
            '_device_binding': device_binding,
            '_device_id': device_id,
            '_device_binding_digest': device_binding_digest,
            '_authority_resolver': authority_resolver,
            '_authority_validator': authority_validator,
            '_confirmation_resolver': confirmation_resolver,
            '_state_resolver': state_resolver,
            '_state_validator': state_validator,
            '_clock': clock,
            '_monotonic_clock': monotonic_clock,
            '_worker_id': worker_id,
            '_max_state_age_seconds': float(max_state_age_seconds),
            '_adapter_timeout_seconds': float(adapter_timeout_seconds),
            '_stream_timeout_seconds': float(stream_timeout_seconds),
            '_cancellation_timeout_seconds': float(
                cancellation_timeout_seconds
            ),
            '_durability': 'sqlite_local',
            '_lease_scope': 'database_device',
            '_proposals': {},
            '_revoked_proposals': set(),
            '_executing': set(),
            '_cancelling': set(),
            '_leases': {},
            '_adapter_poisoned': False,
            '_simulation_calls': [],
            '_simulation_calls_lock': threading.Lock(),
            '_lock': threading.RLock(),
        })

    @property
    def simulation_calls(self) -> Tuple[Tuple[str, str], ...]:
        """Return content-free calls from the sealed fake runtime."""
        with self._simulation_calls_lock:
            return tuple(self._simulation_calls)

    def propose(self, result: OrchestrationResult) -> DurableProposalResult:
        """Validate, snapshot, and durably register one proposal."""
        return self._register(result, rehydrating=False)

    def rehydrate(
        self,
        persisted_result: OrchestrationResult,
    ) -> DurableProposalResult:
        """Reissue a local handle only after full durable revalidation."""
        return self._register(persisted_result, rehydrating=True)

    def _register(
        self,
        result: OrchestrationResult,
        *,
        rehydrating: bool,
    ) -> DurableProposalResult:
        snapshot = _snapshot_orchestration(result)
        now = self._safe_clock()
        if (
            snapshot is None
            or now is None
            or not _valid_monitor_snapshot(
                snapshot, now, require_current=not rehydrating
            )
        ):
            return DurableProposalResult(self._feedback(
                'rejected', 'proposal', 'untrusted_proposal', 0,
            ))
        source_digest = _json_digest(snapshot)
        authority_digest = None
        arguments_digest = None
        try:
            authority_digest = orchestration_authority_digest(result)
            arguments_digest = monitor_room_arguments_digest(
                snapshot['decision']['arguments']
            )
        except Exception:
            pass
        if authority_digest is None or arguments_digest is None:
            return DurableProposalResult(self._feedback(
                'rejected', 'proposal', 'untrusted_proposal', 0,
            ))
        authority = None
        try:
            authority = self._authority_resolver(result)
        except Exception:
            pass
        if (
            type(authority) is not DurableMissionAuthority
            or not _authority_matches(authority, snapshot, authority_digest)
        ):
            return DurableProposalResult(self._feedback(
                'rejected', 'proposal', 'authority_unavailable', 0,
            ))
        location = snapshot['decision']['arguments']['location']
        plan = None
        try:
            plan = self._resolve_plan(location)
            plan_digest = _plan_digest(plan)
        except Exception:
            plan_digest = None
        if (
            plan is None
            or plan_digest is None
        ):
            return DurableProposalResult(self._feedback(
                'rejected', 'proposal', 'room_unavailable', 0,
            ))
        durable = None
        try:
            durable = DurableMissionProposal(
                authority=authority,
                decision_id=snapshot['decision_id'],
                arguments_digest=arguments_digest,
                device_id=self._device_id,
                device_binding_digest=self._device_binding_digest,
                map_id=plan.map_id,
                map_revision=plan.map_revision,
                room_id=plan.room_id,
                plan_digest=plan_digest,
                issued_at=float(snapshot['issued_at']),
                expires_at=float(snapshot['expires_at']),
            )
            stored = self._store.register_proposal(durable)
        except (
            RoomMissionLedgerAuthorityError,
            RoomMissionLedgerConflictError,
        ):
            return DurableProposalResult(self._feedback(
                'rejected', 'proposal', 'proposal_conflict', 0,
            ))
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return DurableProposalResult(self._feedback(
                'rejected', 'proposal', 'ledger_unavailable', 0,
            ))
        if stored.status not in {'proposed', 'confirmed'}:
            status = (
                stored.status
                if stored.status in _PUBLIC_STATUSES
                else 'failed'
            )
            return DurableProposalResult(self._feedback(
                status,
                'proposal',
                'proposal_unavailable',
                0,
            ))
        candidate = DurableMissionProposalHandle(stored.proposal_id)
        record = _ProposalRecord(
            handle=candidate,
            authority=authority,
            decision_id=snapshot['decision_id'],
            arguments_digest=arguments_digest,
            location=location,
            plan_digest=plan_digest,
            device_id=self._device_id,
            device_binding_digest=self._device_binding_digest,
            source=result,
            source_digest=source_digest,
            issued_at=float(snapshot['issued_at']),
            expires_at=float(snapshot['expires_at']),
        )
        invalidation_code = self._proposal_invalidation_code(record)
        if invalidation_code is not None:
            if stored.status == 'confirmed':
                return DurableProposalResult(
                    self._abort_confirmed_invalidation(
                        record, invalidation_code
                    )
                )
            invalidated = self._invalidate_proposal(
                record, invalidation_code
            )
            return DurableProposalResult(self._feedback(
                'rejected',
                'proposal',
                (
                    invalidation_code
                    if invalidated
                    else 'invalidation_unavailable'
                ),
                0,
            ))
        with self._lock:
            existing = self._proposals.get(stored.proposal_id)
            if existing is None:
                self._proposals[stored.proposal_id] = record
                handle = candidate
            elif (
                existing.source_digest == source_digest
                and existing.authority.binding_digest
                == authority.binding_digest
                and existing.plan_digest == plan_digest
            ):
                handle = existing.handle
            else:
                handle = None
            if stored.proposal_id in self._revoked_proposals:
                handle = None
        if handle is None:
            return DurableProposalResult(self._feedback(
                'rejected', 'proposal', 'proposal_conflict', 0,
            ))
        code = (
            'proposal_rehydrated'
            if rehydrating or stored.cached
            else 'mission_proposed'
        )
        status = 'confirmed' if stored.status == 'confirmed' else 'proposed'
        return DurableProposalResult(
            self._feedback(status, 'proposal', code, 0),
            handle,
        )

    def confirm(
        self,
        proposal: DurableMissionProposalHandle,
        confirmation_id: str,
    ) -> DurableMissionFeedback:
        """Resolve trusted evidence and atomically consume it once."""
        record = self._record_for_handle(proposal)
        if record is None:
            return self._feedback(
                'rejected', 'confirmation', 'authority_required', 0,
            )
        try:
            confirmation_key = _identifier(confirmation_id)
        except RoomMissionValidationError:
            return self._feedback(
                'rejected', 'confirmation', 'confirmation_invalid', 0,
            )
        evidence = None
        try:
            evidence = self._confirmation_resolver(confirmation_key)
        except Exception:
            pass
        evidence_valid = type(evidence) is DurableMissionConfirmation and (
            evidence.confirmation_id == confirmation_key
            and evidence.authority.binding_digest
            == record.authority.binding_digest
            and evidence.decision_id == record.decision_id
            and evidence.arguments_digest == record.arguments_digest
            and evidence.person_subject_id == record.authority.subject_id
        )
        invalidation_code = self._proposal_invalidation_code(record)
        if invalidation_code is not None:
            invalidated = self._invalidate_proposal(
                record, invalidation_code
            )
            if invalidated:
                return self._feedback(
                    'rejected',
                    'confirmation',
                    invalidation_code,
                    0,
                )
            if evidence_valid:
                raced = self._consume_confirmation(
                    proposal.proposal_id,
                    record,
                    evidence,
                )
                if raced is not None and raced.tool_call_id is not None:
                    return self._abort(
                        record,
                        raced.tool_call_id,
                        _execution_abort_code(invalidation_code),
                        None,
                    )
            return self._feedback(
                'rejected',
                'confirmation',
                'invalidation_unavailable',
                0,
            )
        if not evidence_valid:
            return self._feedback(
                'rejected', 'confirmation', 'confirmation_invalid', 0,
            )
        authorization = self._consume_confirmation(
            proposal.proposal_id, record, evidence
        )
        if authorization is None:
            return self._feedback(
                'rejected', 'confirmation', 'confirmation_unavailable', 0,
            )
        if authorization.status == 'timed_out':
            return self._feedback(
                'timed_out', 'confirmation', 'confirmation_expired', 0,
            )
        assert authorization.tool_call_id is not None
        final_invalidation = self._proposal_invalidation_code(record)
        if final_invalidation is not None:
            with self._lock:
                self._revoked_proposals.add(record.handle.proposal_id)
            return self._abort(
                record,
                authorization.tool_call_id,
                _execution_abort_code(final_invalidation),
                None,
            )
        return self._feedback(
            'confirmed',
            'confirmation',
            'mission_confirmed',
            1,
            authorization.tool_call_id,
        )

    def deny(
        self,
        proposal: DurableMissionProposalHandle,
    ) -> DurableMissionFeedback:
        """Durably consume an authenticated negative confirmation."""
        record = self._record_for_handle(proposal)
        if record is None:
            return self._feedback(
                'rejected', 'confirmation', 'authority_required', 0,
            )
        invalidation_code = self._proposal_invalidation_code(record)
        if invalidation_code is not None:
            invalidated = self._invalidate_proposal(
                record, invalidation_code
            )
            return self._feedback(
                'rejected',
                'confirmation',
                (
                    invalidation_code
                    if invalidated
                    else 'invalidation_unavailable'
                ),
                0,
            )
        try:
            stored = self._store.deny_proposal(
                proposal.proposal_id, record.authority
            )
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return self._feedback(
                'rejected', 'confirmation', 'denial_unavailable', 0,
            )
        if stored.status == 'timed_out':
            return self._feedback(
                'timed_out', 'confirmation', 'confirmation_expired', 0,
            )
        return self._feedback(
            'denied', 'confirmation', 'confirmation_denied', 0,
        )

    def execute(
        self,
        tool_call_id: str,
        proposal: DurableMissionProposalHandle,
    ) -> DurableMissionFeedback:
        """Execute only fresh, durably prepared fake-adapter intents."""
        record = self._authorized_execution(tool_call_id, proposal)
        if record is None:
            return self._feedback(
                'rejected', 'preflight', 'authority_required', 0,
            )
        with self._lock:
            if tool_call_id in self._executing:
                already_executing = True
            else:
                self._executing.add(tool_call_id)
                already_executing = False
        if already_executing:
            return self._read_feedback(record, tool_call_id)
        try:
            return self._execute_owned(record, tool_call_id)
        finally:
            with self._lock:
                self._executing.discard(tool_call_id)
                self._leases.pop(tool_call_id, None)

    def _execute_owned(
        self,
        record: _ProposalRecord,
        tool_call_id: str,
    ) -> DurableMissionFeedback:
        execution = self._read_execution(record, tool_call_id)
        if execution is None:
            return self._feedback(
                'rejected', 'preflight', 'authority_required', 0,
            )
        if execution.status in _TERMINAL_STATUSES:
            return self._execution_feedback(execution, record.authority)
        wall_now = self._safe_clock()
        if wall_now is not None and wall_now >= record.expires_at:
            try:
                expired_lease = self._store.claim_execution(
                    tool_call_id, record.authority, self._worker_id
                )
            except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
                return self._read_feedback(
                    record,
                    tool_call_id,
                    fallback_code='execution_unavailable',
                )
            self._set_lease(expired_lease)
            if expired_lease.recovery_required:
                return self._fail_unresolved(record, expired_lease)
            return self._read_feedback(record, tool_call_id)
        guard_code, _plan, _state = self._guard(record)
        if guard_code is not None:
            return self._abort(record, tool_call_id, guard_code, None)
        try:
            lease = self._store.claim_execution(
                tool_call_id, record.authority, self._worker_id
            )
        except RoomMissionLedgerBusyError:
            return self._read_feedback(record, tool_call_id)
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return self._read_feedback(
                record, tool_call_id, fallback_code='execution_unavailable'
            )
        self._set_lease(lease)
        if lease.recovery_required:
            return self._fail_unresolved(record, lease)
        execution = self._read_execution(record, tool_call_id)
        if execution is None:
            return self._feedback(
                'failed', 'terminal', 'ledger_unavailable', 0,
                tool_call_id,
            )
        start_index = _next_phase_index(execution)
        if start_index is None:
            return self._read_feedback(
                record, tool_call_id, fallback_code='execution_unavailable'
            )
        context = MissionExecutionContext(
            tool_call_id=tool_call_id,
            proposal_id=record.handle.proposal_id,
        )
        for phase in _EXECUTABLE_PHASES[start_index:]:
            guard_code, plan, state = self._guard(record)
            if guard_code is not None:
                return self._abort(
                    record, tool_call_id, guard_code, lease
                )
            assert plan is not None
            assert state is not None
            try:
                lease = self._store.renew_lease(lease)
                self._set_lease(lease)
                intent = self._store.prepare_phase(lease, phase)
            except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
                return self._read_feedback(
                    record,
                    tool_call_id,
                    fallback_code='execution_unavailable',
                )
            if (
                type(intent) is not PhaseIntent
                or intent.cached is not False
                or lease.recovery_required is not False
            ):
                return self._fail_unresolved(record, lease)
            dispatch_guard, checked_plan, checked_state = self._guard(record)
            if dispatch_guard is not None:
                return self._abort(
                    record, tool_call_id, dispatch_guard, lease
                )
            if checked_plan != plan or checked_state is None:
                return self._abort(
                    record, tool_call_id, 'map_changed', lease
                )
            method, arguments, timeout = self._phase_call(
                phase, context, checked_plan, checked_state
            )
            if not self._store_identity_is_current():
                return self._fail_unresolved(record, lease)
            outcome, hung, lease = self._bounded_adapter_call(
                method,
                arguments,
                timeout,
                lease,
                (tool_call_id, phase),
            )
            self._set_lease(lease)
            if hung or outcome is None:
                return self._fail_unresolved(record, lease)
            result_guard, _checked_plan, _checked_state = self._guard(record)
            if result_guard is not None:
                return self._abort(
                    record, tool_call_id, result_guard, lease
                )
            try:
                execution = self._store.record_phase_result(
                    lease, intent, outcome.status
                )
            except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
                return self._read_feedback(
                    record,
                    tool_call_id,
                    fallback_code='execution_unavailable',
                )
            if execution.status in _TERMINAL_STATUSES:
                return self._execution_feedback(
                    execution, record.authority
                )
        return self._read_feedback(record, tool_call_id)

    def cancel(
        self,
        tool_call_id: str,
        proposal: DurableMissionProposalHandle,
    ) -> DurableMissionFeedback:
        """Persist cancellation before any bounded fake cancel call."""
        record = self._authorized_execution(tool_call_id, proposal)
        if record is None:
            return self._feedback(
                'rejected', 'terminal', 'authority_required', 0,
            )
        with self._lock:
            if tool_call_id in self._cancelling:
                already_cancelling = True
            else:
                self._cancelling.add(tool_call_id)
                already_cancelling = False
        if already_cancelling:
            return self._read_feedback(record, tool_call_id)
        try:
            return self._cancel_owned(record, tool_call_id)
        finally:
            with self._lock:
                self._cancelling.discard(tool_call_id)

    def _cancel_owned(
        self,
        record: _ProposalRecord,
        tool_call_id: str,
    ) -> DurableMissionFeedback:
        """Run one locally serialized durable cancellation transition."""
        execution = self._read_execution(record, tool_call_id)
        if execution is None:
            return self._feedback(
                'rejected', 'terminal', 'authority_required', 0,
            )
        if execution.status in _TERMINAL_STATUSES:
            return self._execution_feedback(execution, record.authority)
        if (
            not self._record_source_is_current(record)
            or not self._authority_is_current(record.authority)
        ):
            return self._abort(
                record, tool_call_id, 'authority_revoked', None
            )
        plan_error, plan = self._current_plan(record)
        if plan_error is not None or plan is None:
            return self._abort(record, tool_call_id, 'map_changed', None)
        if not self._device_is_current(record):
            return self._abort(
                record, tool_call_id, 'device_unavailable', None
            )
        lease = self._get_lease(tool_call_id)
        if lease is None:
            try:
                lease = self._store.claim_execution(
                    tool_call_id, record.authority, self._worker_id
                )
                self._set_lease(lease)
            except RoomMissionLedgerBusyError:
                return self._persist_unclaimed_cancel(record, tool_call_id)
            except (
                RoomMissionLedgerError,
                RoomMissionLedgerValidationError,
            ):
                return self._read_feedback(
                    record,
                    tool_call_id,
                    fallback_code='cancellation_unavailable',
                )
        try:
            request = self._store.request_cancel(
                tool_call_id,
                record.authority,
                self._worker_id,
                current_lease=lease,
            )
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return self._read_feedback(
                record,
                tool_call_id,
                fallback_code='cancellation_unavailable',
            )
        dispatchable = (
            type(request.intent) is CancelIntent
            and request.intent.cached is False
            and request.lease is not None
            and request.pending_lease is False
            and lease.recovery_required is False
        )
        if not dispatchable:
            return self._fail_unresolved(
                record, lease, defer_local_cancel=False
            )
        if (
            not self._record_source_is_current(record)
            or not self._authority_is_current(record.authority)
        ):
            return self._abort(
                record,
                tool_call_id,
                'authority_revoked',
                lease,
                defer_local_cancel=False,
            )
        final_plan_error, final_plan = self._current_plan(record)
        if final_plan_error is not None or final_plan != plan:
            return self._abort(
                record,
                tool_call_id,
                'map_changed',
                lease,
                defer_local_cancel=False,
            )
        if not self._device_is_current(record):
            return self._abort(
                record,
                tool_call_id,
                'device_unavailable',
                lease,
                defer_local_cancel=False,
            )
        context = MissionExecutionContext(
            tool_call_id=tool_call_id,
            proposal_id=record.handle.proposal_id,
        )
        if not self._store_identity_is_current():
            return self._fail_unresolved(
                record, lease, defer_local_cancel=False
            )
        outcome, hung, lease = self._bounded_adapter_call(
            _sealed_simulation_step,
            (
                context,
                'cancel',
                self._adapter_fail_phase,
                self._adapter_timeout_phase,
            ),
            self._cancellation_timeout_seconds,
            lease,
            (tool_call_id, 'cancel'),
        )
        self._set_lease(lease)
        if hung or outcome is None:
            return self._fail_cancel_intent(
                record, lease, request.intent
            )
        try:
            execution = self._store.record_cancel_result(
                lease, request.intent, outcome.status
            )
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return self._read_feedback(
                record,
                tool_call_id,
                fallback_code='cancellation_unavailable',
            )
        return self._execution_feedback(execution, record.authority)

    def feedback(
        self,
        tool_call_id: str,
        proposal: DurableMissionProposalHandle,
    ) -> DurableMissionFeedback:
        """Read only content-free owner-bound durable state."""
        record = self._authorized_execution(tool_call_id, proposal)
        if record is None:
            return self._feedback(
                'rejected', 'terminal', 'authority_required', 0,
            )
        if (
            not self._record_source_is_current(record)
            or not self._authority_is_current(record.authority)
        ):
            return self._feedback(
                'rejected', 'terminal', 'authority_revoked', 0,
            )
        return self._read_feedback(record, tool_call_id)

    def _persist_unclaimed_cancel(
        self,
        record: _ProposalRecord,
        tool_call_id: str,
    ) -> DurableMissionFeedback:
        try:
            self._store.request_cancel(
                tool_call_id, record.authority, self._worker_id
            )
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            pass
        return self._read_feedback(
            record, tool_call_id, fallback_code='cancellation_pending'
        )

    def _fail_unresolved(
        self,
        record: _ProposalRecord,
        lease: ExecutionLease,
        *,
        defer_local_cancel: bool = True,
    ) -> DurableMissionFeedback:
        execution = self._read_execution(record, lease.tool_call_id)
        if execution is None:
            return self._feedback(
                'failed', 'terminal', 'ledger_unavailable', 0,
                lease.tool_call_id,
            )
        if execution.status in _TERMINAL_STATUSES:
            return self._execution_feedback(execution, record.authority)
        if execution.cancel_requested:
            with self._lock:
                cancel_call_inflight = (
                    lease.tool_call_id in self._cancelling
                )
            if cancel_call_inflight and defer_local_cancel:
                return self._execution_feedback(
                    execution, record.authority
                )
        try:
            if (
                not execution.cancel_requested
                and execution.active_operation_id is not None
                and execution.status != 'reconcile_required'
            ):
                self._store.abort_execution(
                    lease.tool_call_id,
                    record.authority,
                    'device_unavailable',
                    current_lease=lease,
                )
            if execution.cancel_requested:
                intent = self._store.get_cancel_intent(lease)
            else:
                intent = self._store.get_recovery_intent(lease)
            result = self._store.fail_reconciliation(lease, intent)
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return self._recover_after_lost_lease(
                record, lease.tool_call_id
            )
        return self._execution_feedback(result, record.authority)

    def _recover_after_lost_lease(
        self,
        record: _ProposalRecord,
        tool_call_id: str,
    ) -> DurableMissionFeedback:
        """Claim expired unresolved work without trusting a stale result."""
        try:
            recovery_lease = self._store.claim_execution(
                tool_call_id, record.authority, self._worker_id
            )
        except RoomMissionLedgerBusyError:
            return self._read_feedback(record, tool_call_id)
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return self._read_feedback(
                record,
                tool_call_id,
                fallback_code='recovery_unavailable',
            )
        self._set_lease(recovery_lease)
        if not recovery_lease.recovery_required:
            return self._read_feedback(record, tool_call_id)
        return self._fail_unresolved(
            record,
            recovery_lease,
            defer_local_cancel=False,
        )

    def _fail_cancel_intent(
        self,
        record: _ProposalRecord,
        lease: ExecutionLease,
        intent: CancelIntent,
    ) -> DurableMissionFeedback:
        try:
            result = self._store.fail_reconciliation(lease, intent)
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return self._recover_after_lost_lease(
                record, lease.tool_call_id
            )
        return self._execution_feedback(result, record.authority)

    def _abort(
        self,
        record: _ProposalRecord,
        tool_call_id: str,
        code: str,
        lease: Optional[ExecutionLease],
        *,
        defer_local_cancel: bool = True,
    ) -> DurableMissionFeedback:
        try:
            result = self._store.abort_execution(
                tool_call_id,
                record.authority,
                code,
                current_lease=lease,
            )
        except RoomMissionLedgerBusyError:
            return self._read_feedback(record, tool_call_id)
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return self._read_feedback(
                record, tool_call_id, fallback_code='execution_unavailable'
            )
        if result.status in _TERMINAL_STATUSES:
            return self._execution_feedback(result, record.authority)
        recovery_lease = lease
        if recovery_lease is None:
            try:
                recovery_lease = self._store.claim_execution(
                    tool_call_id, record.authority, self._worker_id
                )
            except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
                return self._execution_feedback(result, record.authority)
        self._set_lease(recovery_lease)
        return self._fail_unresolved(
            record,
            recovery_lease,
            defer_local_cancel=defer_local_cancel,
        )

    def _guard(
        self,
        record: _ProposalRecord,
    ) -> Tuple[
        Optional[str], Optional[RoomMissionPlan], Optional[TrustedMissionState]
    ]:
        if not self._record_source_is_current(record):
            return 'authority_revoked', None, None
        if not self._authority_is_current(record.authority):
            return 'authority_revoked', None, None
        if not self._record_source_is_current(record):
            return 'authority_revoked', None, None
        if not self._device_is_current(record):
            return 'device_unavailable', None, None
        plan_error, plan = self._current_plan(record)
        if plan_error is not None or plan is None:
            return 'map_changed', None, None
        state = None
        try:
            state = self._state_resolver(record.authority, plan)
        except Exception:
            pass
        if not self._record_source_is_current(record):
            return 'authority_revoked', None, None
        if type(state) is not TrustedMissionState:
            return 'state_unavailable', None, None
        state_valid = False
        try:
            state_valid = self._state_validator(
                state, record.authority, plan
            ) is True
        except Exception:
            pass
        if not state_valid:
            return 'state_unavailable', None, None
        if not self._record_source_is_current(record):
            return 'authority_revoked', None, None
        final_plan_error, final_plan = self._current_plan(record)
        if final_plan_error is not None or final_plan != plan:
            return 'map_changed', None, None
        state_error = self._validate_state(final_plan, state)
        if state_error is not None:
            return state_error, None, None
        with self._lock:
            poisoned = self._adapter_poisoned
        if poisoned:
            return 'device_unavailable', None, None
        if (
            not self._record_source_is_current(record)
            or not self._authority_is_current(record.authority)
        ):
            return 'authority_revoked', None, None
        last_plan_error, last_plan = self._current_plan(record)
        if last_plan_error is not None or last_plan != final_plan:
            return 'map_changed', None, None
        if not self._device_is_current(record):
            return 'device_unavailable', None, None
        if not self._record_source_is_current(record):
            return 'authority_revoked', None, None
        if not self._authority_is_current(record.authority):
            return 'authority_revoked', None, None
        if not self._record_source_is_current(record):
            return 'authority_revoked', None, None
        return None, final_plan, state

    def _proposal_invalidation_code(
        self,
        record: _ProposalRecord,
    ) -> Optional[str]:
        """Return one typed pre-confirm invalidation reason."""
        if not self._record_source_is_current(record):
            return 'source_changed'
        if not self._authority_is_current(record.authority):
            return 'authority_revoked'
        if not self._record_source_is_current(record):
            return 'source_changed'
        plan_error, _plan = self._current_plan(record)
        if plan_error is not None:
            return 'map_changed'
        if not self._device_is_current(record):
            return 'device_changed'
        if not self._record_source_is_current(record):
            return 'source_changed'
        if not self._authority_is_current(record.authority):
            return 'authority_revoked'
        if not self._record_source_is_current(record):
            return 'source_changed'
        if self._current_plan(record)[0] is not None:
            return 'map_changed'
        if not self._device_is_current(record):
            return 'device_changed'
        return None

    def _device_is_current(self, record: _ProposalRecord) -> bool:
        return (
            type(self._device_binding) is SimulationDeviceBinding
            and self._device_binding.device_id == record.device_id
            and self._device_binding.device_binding_digest
            == record.device_binding_digest
            and self._device_id == record.device_id
            and self._device_binding_digest
            == record.device_binding_digest
        )

    def _invalidate_proposal(
        self,
        record: _ProposalRecord,
        code: str,
    ) -> bool:
        """Persist a typed system invalidation and fence the local handle."""
        with self._lock:
            self._revoked_proposals.add(record.handle.proposal_id)
        try:
            stored = self._store.invalidate_proposal(
                record.handle.proposal_id,
                record.authority,
                code,
            )
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return False
        return stored.status == 'failed'

    def _abort_confirmed_invalidation(
        self,
        record: _ProposalRecord,
        code: str,
    ) -> DurableMissionFeedback:
        """Terminalize the one owner-bound confirmed restart candidate."""
        with self._lock:
            self._revoked_proposals.add(record.handle.proposal_id)
        try:
            candidates = tuple(
                candidate
                for candidate in self._store.list_recovery_candidates(
                    record.authority
                )
                if candidate.device_id == record.device_id
            )
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            candidates = ()
        if len(candidates) != 1:
            return self._feedback(
                'rejected',
                'proposal',
                'invalidation_unavailable',
                0,
            )
        return self._abort(
            record,
            candidates[0].tool_call_id,
            _execution_abort_code(code),
            None,
        )

    def _consume_confirmation(
        self,
        proposal_id: str,
        record: _ProposalRecord,
        evidence: DurableMissionConfirmation,
    ) -> Optional[StoredMissionAuthorization]:
        """Return one sanitized durable confirmation result."""
        try:
            return self._store.consume_confirmation(
                proposal_id, record.authority, evidence
            )
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return None

    def _validate_state(
        self,
        plan: RoomMissionPlan,
        state: TrustedMissionState,
    ) -> Optional[str]:
        now = self._safe_clock()
        if now is None:
            return 'state_unavailable'
        age = now - float(state.observed_at)
        if (
            age < -MAX_CLOCK_SKEW_SECONDS
            or age > self._max_state_age_seconds
        ):
            return 'state_stale'
        if (
            state.map_id != plan.map_id
            or state.map_revision != plan.map_revision
        ):
            return 'map_changed'
        if state.emergency_stop:
            return 'emergency_stop'
        if state.privacy_mode:
            return 'privacy_blocked'
        if not all((
            state.navigation_available,
            state.localization_ok,
            state.camera_available,
            state.stream_available,
        )):
            return 'device_unavailable'
        return None

    def _phase_call(
        self,
        phase: str,
        context: MissionExecutionContext,
        plan: RoomMissionPlan,
        state: TrustedMissionState,
    ) -> Tuple[Callable[..., AdapterStepResult], Tuple[Any, ...], float]:
        del plan, state
        arguments = (
            context,
            phase,
            self._adapter_fail_phase,
            self._adapter_timeout_phase,
        )
        if phase == 'preflight':
            return (
                _sealed_simulation_step,
                arguments,
                self._adapter_timeout_seconds,
            )
        if phase == 'navigating':
            return (
                _sealed_simulation_step,
                arguments,
                self._adapter_timeout_seconds,
            )
        if phase == 'coverage':
            return (
                _sealed_simulation_step,
                arguments,
                self._adapter_timeout_seconds,
            )
        return (
            _sealed_simulation_step,
            arguments,
            self._stream_timeout_seconds,
        )

    def _bounded_adapter_call(
        self,
        method: Callable[..., AdapterStepResult],
        arguments: Tuple[Any, ...],
        timeout_seconds: float,
        lease: ExecutionLease,
        simulation_call: Optional[Tuple[str, str]] = None,
    ) -> Tuple[Optional[AdapterStepResult], bool, ExecutionLease]:
        completed = threading.Event()
        result_box: Dict[str, Any] = {}

        def invoke() -> None:
            try:
                if simulation_call is not None:
                    with self._simulation_calls_lock:
                        self._simulation_calls.append(simulation_call)
                result_box['result'] = method(*arguments)
            except BaseException:
                result_box['failed'] = True
            finally:
                completed.set()

        worker = threading.Thread(
            target=invoke,
            name='durable-room-simulation-adapter',
            daemon=True,
        )
        started = False
        try:
            worker.start()
            started = True
        except Exception:
            pass
        if not started:
            return None, False, lease
        start = self._safe_monotonic()
        if start is None:
            self._poison_adapter()
            return None, False, lease
        deadline = start + timeout_seconds
        hard_deadline = time.monotonic() + timeout_seconds
        current_lease = lease
        while True:
            now = self._safe_monotonic()
            if now is None:
                self._poison_adapter()
                return None, False, current_lease
            remaining = min(
                deadline - now,
                hard_deadline - time.monotonic(),
            )
            if remaining <= 0:
                self._poison_adapter()
                return None, True, current_lease
            wait_budget = self._lease_wait_budget(
                current_lease, remaining
            )
            if wait_budget is None:
                self._poison_adapter()
                return None, False, current_lease
            if completed.wait(wait_budget):
                break
            try:
                current_lease = self._store.renew_lease(current_lease)
                self._set_lease(current_lease)
            except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
                self._poison_adapter()
                return None, False, current_lease
        result = result_box.get('result')
        if type(result) is not AdapterStepResult:
            return None, False, current_lease
        return result, False, current_lease

    def _lease_wait_budget(
        self,
        lease: ExecutionLease,
        adapter_remaining: float,
    ) -> Optional[float]:
        """Wake with margin before the durable lease can expire."""
        wall_now = self._safe_clock()
        if wall_now is None:
            return None
        lease_remaining = float(lease.expires_at) - wall_now
        if lease_remaining <= 0:
            return 0.0
        margin = min(0.02, max(0.005, lease_remaining * 0.4))
        safe_window = max(0.0, lease_remaining - margin)
        return min(
            adapter_remaining,
            0.02,
            max(0.001, safe_window * 0.5),
        )

    def _current_plan(
        self,
        record: _ProposalRecord,
    ) -> Tuple[Optional[str], Optional[RoomMissionPlan]]:
        plan = None
        try:
            plan = self._resolve_plan(record.location)
            digest = _plan_digest(plan)
        except Exception:
            digest = None
        if (
            plan is None
            or digest != record.plan_digest
            or plan.map_id != self._resolver_map_id
            or plan.map_revision != self._resolver_map_revision
        ):
            return 'map_changed', None
        return None, plan

    def _resolve_plan(self, location: Any) -> RoomMissionPlan:
        """Resolve only against the controller-owned immutable snapshot."""
        normalized = _normalize_location(location)
        plan = self._resolver_plans.get(normalized)
        if type(plan) is not RoomMissionPlan:
            raise RoomMissionValidationError(
                'semantic room is unavailable'
            )
        return plan

    def _record_for_handle(
        self,
        handle: Any,
    ) -> Optional[_ProposalRecord]:
        if type(handle) is not DurableMissionProposalHandle:
            return None
        with self._lock:
            record = self._proposals.get(handle.proposal_id)
            if (
                record is None
                or record.handle is not handle
                or handle.proposal_id in self._revoked_proposals
            ):
                return None
            return record

    def _authorized_execution(
        self,
        tool_call_id: Any,
        handle: Any,
    ) -> Optional[_ProposalRecord]:
        record = self._record_for_handle(handle)
        if record is None:
            return None
        try:
            _identifier(tool_call_id)
            execution = self._store.get_execution(
                tool_call_id, record.authority
            )
        except (
            RoomMissionValidationError,
            RoomMissionLedgerError,
            RoomMissionLedgerValidationError,
        ):
            return None
        if execution.tool_call_id != tool_call_id:
            return None
        return record

    def _authority_is_current(
        self,
        authority: DurableMissionAuthority,
    ) -> bool:
        try:
            return self._authority_validator(authority) is True
        except Exception:
            return False

    def _source_matches(
        self,
        source: OrchestrationResult,
        source_digest: str,
        authority_digest: str,
    ) -> bool:
        snapshot = _snapshot_orchestration(source)
        if snapshot is None or _json_digest(snapshot) != source_digest:
            return False
        try:
            live_authority_digest = orchestration_authority_digest(source)
        except Exception:
            return False
        return live_authority_digest == authority_digest

    def _record_source_is_current(self, record: _ProposalRecord) -> bool:
        return self._source_matches(
            record.source,
            record.source_digest,
            record.authority.authority_digest,
        )

    def _read_execution(
        self,
        record: _ProposalRecord,
        tool_call_id: str,
    ) -> Optional[StoredMissionExecution]:
        try:
            return self._store.get_execution(
                tool_call_id, record.authority
            )
        except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
            return None

    def _read_feedback(
        self,
        record: _ProposalRecord,
        tool_call_id: str,
        *,
        fallback_code: str = 'execution_busy',
    ) -> DurableMissionFeedback:
        execution = self._read_execution(record, tool_call_id)
        if execution is None:
            return self._feedback(
                'failed', 'terminal', fallback_code, 0, tool_call_id
            )
        return self._execution_feedback(execution, record.authority)

    def _execution_feedback(
        self,
        execution: StoredMissionExecution,
        authority: DurableMissionAuthority,
    ) -> DurableMissionFeedback:
        source = None
        ledger_current = True
        if execution.status in _TERMINAL_STATUSES:
            try:
                events = self._store.list_events(
                    execution.tool_call_id, authority
                )
                terminal_events = tuple(
                    event for event in events
                    if event.event_kind == 'terminal'
                )
                if terminal_events:
                    source = terminal_events[-1].source
            except (RoomMissionLedgerError, RoomMissionLedgerValidationError):
                ledger_current = False
            if source is None:
                ledger_current = False
        if not self._store_identity_is_current():
            ledger_current = False
        if not ledger_current:
            return self._feedback(
                'failed',
                'terminal',
                'ledger_unavailable',
                execution.state_revision,
                execution.tool_call_id,
            )
        return DurableMissionFeedback(
            status=execution.status,
            phase=execution.phase,
            code=execution.code,
            sequence=execution.state_revision,
            tool_call_id=execution.tool_call_id,
            terminal_source=source,
            durability=execution.durability,
            lease_scope=execution.lease_scope,
        )

    def _feedback(
        self,
        status: str,
        phase: str,
        code: str,
        sequence: int,
        tool_call_id: Optional[str] = None,
    ) -> DurableMissionFeedback:
        return DurableMissionFeedback(
            status=status,
            phase=phase,
            code=code,
            sequence=sequence,
            tool_call_id=tool_call_id,
            durability=self._durability,
            lease_scope=self._lease_scope,
        )

    def _set_lease(self, lease: ExecutionLease) -> None:
        with self._lock:
            self._leases[lease.tool_call_id] = lease

    def _get_lease(self, tool_call_id: str) -> Optional[ExecutionLease]:
        with self._lock:
            return self._leases.get(tool_call_id)

    def _poison_adapter(self) -> None:
        """Fence later dispatch after an uncertain local worker result."""
        with self._lock:
            _set_controller_poisoned(self)

    def _store_identity_is_current(self) -> bool:
        """Reattest the fixed durable device immediately before dispatch."""
        try:
            return self._store.assert_durable_identity() is None
        except Exception:
            return False

    def _safe_clock(self) -> Optional[float]:
        try:
            value = self._clock()
        except Exception:
            return None
        if (
            type(value) not in {int, float}
            or not math.isfinite(float(value))
        ):
            return None
        return float(value)

    def _safe_monotonic(self) -> Optional[float]:
        try:
            value = self._monotonic_clock()
        except Exception:
            return None
        if (
            type(value) not in {int, float}
            or not math.isfinite(float(value))
        ):
            return None
        return float(value)


def _valid_timeout(value: Any) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(float(value))
        and 0 < float(value) <= 120.0
    )


def _snapshot_orchestration(
    result: Any,
) -> Optional[Dict[str, Any]]:
    if type(result) is not OrchestrationResult:
        return None
    payload = None
    try:
        payload = {
            'request_id': result.request_id,
            'conversation_id': result.conversation_id,
            'turn_id': result.turn_id,
            'conversation_generation': result.conversation_generation,
            'conversation_revision': result.conversation_revision,
            'conversation_ordinal': result.conversation_ordinal,
            'decision_id': result.decision_id,
            'issued_at': result.issued_at,
            'expires_at': result.expires_at,
            'state_trusted': result.state_trusted,
            'memory_revision': result.memory_revision,
            'raw_decision': result.raw_decision.to_dict(),
            'decision': result.decision.to_dict(),
            'provider_decision': result.provider_result.decision.to_dict(),
            'safety': result.safety.to_dict(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
        payload = copy.deepcopy(json.loads(encoded.decode('utf-8')))
    except Exception:
        payload = None
    if type(payload) is not dict:
        return None
    return payload


def _valid_monitor_snapshot(
    snapshot: Dict[str, Any],
    now: float,
    *,
    require_current: bool,
) -> bool:
    try:
        decision = snapshot['decision']
        issued = snapshot['issued_at']
        expires = snapshot['expires_at']
        identifiers_valid = all(
            _identifier(snapshot[name]) == snapshot[name]
            for name in (
                'request_id', 'conversation_id', 'turn_id', 'decision_id'
            )
        )
        valid = (
            identifiers_valid
            and type(issued) in {int, float}
            and not isinstance(issued, bool)
            and math.isfinite(float(issued))
            and type(expires) in {int, float}
            and not isinstance(expires, bool)
            and math.isfinite(float(expires))
            and type(decision) is dict
            and snapshot['raw_decision'] == decision
            and snapshot['provider_decision'] == decision
            and decision['type'] == 'tool_call'
            and decision['tool_name'] == 'monitor_room'
            and type(decision['arguments']) is dict
            and set(decision['arguments']) == {'location'}
            and type(decision['arguments']['location']) is str
            and 1 <= len(decision['arguments']['location'].strip()) <= 64
            and type(decision['expires_in_ms']) is int
            and 1 <= decision['expires_in_ms'] <= 10000
            and snapshot['state_trusted'] is True
            and snapshot['safety']['allowed'] is True
            and snapshot['safety']['code'] == 'allowed'
            and all(
                type(snapshot[name]) is int and snapshot[name] >= 0
                for name in (
                    'conversation_generation',
                    'conversation_revision',
                    'conversation_ordinal',
                    'memory_revision',
                )
            )
            and float(issued) <= now + MAX_CLOCK_SKEW_SECONDS
            and float(issued) < float(expires)
            and (not require_current or now < float(expires))
            and float(expires) - float(issued)
            <= MAX_ACTION_TTL_SECONDS + 1e-9
            and float(expires)
            <= float(issued) + decision['expires_in_ms'] / 1000.0 + 1e-9
        )
    except (KeyError, TypeError, ValueError, RoomMissionValidationError):
        valid = False
    return valid


def _authority_matches(
    authority: DurableMissionAuthority,
    snapshot: Dict[str, Any],
    authority_digest: str,
) -> bool:
    return (
        authority.request_id == snapshot['request_id']
        and authority.conversation_id == snapshot['conversation_id']
        and authority.proposal_turn_id == snapshot['turn_id']
        and authority.conversation_generation
        == snapshot['conversation_generation']
        and authority.conversation_revision
        == snapshot['conversation_revision']
        and authority.conversation_ordinal
        == snapshot['conversation_ordinal']
        and authority.authority_digest == authority_digest
    )


def _next_phase_index(execution: StoredMissionExecution) -> Optional[int]:
    if execution.phase == 'confirmation' and execution.status == 'leased':
        return 0
    if (
        execution.phase in _EXECUTABLE_PHASES
        and execution.code == f'{execution.phase}_succeeded'
        and execution.active_operation_id is None
    ):
        index = _EXECUTABLE_PHASES.index(execution.phase) + 1
        return index if index < len(_EXECUTABLE_PHASES) else None
    return None


def _execution_abort_code(invalidation_code: str) -> str:
    """Map a pre-confirm invalidation to an execution abort code."""
    if invalidation_code == 'map_changed':
        return 'map_changed'
    if invalidation_code == 'device_changed':
        return 'device_unavailable'
    return 'authority_revoked'
