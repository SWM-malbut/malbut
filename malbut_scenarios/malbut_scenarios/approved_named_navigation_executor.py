"""
Simulation adapters for approved SWM25-130 named navigation.

The execution adapter deliberately owns only process-local opaque previews and
Robot Web sessions.  The application/repository layer must commit its durable
dispatch intent before calling :meth:`ApprovedNamedNavigationExecutor.start`.
Losing an ambiguous start result poisons that intent in this process: it is
reported as unknown on every later call and is never sent again.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import secrets
from threading import RLock
import time
from typing import Callable

from malbut_agent_server.ports.approved_navigation_executor import (
    ApprovedNavigationOutcomeUnknown,
    ApprovedNavigationRejected,
    ApprovedNavigationStatus,
)
from malbut_agent_server.robot_state_source import RobotStateEvidence
from malbut_agent_server.schemas import RobotState
from malbut_gazebo.named_navigation import NamedNavigationError
from malbut_gazebo.named_navigation_facade import (
    NamedNavigationExecution,
    NamedNavigationFacade,
    NamedNavigationFacadeError,
    PreparedNamedNavigation,
)
from malbut_gazebo.robot_web_navigation_client import (
    NavigationStatus,
    RobotWebHTTPError,
    RobotWebNavigationClient,
    RobotWebNavigationClientError,
    RobotWebOutcomeUnknown,
    RobotWebReadiness,
)


MAX_TRACKED_EXECUTIONS = 256


def _same_text(first: str, second: str) -> bool:
    return hmac.compare_digest(
        first.encode('utf-8'),
        second.encode('utf-8'),
    )


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise ApprovedNavigationRejected(f'invalid_{name}')
    return value


def _committed_intent_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(
            ord(character) < 33 or ord(character) > 126
            for character in value
        )
    ):
        raise ApprovedNavigationRejected('invalid_committed_intent_id')
    return value


def _known_failure_code(error: BaseException, fallback: str) -> str:
    code = getattr(error, 'code', None)
    if isinstance(error, RobotWebHTTPError):
        code = error.error_code
    if (
        not isinstance(code, str)
        or not code
        or len(code) > 128
        or any(
            ord(character) < 33 or ord(character) > 126
            for character in code
        )
    ):
        return fallback
    return code


class SimulationStateSourceError(RuntimeError):
    """A Robot Web snapshot cannot safely represent simulation state."""

    def __init__(self, code: str) -> None:
        """Create a bounded error without retaining a raw response."""
        self.code = code
        super().__init__(code)


class RobotWebSimulationStateSource:
    """Build fresh server-owned state from one exact simulation runtime."""

    def __init__(
        self,
        client: RobotWebNavigationClient,
        *,
        expected_device_id: str,
        expected_map_id: str,
        expected_map_revision: str,
        assumed_battery_percent: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Require exact identity and an explicit simulation battery value."""
        if not isinstance(client, RobotWebNavigationClient):
            raise TypeError('Robot Web navigation client is required')
        for name, value, maximum in (
            ('device_id', expected_device_id, 128),
            ('map_id', expected_map_id, 256),
            ('map_revision', expected_map_revision, 256),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or any(ord(character) < 33 for character in value)
            ):
                raise ValueError(f'expected {name} is invalid')
        if (
            isinstance(assumed_battery_percent, bool)
            or not isinstance(assumed_battery_percent, (int, float))
            or not math.isfinite(float(assumed_battery_percent))
            or not 0.0 <= float(assumed_battery_percent) <= 100.0
        ):
            raise ValueError('simulation battery assumption is invalid')
        if not callable(clock):
            raise TypeError('clock must be callable')
        self._client = client
        self._expected_device_id = expected_device_id
        self._expected_map_id = expected_map_id
        self._expected_map_revision = expected_map_revision
        self._assumed_battery_percent = float(assumed_battery_percent)
        self._clock = clock
        self._sequence = 0
        self._lock = RLock()

    def __repr__(self) -> str:
        """Describe the assumption without exposing runtime identity."""
        return (
            'RobotWebSimulationStateSource('
            "battery_basis='explicit_simulation_assumption')"
        )

    def read(self) -> RobotStateEvidence:
        """Return a unique content-derived snapshot or fail closed."""
        with self._lock:
            request_started_at = self._read_clock()
            try:
                readiness = self._client.readiness()
            except RobotWebNavigationClientError as error:
                raise SimulationStateSourceError(
                    'robot_web_readiness_unavailable'
                ) from error
            if not isinstance(readiness, RobotWebReadiness):
                raise SimulationStateSourceError(
                    'malformed_robot_web_readiness'
                )
            if not readiness.simulation:
                raise SimulationStateSourceError(
                    'simulation_runtime_required'
                )
            if not readiness.matches_runtime(
                device_id=self._expected_device_id,
                map_id=self._expected_map_id,
                map_revision=self._expected_map_revision,
            ):
                raise SimulationStateSourceError(
                    'runtime_binding_mismatch'
                )
            received_at = self._read_clock()
            if received_at < request_started_at:
                raise SimulationStateSourceError('observation_clock_rollback')
            observed_at = (
                request_started_at
                - readiness.conservative_source_age_seconds()
            )
            if observed_at < 0.0:
                raise SimulationStateSourceError(
                    'observation_predates_clock_epoch'
                )
            self._sequence += 1
            evidence_material = json.dumps(
                {
                    'readiness_fingerprint': readiness.content_fingerprint(),
                    'observed_at': observed_at,
                    'sample_sequence': self._sequence,
                    'assumed_battery_percent': self._assumed_battery_percent,
                    'battery_basis': 'explicit_simulation_assumption',
                    'emergency_stop_basis': 'simulation_clear_assumption',
                },
                allow_nan=False,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')
            evidence_id = (
                'swm25-132-robot-web-sim-'
                + hashlib.sha256(evidence_material).hexdigest()
            )
            state = RobotState(
                battery_percent=self._assumed_battery_percent,
                navigation_available=readiness.ready_for_navigation,
                localization_ok=readiness.localization_ok,
                emergency_stop=False,
                camera_available=False,
                privacy_mode=True,
                docked=False,
            )
            return RobotStateEvidence(
                state=state,
                observed_at=observed_at,
                evidence_id=evidence_id,
                trusted=True,
            )

    def _read_clock(self) -> float:
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError) as error:
            raise SimulationStateSourceError(
                'invalid_observation_clock'
            ) from error
        if not math.isfinite(value) or value < 0.0:
            raise SimulationStateSourceError('invalid_observation_clock')
        return value


class PreparedExecutionRef:
    """Opaque process-local reference to one side-effect-free preview."""

    __slots__ = ('_owner', '_prepared_id')

    def __init__(self, owner: object, prepared_id: str) -> None:
        """Keep the cache identifier private and owner-bound."""
        self._owner = owner
        self._prepared_id = prepared_id

    def __repr__(self) -> str:
        """Render no opaque identifier or semantic target."""
        return "PreparedExecutionRef(state='prepared')"


class ExecutionHandleRef:
    """Opaque process-local reference to one accepted Robot Web session."""

    __slots__ = ('_owner', '_handle_id')

    def __init__(self, owner: object, handle_id: str) -> None:
        """Keep the Robot Web handle identifier private and owner-bound."""
        self._owner = owner
        self._handle_id = handle_id

    def __repr__(self) -> str:
        """Render no opaque identifier or Robot Web session."""
        return "ExecutionHandleRef(state='accepted')"


@dataclass
class _PreparedRecord:
    prepared: PreparedNamedNavigation
    reference: PreparedExecutionRef
    committed_intent_id: str | None = None
    state: str = 'prepared'
    handle: ExecutionHandleRef | None = None
    rejection_code: str | None = None
    unknown_cause_code: str | None = None


@dataclass(frozen=True)
class _ExecutionRecord:
    execution: NamedNavigationExecution
    handle: ExecutionHandleRef


class ApprovedNamedNavigationExecutor:
    """Invoke the simulation façade once for one committed dispatch intent."""

    def __init__(
        self,
        facade: NamedNavigationFacade,
        *,
        max_tracked_executions: int = MAX_TRACKED_EXECUTIONS,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """Create bounded process-local caches without doing Robot Web I/O."""
        if not isinstance(facade, NamedNavigationFacade):
            raise TypeError('NamedNavigationFacade is required')
        if (
            isinstance(max_tracked_executions, bool)
            or not isinstance(max_tracked_executions, int)
            or not 1 <= max_tracked_executions <= 1024
        ):
            raise ValueError('max_tracked_executions must be within [1, 1024]')
        if id_factory is not None and not callable(id_factory):
            raise TypeError('id_factory must be callable')
        self._facade = facade
        self._maximum = max_tracked_executions
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._owner = object()
        self._prepared: dict[str, _PreparedRecord] = {}
        self._handles: dict[str, _ExecutionRecord] = {}
        self._intents: dict[str, str] = {}
        self._lock = RLock()

    def __repr__(self) -> str:
        """Advertise only the non-physical runtime mode."""
        return 'ApprovedNamedNavigationExecutor(simulation=True)'

    def _new_id(self, existing: dict[str, object]) -> str:
        for _attempt in range(8):
            value = self._id_factory()
            if (
                isinstance(value, str)
                and 16 <= len(value) <= 128
                and all(33 <= ord(character) <= 126 for character in value)
                and value not in existing
            ):
                return value
        raise ApprovedNavigationRejected('opaque_id_unavailable')

    def _prepared_record(
        self,
        reference: PreparedExecutionRef,
    ) -> _PreparedRecord:
        if (
            not isinstance(reference, PreparedExecutionRef)
            or reference._owner is not self._owner
        ):
            raise ApprovedNavigationRejected('prepared_reference_invalid')
        record = self._prepared.get(reference._prepared_id)
        if record is None or record.reference is not reference:
            raise ApprovedNavigationRejected('prepared_reference_unknown')
        return record

    def _execution_record(
        self,
        handle: ExecutionHandleRef,
    ) -> _ExecutionRecord:
        if (
            not isinstance(handle, ExecutionHandleRef)
            or handle._owner is not self._owner
        ):
            raise ApprovedNavigationRejected('execution_handle_invalid')
        record = self._handles.get(handle._handle_id)
        if record is None or record.handle is not handle:
            raise ApprovedNavigationRejected('execution_handle_unknown')
        return record

    def prepare(
        self,
        location: str,
        expected_target_binding_digest: str,
    ) -> PreparedExecutionRef:
        """Create and bind a preview without causing an external effect."""
        expected_digest = _digest(
            expected_target_binding_digest,
            'target_binding_digest',
        )
        with self._lock:
            if len(self._prepared) >= self._maximum:
                raise ApprovedNavigationRejected('prepared_cache_full')
            try:
                prepared = self._facade.preview(location)
            except (
                NamedNavigationError,
                NamedNavigationFacadeError,
                RobotWebNavigationClientError,
                TypeError,
                ValueError,
            ) as error:
                raise ApprovedNavigationRejected(
                    _known_failure_code(error, 'prepare_rejected')
                ) from error
            if not isinstance(prepared, PreparedNamedNavigation):
                raise ApprovedNavigationRejected(
                    'malformed_prepared_navigation'
                )
            if not _same_text(
                prepared.target.binding_digest,
                expected_digest,
            ):
                raise ApprovedNavigationRejected('target_binding_mismatch')
            prepared_id = self._new_id(self._prepared)
            reference = PreparedExecutionRef(self._owner, prepared_id)
            self._prepared[prepared_id] = _PreparedRecord(
                prepared=prepared,
                reference=reference,
            )
            return reference

    def start(
        self,
        prepared: PreparedExecutionRef,
        *,
        committed_intent_id: str,
    ) -> ExecutionHandleRef:
        """Send one start after the caller provides its committed intent."""
        intent_id = _committed_intent_id(committed_intent_id)
        with self._lock:
            record = self._prepared_record(prepared)
            bound_prepared_id = self._intents.get(intent_id)
            if (
                bound_prepared_id is not None
                and bound_prepared_id != prepared._prepared_id
            ):
                raise ApprovedNavigationRejected('intent_binding_mismatch')
            if record.committed_intent_id is not None:
                if not _same_text(record.committed_intent_id, intent_id):
                    raise ApprovedNavigationRejected(
                        'prepared_intent_mismatch'
                    )
                if record.state == 'started' and record.handle is not None:
                    return record.handle
                if record.state == 'unknown':
                    raise ApprovedNavigationOutcomeUnknown(
                        'start',
                        record.unknown_cause_code or 'OUTCOME_UNKNOWN',
                    )
                if record.state == 'rejected':
                    raise ApprovedNavigationRejected(
                        record.rejection_code or 'start_rejected'
                    )
                raise ApprovedNavigationOutcomeUnknown(
                    'start',
                    'START_IN_PROGRESS',
                )

            record.committed_intent_id = intent_id
            record.state = 'starting'
            self._intents[intent_id] = prepared._prepared_id
            try:
                execution = self._facade.start(record.prepared)
            except RobotWebOutcomeUnknown as error:
                record.state = 'unknown'
                record.unknown_cause_code = error.cause_code
                raise ApprovedNavigationOutcomeUnknown(
                    'start',
                    error.cause_code,
                ) from error
            except (
                NamedNavigationError,
                NamedNavigationFacadeError,
                RobotWebHTTPError,
            ) as error:
                code = _known_failure_code(error, 'start_rejected')
                record.state = 'rejected'
                record.rejection_code = code
                raise ApprovedNavigationRejected(code) from error
            except (
                RobotWebNavigationClientError,
                TypeError,
                ValueError,
            ) as error:
                record.state = 'unknown'
                record.unknown_cause_code = _known_failure_code(
                    error,
                    'UNCLASSIFIED_START_FAILURE',
                )
                raise ApprovedNavigationOutcomeUnknown(
                    'start',
                    record.unknown_cause_code,
                ) from error
            except Exception as error:
                record.state = 'unknown'
                record.unknown_cause_code = 'UNCLASSIFIED_START_FAILURE'
                raise ApprovedNavigationOutcomeUnknown(
                    'start',
                    record.unknown_cause_code,
                ) from error
            if not isinstance(execution, NamedNavigationExecution):
                record.state = 'unknown'
                record.unknown_cause_code = 'MALFORMED_EXECUTION_HANDLE'
                raise ApprovedNavigationOutcomeUnknown(
                    'start',
                    record.unknown_cause_code,
                )
            if len(self._handles) >= self._maximum:
                record.state = 'unknown'
                record.unknown_cause_code = 'HANDLE_CACHE_FULL_AFTER_START'
                raise ApprovedNavigationOutcomeUnknown(
                    'start',
                    record.unknown_cause_code,
                )
            handle_id = self._new_id(self._handles)
            handle = ExecutionHandleRef(self._owner, handle_id)
            self._handles[handle_id] = _ExecutionRecord(
                execution=execution,
                handle=handle,
            )
            record.state = 'started'
            record.handle = handle
            return handle

    def status(self, handle: ExecutionHandleRef) -> ApprovedNavigationStatus:
        """Read one exact session without constructing or resending a goal."""
        with self._lock:
            record = self._execution_record(handle)
            try:
                status = self._facade.status(record.execution)
            except RobotWebOutcomeUnknown as error:
                raise ApprovedNavigationOutcomeUnknown(
                    'status',
                    error.cause_code,
                ) from error
            except Exception as error:
                raise ApprovedNavigationOutcomeUnknown(
                    'status',
                    _known_failure_code(error, 'STATUS_UNAVAILABLE'),
                ) from error
            if not isinstance(status, NavigationStatus):
                raise ApprovedNavigationOutcomeUnknown(
                    'status',
                    'MALFORMED_STATUS',
                )
            result_code = status.message_code
            if status.terminal and result_code is None:
                result_code = {
                    'succeeded': 'NAVIGATION_SUCCEEDED',
                    'canceled': 'NAVIGATION_CANCELED',
                    'failed': 'NAVIGATION_FAILED',
                }.get(status.state)
            try:
                return ApprovedNavigationStatus(
                    state=status.state,
                    terminal=status.terminal,
                    result_code=result_code,
                    progress_ratio=status.progress_ratio,
                )
            except (TypeError, ValueError) as error:
                raise ApprovedNavigationOutcomeUnknown(
                    'status',
                    'MALFORMED_STATUS',
                ) from error

    def release(self, prepared: PreparedExecutionRef) -> None:
        """
        Drop one completed/blocked process-local execution capability.

        This operation performs no Robot Web or ROS request.  The durable
        action/outbox remains the source of truth; a released opaque reference
        is permanently rejected by this executor instance.
        """
        with self._lock:
            record = self._prepared_record(prepared)
            if record.state == 'starting':
                raise ApprovedNavigationRejected(
                    'prepared_execution_still_starting'
                )
            if record.handle is not None:
                self._handles.pop(record.handle._handle_id, None)
            if record.committed_intent_id is not None:
                bound = self._intents.get(record.committed_intent_id)
                if bound == prepared._prepared_id:
                    self._intents.pop(record.committed_intent_id, None)
            self._prepared.pop(prepared._prepared_id, None)


__all__ = [
    'ApprovedNamedNavigationExecutor',
    'ExecutionHandleRef',
    'PreparedExecutionRef',
    'RobotWebSimulationStateSource',
    'SimulationStateSourceError',
]
