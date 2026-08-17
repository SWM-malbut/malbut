"""
Explicit, restart-safe Agent driver for a durably prepared Gazebo operation.

The conversational process never imports ROS or ``malbut_gazebo``.  It sends
only a closed command and opaque identities to one fixed, peer-authenticated
Unix-domain gateway.  No constructor starts a worker and no method drains a
queue in the background: every transition is requested explicitly by its
caller.

Request identities form a deterministic response-linked chain.  Repeating a
run from step zero after a lost response or process restart exact-replays all
completed gateway requests before it reaches the first new step.  The step
ordinal is part of every identity, so a new step still progresses when two
successive observations are byte-identical.

Cancellation is conservative.  One stable cancel request is retried after an
ambiguous transport failure.  If gateway recovery reports that no durable
cancel intent exists, the runner performs a fresh observation before deriving
another cancel request.  Once ``cancel_requested`` is visible, only ``drive``
is used to reconcile the already-recorded intent.

The public execution selector is only a confirmation identifier.  This module
never accepts an outbox ID, operation ID, claim fence, owner digest, or
dispatcher result as authority.  It rederives those values from the exact
conversation store's immutable prepared acknowledgement on every call.  This
also recovers after an acknowledgement commit whose HTTP response was lost.
"""

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from threading import RLock
import time
from typing import Any, Dict, Optional, Tuple
import weakref

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboExecutionOutboxError,
    GazeboPreparedExecutionAuthority,
    GazeboSimulationExecutionPolicy,
)
from malbut_agent_server.gazebo_monitor_room_gateway_client import (
    MAX_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS,
    GazeboMonitorRoomGatewayClient,
    GazeboMonitorRoomGatewayClientError,
    GazeboMonitorRoomGatewayResult,
)
from malbut_agent_server.schemas import ValidationError, validate_user_id


GAZEBO_COMMAND_RUNNER_SCHEMA_VERSION = 1
GAZEBO_COMMAND_RUNNER_DEFAULT_MAX_STEPS = 512
GAZEBO_COMMAND_RUNNER_MAX_STEPS = 32768
GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS = 30.0
GAZEBO_COMMAND_RUNNER_MAX_TIMEOUT_SECONDS = 3600.0

_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_ERROR_CODE = re.compile(r'^gazebo_command_runner_[a-z0-9_]{1,64}$')
_FLOWS = frozenset({'drive', 'observe', 'cancel'})
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
_TERMINAL_STATES = frozenset(
    {
        'delivery_unknown',
        'cancel_unknown',
        'succeeded',
        'failed',
        'canceled',
    }
)
_RUNNER_SEAL_LOCK = RLock()
_RUNNER_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)


class GazeboMonitorRoomCommandRunnerError(RuntimeError):
    """Content-free failure at the explicit Agent execution boundary."""

    def __init__(
        self,
        code: str = 'gazebo_command_runner_unavailable',
    ) -> None:
        """Expose a stable code without operation or transport details."""
        normalized = (
            code
            if type(code) is str and _ERROR_CODE.fullmatch(code) is not None
            else 'gazebo_command_runner_unavailable'
        )
        super().__init__('Gazebo command execution is unavailable')
        self.code = normalized

    def __getattribute__(self, name: str) -> Any:
        """Keep collaborator exceptions and private values out of chains."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


def _error(code: str) -> GazeboMonitorRoomCommandRunnerError:
    return GazeboMonitorRoomCommandRunnerError(code)


def _identifier(value: Any) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _error('gazebo_command_runner_request_invalid')
    return value


def _digest(value: Any) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _error('gazebo_command_runner_result_invalid')
    return value


def _canonical_json_bytes(value: Any) -> bytes:
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
    if invalid or not encoded or len(encoded) > 16384:
        raise _error('gazebo_command_runner_result_invalid')
    return encoded


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _monotonic() -> float:
    invalid = False
    value: Any = None
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
        raise _error('gazebo_command_runner_clock_unavailable')
    return float(value)


def _timeout(value: Any, *, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise _error('gazebo_command_runner_deadline_invalid')
    result = float(value)
    if not math.isfinite(result) or result <= 0.0 or result > maximum:
        raise _error('gazebo_command_runner_deadline_invalid')
    return result


def _step_limit(value: Any, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise _error('gazebo_command_runner_step_limit_invalid')
    return value


def _request_id(
    *,
    authorization_digest: str,
    operation_id: str,
    run_request_id: str,
    flow: str,
    command: str,
    step_index: int,
    previous_request_id: Optional[str],
    previous_response_fingerprint: str,
) -> str:
    digest = _hash_json(
        {
            'contract': 'gazebo-command-runner-request-v1',
            'authorization_digest': authorization_digest,
            'operation_id': operation_id,
            'run_request_id': run_request_id,
            'flow': flow,
            'command': command,
            'step_index': step_index,
            'previous_request_id': previous_request_id,
            'previous_response_fingerprint': (
                previous_response_fingerprint
            ),
        }
    )
    return f'gazebo-command-{digest}'


@dataclass(frozen=True, repr=False)
class GazeboMonitorRoomCommandStep:
    """One immutable response-linked, coordinate-free execution cursor."""

    outbox_id: str = field(repr=False)
    operation_id: str = field(repr=False)
    run_request_id: str = field(repr=False)
    authorization_digest: str = field(repr=False)
    flow: str
    request_id: str = field(repr=False)
    command: str
    step_index: int
    previous_request_id: Optional[str] = field(repr=False)
    previous_response_fingerprint: str = field(repr=False)
    state: str
    current_sample_index: int
    navigation_samples_total: int
    navigation_samples_reached: int
    terminal: bool
    robot_blocked: bool
    terminal_code: Optional[str]
    evidence_digest: str = field(repr=False)
    gateway_response_fingerprint: str = field(repr=False)
    previous_chain_digest: str = field(repr=False)
    schema_version: int = GAZEBO_COMMAND_RUNNER_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _chain_digest: str = field(init=False, repr=False)
    _result_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the gateway projection and cache the complete cursor."""
        GazeboMonitorRoomCommandStep._validate(self)
        object.__setattr__(
            self,
            '_chain_digest',
            GazeboMonitorRoomCommandStep._expected_chain_digest(self),
        )
        object.__setattr__(
            self,
            '_result_fingerprint',
            _hash_json(GazeboMonitorRoomCommandStep._private_values(self)),
        )

    def _gateway_result(self) -> GazeboMonitorRoomGatewayResult:
        return GazeboMonitorRoomGatewayResult(
            request_id=self.request_id,
            operation_id=self.operation_id,
            command=self.command,
            state=self.state,
            current_sample_index=self.current_sample_index,
            navigation_samples_total=self.navigation_samples_total,
            navigation_samples_reached=self.navigation_samples_reached,
            terminal=self.terminal,
            robot_blocked=self.robot_blocked,
            terminal_code=self.terminal_code,
            evidence_digest=self.evidence_digest,
        )

    def _validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != GAZEBO_COMMAND_RUNNER_SCHEMA_VERSION
            or type(self.outbox_id) is not str
            or not self.outbox_id.startswith('gazebo-execution-outbox-')
            or _IDENTIFIER.fullmatch(self.outbox_id) is None
            or type(self.operation_id) is not str
            or not self.operation_id.startswith('gazebo-operation-')
            or _IDENTIFIER.fullmatch(self.operation_id) is None
            or type(self.run_request_id) is not str
            or _IDENTIFIER.fullmatch(self.run_request_id) is None
            or self.flow not in _FLOWS
            or self.command not in _COMMANDS
            or type(self.step_index) is not int
            or not 0 <= self.step_index < GAZEBO_COMMAND_RUNNER_MAX_STEPS
            or self.state not in _NONTERMINAL_STATES | _TERMINAL_STATES
            or self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            raise _error('gazebo_command_runner_result_invalid')
        _digest(self.authorization_digest)
        _digest(self.previous_response_fingerprint)
        _digest(self.previous_chain_digest)
        _digest(self.evidence_digest)
        _digest(self.gateway_response_fingerprint)
        if self.previous_request_id is not None:
            _identifier(self.previous_request_id)
        expected_request_id = _request_id(
            authorization_digest=self.authorization_digest,
            operation_id=self.operation_id,
            run_request_id=self.run_request_id,
            flow=self.flow,
            command=self.command,
            step_index=self.step_index,
            previous_request_id=self.previous_request_id,
            previous_response_fingerprint=(
                self.previous_response_fingerprint
            ),
        )
        if self.request_id != expected_request_id:
            raise _error('gazebo_command_runner_result_invalid')
        if self.step_index == 0:
            if (
                self.previous_request_id is not None
                or self.previous_response_fingerprint
                != self.authorization_digest
                or self.previous_chain_digest != self.authorization_digest
            ):
                raise _error('gazebo_command_runner_result_invalid')
        elif self.previous_request_id is None:
            raise _error('gazebo_command_runner_result_invalid')
        if (
            (self.flow == 'drive' and self.command != 'drive')
            or (self.flow == 'observe' and self.command != 'observe')
            or (
                self.flow == 'cancel'
                and self.step_index == 0
                and self.command != 'cancel'
            )
        ):
            raise _error('gazebo_command_runner_result_invalid')
        invalid_gateway = False
        try:
            gateway = GazeboMonitorRoomCommandStep._gateway_result(self)
            if (
                gateway.response_fingerprint
                != self.gateway_response_fingerprint
            ):
                invalid_gateway = True
        except (GazeboMonitorRoomGatewayClientError, TypeError, ValueError):
            invalid_gateway = True
        if invalid_gateway:
            raise _error('gazebo_command_runner_result_invalid')

    def _expected_chain_digest(self) -> str:
        return _hash_json(
            {
                'contract': 'gazebo-command-runner-chain-v1',
                'authorization_digest': self.authorization_digest,
                'flow': self.flow,
                'request_id': self.request_id,
                'command': self.command,
                'step_index': self.step_index,
                'previous_chain_digest': self.previous_chain_digest,
                'gateway_response_fingerprint': (
                    self.gateway_response_fingerprint
                ),
            }
        )

    def _private_values(self) -> Dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'outbox_id': self.outbox_id,
            'operation_id': self.operation_id,
            'run_request_id': self.run_request_id,
            'authorization_digest': self.authorization_digest,
            'flow': self.flow,
            'request_id': self.request_id,
            'command': self.command,
            'step_index': self.step_index,
            'previous_request_id': self.previous_request_id,
            'previous_response_fingerprint': (
                self.previous_response_fingerprint
            ),
            'state': self.state,
            'current_sample_index': self.current_sample_index,
            'navigation_samples_total': self.navigation_samples_total,
            'navigation_samples_reached': (
                self.navigation_samples_reached
            ),
            'terminal': self.terminal,
            'robot_blocked': self.robot_blocked,
            'terminal_code': self.terminal_code,
            'evidence_digest': self.evidence_digest,
            'gateway_response_fingerprint': (
                self.gateway_response_fingerprint
            ),
            'previous_chain_digest': self.previous_chain_digest,
            'chain_digest': getattr(self, '_chain_digest', None),
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
        GazeboMonitorRoomCommandStep._validate(self)
        chain = GazeboMonitorRoomCommandStep._expected_chain_digest(self)
        if chain != getattr(self, '_chain_digest', None):
            raise _error('gazebo_command_runner_result_invalid')
        if (
            _hash_json(GazeboMonitorRoomCommandStep._private_values(self))
            != getattr(self, '_result_fingerprint', None)
        ):
            raise _error('gazebo_command_runner_result_invalid')

    @property
    def chain_digest(self) -> str:
        """Return the private continuation identity after re-attestation."""
        GazeboMonitorRoomCommandStep._attest(self)
        return self._chain_digest

    def to_public_dict(self) -> Dict[str, Any]:
        """Return progress without operation IDs, request IDs, or evidence."""
        GazeboMonitorRoomCommandStep._attest(self)
        return {
            'schema_version': self.schema_version,
            'flow': self.flow,
            'command': self.command,
            'step_index': self.step_index,
            'state': self.state,
            'current_sample_index': self.current_sample_index,
            'navigation_samples_total': self.navigation_samples_total,
            'navigation_samples_reached': (
                self.navigation_samples_reached
            ),
            'terminal': self.terminal,
            'robot_blocked': self.robot_blocked,
            'terminal_code': self.terminal_code,
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

    def __repr__(self) -> str:
        """Render only the same redacted progress exposed publicly."""
        public = GazeboMonitorRoomCommandStep.to_public_dict(self)
        return f'GazeboMonitorRoomCommandStep({public!r})'


@dataclass(frozen=True, repr=False)
class GazeboMonitorRoomCommandRun:
    """Bounded result of one explicit foreground drive or cancel loop."""

    flow: str
    stop_reason: str
    requests_made: int
    request_limit: int
    last_step: Optional[GazeboMonitorRoomCommandStep] = field(repr=False)
    schema_version: int = GAZEBO_COMMAND_RUNNER_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _result_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate and fingerprint one bounded foreground outcome."""
        GazeboMonitorRoomCommandRun._validate(self)
        object.__setattr__(
            self,
            '_result_fingerprint',
            _hash_json(GazeboMonitorRoomCommandRun._private_values(self)),
        )

    def _validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != GAZEBO_COMMAND_RUNNER_SCHEMA_VERSION
            or self.flow not in {'drive', 'cancel'}
            or self.stop_reason not in {
                'terminal',
                'step_limit',
                'deadline',
            }
            or type(self.requests_made) is not int
            or type(self.request_limit) is not int
            or not 0 <= self.requests_made <= self.request_limit
            or not 1 <= self.request_limit
            <= GAZEBO_COMMAND_RUNNER_MAX_STEPS
            or self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            raise _error('gazebo_command_runner_result_invalid')
        if self.last_step is not None:
            if type(self.last_step) is not GazeboMonitorRoomCommandStep:
                raise _error('gazebo_command_runner_result_invalid')
            GazeboMonitorRoomCommandStep._attest(self.last_step)
            if self.last_step.flow != self.flow:
                raise _error('gazebo_command_runner_result_invalid')
        if self.stop_reason == 'terminal':
            if self.last_step is None or self.last_step.terminal is not True:
                raise _error('gazebo_command_runner_result_invalid')
        elif self.last_step is not None and self.last_step.terminal:
            raise _error('gazebo_command_runner_result_invalid')
        if (
            self.stop_reason == 'step_limit'
            and self.requests_made != self.request_limit
        ):
            raise _error('gazebo_command_runner_result_invalid')

    def _private_values(self) -> Dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'flow': self.flow,
            'stop_reason': self.stop_reason,
            'requests_made': self.requests_made,
            'request_limit': self.request_limit,
            'last_step_fingerprint': (
                None
                if self.last_step is None
                else self.last_step._result_fingerprint
            ),
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
        GazeboMonitorRoomCommandRun._validate(self)
        if (
            _hash_json(GazeboMonitorRoomCommandRun._private_values(self))
            != getattr(self, '_result_fingerprint', None)
        ):
            raise _error('gazebo_command_runner_result_invalid')

    @property
    def terminal(self) -> bool:
        """Return whether the final trusted observation is terminal."""
        GazeboMonitorRoomCommandRun._attest(self)
        return self.stop_reason == 'terminal'

    def to_public_dict(self) -> Dict[str, Any]:
        """Return a redacted bounded-run summary and last progress value."""
        GazeboMonitorRoomCommandRun._attest(self)
        return {
            'schema_version': self.schema_version,
            'flow': self.flow,
            'stop_reason': self.stop_reason,
            'requests_made': self.requests_made,
            'request_limit': self.request_limit,
            'terminal': self.stop_reason == 'terminal',
            'last_observation': (
                None
                if self.last_step is None
                else self.last_step.to_public_dict()
            ),
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

    def __repr__(self) -> str:
        """Render only the redacted public summary."""
        public = GazeboMonitorRoomCommandRun.to_public_dict(self)
        return f'GazeboMonitorRoomCommandRun({public!r})'


def _canonical_authority(
    value: Any,
    confirmation_request_id: str,
) -> GazeboPreparedExecutionAuthority:
    """Detach one exact store-issued authority from mutable caller state."""
    invalid = False
    canonical: Any = None
    try:
        if type(value) is not GazeboPreparedExecutionAuthority:
            invalid = True
        else:
            first = (
                GazeboPreparedExecutionAuthority.binding_digest.fget(value)
            )
            values = {
                'confirmation_request_id': value.confirmation_request_id,
                'outbox_id': value.outbox_id,
                'operation_id': value.operation_id,
                'claim_fence': value.claim_fence,
                'owner_binding_digest': value.owner_binding_digest,
                'prepare_fingerprint': value.prepare_fingerprint,
                'acknowledgement_fingerprint': (
                    value.acknowledgement_fingerprint
                ),
                'host_boot_id': value.host_boot_id,
                'prepared_boottime_ns': value.prepared_boottime_ns,
                'deadline_boottime_ns': value.deadline_boottime_ns,
                'execution_scope': value.execution_scope,
                'schema_version': value.schema_version,
            }
            second = (
                GazeboPreparedExecutionAuthority.binding_digest.fget(value)
            )
            canonical = GazeboPreparedExecutionAuthority(**values)
            canonical_digest = (
                GazeboPreparedExecutionAuthority.binding_digest.fget(
                    canonical
                )
            )
            invalid = (
                first != second
                or canonical_digest != first
                or canonical.confirmation_request_id
                != confirmation_request_id
                or canonical.runtime_mode != 'gazebo'
                or canonical.simulation is not True
                or canonical.physical_authorized is not False
                or canonical.physical_effects is not False
                or canonical.viewer_live is not False
                or canonical.camera_coverage_validated is not False
                or canonical.coverage_achieved is not False
            )
    except (AttributeError, GazeboExecutionOutboxError, TypeError, ValueError):
        invalid = True
    if invalid:
        raise _error('gazebo_command_runner_prepared_invalid')
    return canonical


def _canonical_gateway_result(
    value: Any,
) -> GazeboMonitorRoomGatewayResult:
    """Fingerprint and detach a gateway response before later reads."""
    invalid = False
    canonical: Any = None
    try:
        if type(value) is not GazeboMonitorRoomGatewayResult:
            invalid = True
        else:
            first = (
                GazeboMonitorRoomGatewayResult.response_fingerprint.fget(
                    value
                )
            )
            values = GazeboMonitorRoomGatewayResult.to_dict(value)
            second = (
                GazeboMonitorRoomGatewayResult.response_fingerprint.fget(
                    value
                )
            )
            canonical = GazeboMonitorRoomGatewayResult._from_mapping(
                dict(values)
            )
            canonical_fingerprint = (
                GazeboMonitorRoomGatewayResult.response_fingerprint.fget(
                    canonical
                )
            )
            invalid = (
                first != second
                or canonical_fingerprint != first
                or values
                != GazeboMonitorRoomGatewayResult.to_dict(canonical)
            )
    except (
        AttributeError,
        GazeboMonitorRoomGatewayClientError,
        TypeError,
        ValueError,
    ):
        invalid = True
    if invalid:
        raise _error('gazebo_command_runner_gateway_invalid')
    return canonical


def _canonical_step(value: Any) -> GazeboMonitorRoomCommandStep:
    """Return a fresh cursor whose private fingerprint was stable to copy."""
    invalid = False
    canonical: Any = None
    try:
        if type(value) is not GazeboMonitorRoomCommandStep:
            invalid = True
        else:
            GazeboMonitorRoomCommandStep._attest(value)
            first_result = getattr(value, '_result_fingerprint', None)
            first_chain = getattr(value, '_chain_digest', None)
            values = {
                'outbox_id': value.outbox_id,
                'operation_id': value.operation_id,
                'run_request_id': value.run_request_id,
                'authorization_digest': value.authorization_digest,
                'flow': value.flow,
                'request_id': value.request_id,
                'command': value.command,
                'step_index': value.step_index,
                'previous_request_id': value.previous_request_id,
                'previous_response_fingerprint': (
                    value.previous_response_fingerprint
                ),
                'state': value.state,
                'current_sample_index': value.current_sample_index,
                'navigation_samples_total': (
                    value.navigation_samples_total
                ),
                'navigation_samples_reached': (
                    value.navigation_samples_reached
                ),
                'terminal': value.terminal,
                'robot_blocked': value.robot_blocked,
                'terminal_code': value.terminal_code,
                'evidence_digest': value.evidence_digest,
                'gateway_response_fingerprint': (
                    value.gateway_response_fingerprint
                ),
                'previous_chain_digest': value.previous_chain_digest,
                'schema_version': value.schema_version,
            }
            GazeboMonitorRoomCommandStep._attest(value)
            second_result = getattr(value, '_result_fingerprint', None)
            second_chain = getattr(value, '_chain_digest', None)
            canonical = GazeboMonitorRoomCommandStep(**values)
            GazeboMonitorRoomCommandStep._attest(canonical)
            invalid = (
                first_result != second_result
                or first_chain != second_chain
                or getattr(canonical, '_result_fingerprint', None)
                != first_result
                or getattr(canonical, '_chain_digest', None) != first_chain
            )
    except (
        AttributeError,
        GazeboMonitorRoomCommandRunnerError,
        TypeError,
        ValueError,
    ):
        invalid = True
    if invalid:
        raise _error('gazebo_command_runner_cursor_invalid')
    return canonical


class GazeboMonitorRoomCommandRunner:
    """Run explicit commands from one sealed durable store and local client."""

    def __init__(
        self,
        store: SQLiteConversationStore,
        client: GazeboMonitorRoomGatewayClient,
        *,
        user_id: str,
        max_steps: int = GAZEBO_COMMAND_RUNNER_MAX_STEPS,
    ) -> None:
        """Fix the durable authority root, endpoint, and foreground bound."""
        if (
            type(store) is not SQLiteConversationStore
            or type(client) is not GazeboMonitorRoomGatewayClient
            or type(getattr(store, '_gazebo_execution_policy', None))
            is not GazeboSimulationExecutionPolicy
        ):
            raise _error('gazebo_command_runner_configuration_invalid')
        try:
            normalized_user = validate_user_id(user_id)
        except ValidationError:
            raise _error(
                'gazebo_command_runner_configuration_invalid'
            ) from None
        try:
            GazeboMonitorRoomGatewayClient._attest_configuration(client)
        except GazeboMonitorRoomGatewayClientError:
            raise _error(
                'gazebo_command_runner_configuration_invalid'
            ) from None
        normalized_max = _step_limit(
            max_steps,
            maximum=GAZEBO_COMMAND_RUNNER_MAX_STEPS,
        )
        lock = RLock()
        store_connection = getattr(store, '_connection', None)
        store_lock = getattr(store, '_lock', None)
        store_policy = getattr(store, '_gazebo_execution_policy', None)
        self._store = store
        self._client = client
        self._user_id = normalized_user
        self._max_steps = normalized_max
        self._command_lock = lock
        self._configuration_seal = (
            store,
            store_connection,
            store_lock,
            store_policy,
            client,
            normalized_user,
            normalized_max,
            lock,
        )
        with _RUNNER_SEAL_LOCK:
            _RUNNER_SEALS[self] = self._configuration_seal

    @property
    def max_steps(self) -> int:
        """Return the fixed maximum commands accepted by one run call."""
        return self._max_steps

    def _attest_configuration(self) -> None:
        seal = getattr(self, '_configuration_seal', None)
        external = None
        try:
            with _RUNNER_SEAL_LOCK:
                external = _RUNNER_SEALS.get(self)
        except Exception:
            external = None
        if (
            type(self) is not GazeboMonitorRoomCommandRunner
            or type(seal) is not tuple
            or len(seal) != 8
            or external is None
            or external != seal
            or set(getattr(self, '__dict__', {}))
            != {
                '_store',
                '_client',
                '_user_id',
                '_max_steps',
                '_command_lock',
                '_configuration_seal',
            }
            or getattr(self, '_store', None) is not seal[0]
            or getattr(self._store, '_connection', None) is not seal[1]
            or getattr(self._store, '_lock', None) is not seal[2]
            or getattr(
                self._store, '_gazebo_execution_policy', None
            ) is not seal[3]
            or getattr(self, '_client', None) is not seal[4]
            or type(getattr(self, '_user_id', None)) is not str
            or getattr(self, '_user_id', None) != seal[5]
            or type(getattr(self, '_max_steps', None)) is not int
            or self._max_steps != seal[6]
            or getattr(self, '_command_lock', None) is not seal[7]
        ):
            raise _error('gazebo_command_runner_configuration_changed')
        try:
            GazeboMonitorRoomGatewayClient._attest_configuration(
                self._client
            )
        except GazeboMonitorRoomGatewayClientError:
            raise _error(
                'gazebo_command_runner_configuration_changed'
            ) from None

    def _resolve_authority(
        self,
        confirmation_request_id: str,
        flow: str,
    ) -> GazeboPreparedExecutionAuthority:
        """Rederive an exact prepared ACK without caller authority fields."""
        GazeboMonitorRoomCommandRunner._attest_configuration(self)
        try:
            SQLiteConversationStore.attest_command_boundary_durability(
                self._store
            )
            value = SQLiteConversationStore.resolve_prepared_gazebo_execution(
                self._store,
                confirmation_request_id=confirmation_request_id,
                expected_user_id=self._user_id,
                execution_scope=flow,
            )
        except GazeboExecutionOutboxError:
            raise _error('gazebo_command_runner_prepared_invalid') from None
        except Exception:
            raise _error('gazebo_command_runner_unavailable') from None
        GazeboMonitorRoomCommandRunner._attest_configuration(self)
        try:
            SQLiteConversationStore.attest_command_boundary_durability(
                self._store
            )
        except Exception:
            raise _error('gazebo_command_runner_unavailable') from None
        canonical = _canonical_authority(value, confirmation_request_id)
        if canonical.execution_scope != flow:
            raise _error('gazebo_command_runner_prepared_invalid')
        return canonical

    @staticmethod
    def _canonical_previous(
        authority: GazeboPreparedExecutionAuthority,
        run_request_id: str,
        flow: str,
        previous: Any,
    ) -> Optional[GazeboMonitorRoomCommandStep]:
        if previous is None:
            return None
        invalid = False
        canonical: Any = None
        try:
            canonical = _canonical_step(previous)
            invalid = (
                canonical.outbox_id != authority.outbox_id
                or canonical.operation_id != authority.operation_id
                or canonical.run_request_id != run_request_id
                or canonical.flow != flow
                or canonical.authorization_digest
                != authority.binding_digest
                or canonical.terminal
            )
        except GazeboMonitorRoomCommandRunnerError:
            invalid = True
        except GazeboExecutionOutboxError:
            invalid = True
        if invalid:
            raise _error('gazebo_command_runner_cursor_invalid')
        return canonical

    @staticmethod
    def _next_cancel_command(
        previous: Optional[GazeboMonitorRoomCommandStep],
    ) -> str:
        if previous is None:
            return 'cancel'
        if previous.state == 'cancel_requested':
            return 'drive'
        if previous.command == 'observe':
            return 'cancel'
        return 'observe'

    def _invoke_locked(
        self,
        *,
        authority: GazeboPreparedExecutionAuthority,
        run_request_id: str,
        flow: str,
        command: str,
        previous: Optional[GazeboMonitorRoomCommandStep],
        deadline: float,
    ) -> GazeboMonitorRoomCommandStep:
        GazeboMonitorRoomCommandRunner._attest_configuration(self)
        try:
            authorization = (
                GazeboPreparedExecutionAuthority.binding_digest.fget(
                    authority
                )
            )
        except GazeboExecutionOutboxError:
            raise _error('gazebo_command_runner_prepared_invalid') from None
        if (
            authority.execution_scope != flow
            or (flow == 'drive' and command != 'drive')
            or (flow == 'observe' and command != 'observe')
            or (
                flow == 'cancel'
                and command not in {'cancel', 'observe', 'drive'}
            )
        ):
            raise _error('gazebo_command_runner_prepared_invalid')
        if previous is None:
            step_index = 0
            previous_request_id = None
            previous_response_fingerprint = authorization
            previous_chain_digest = authorization
        else:
            step_index = previous.step_index + 1
            if step_index >= self._max_steps:
                raise _error('gazebo_command_runner_step_limit_invalid')
            previous_request_id = previous.request_id
            previous_response_fingerprint = (
                previous.gateway_response_fingerprint
            )
            previous_chain_digest = previous.chain_digest
        request_id = _request_id(
            authorization_digest=authorization,
            operation_id=authority.operation_id,
            run_request_id=run_request_id,
            flow=flow,
            command=command,
            step_index=step_index,
            previous_request_id=previous_request_id,
            previous_response_fingerprint=(
                previous_response_fingerprint
            ),
        )
        remaining = deadline - _monotonic()
        if not math.isfinite(remaining) or remaining <= 0.0:
            raise _error('gazebo_command_runner_deadline_exhausted')
        exchange_timeout = min(
            remaining,
            MAX_GAZEBO_MONITOR_ROOM_GATEWAY_TIMEOUT_SECONDS,
        )
        try:
            SQLiteConversationStore.attest_command_boundary_durability(
                self._store
            )
        except Exception:
            raise _error('gazebo_command_runner_unavailable') from None
        failure = None
        gateway: Any = None
        try:
            gateway = GazeboMonitorRoomGatewayClient.exchange(
                self._client,
                request_id=request_id,
                operation_id=authority.operation_id,
                command=command,
                timeout_seconds=exchange_timeout,
            )
        except GazeboMonitorRoomGatewayClientError:
            failure = _error('gazebo_command_runner_gateway_unavailable')
        except Exception:
            failure = _error('gazebo_command_runner_gateway_unavailable')
        if failure is not None:
            failure.__cause__ = None
            failure.__context__ = None
            failure.__traceback__ = None
            raise failure
        try:
            canonical_gateway = _canonical_gateway_result(gateway)
            gateway_fingerprint = (
                GazeboMonitorRoomGatewayResult.response_fingerprint.fget(
                    canonical_gateway
                )
            )
            invalid_gateway = (
                canonical_gateway.request_id != request_id
                or canonical_gateway.operation_id != authority.operation_id
                or canonical_gateway.command != command
                or canonical_gateway.runtime_mode != 'gazebo'
                or canonical_gateway.simulation is not True
                or canonical_gateway.physical_authorized is not False
                or canonical_gateway.physical_effects is not False
                or canonical_gateway.viewer_live is not False
                or canonical_gateway.camera_coverage_validated is not False
                or canonical_gateway.coverage_achieved is not False
            )
        except (
            AttributeError,
            GazeboMonitorRoomCommandRunnerError,
            GazeboMonitorRoomGatewayClientError,
        ):
            invalid_gateway = True
        if invalid_gateway:
            raise _error('gazebo_command_runner_gateway_invalid')
        return GazeboMonitorRoomCommandStep(
            outbox_id=authority.outbox_id,
            operation_id=authority.operation_id,
            run_request_id=run_request_id,
            authorization_digest=authorization,
            flow=flow,
            request_id=request_id,
            command=command,
            step_index=step_index,
            previous_request_id=previous_request_id,
            previous_response_fingerprint=(
                previous_response_fingerprint
            ),
            state=canonical_gateway.state,
            current_sample_index=canonical_gateway.current_sample_index,
            navigation_samples_total=(
                canonical_gateway.navigation_samples_total
            ),
            navigation_samples_reached=(
                canonical_gateway.navigation_samples_reached
            ),
            terminal=canonical_gateway.terminal,
            robot_blocked=canonical_gateway.robot_blocked,
            terminal_code=canonical_gateway.terminal_code,
            evidence_digest=canonical_gateway.evidence_digest,
            gateway_response_fingerprint=gateway_fingerprint,
            previous_chain_digest=previous_chain_digest,
        )

    def _single(
        self,
        *,
        confirmation_request_id: Any,
        run_request_id: Any,
        flow: str,
        command: Optional[str],
        previous: Any,
        timeout_seconds: Any,
    ) -> GazeboMonitorRoomCommandStep:
        failure = None
        try:
            GazeboMonitorRoomCommandRunner._attest_configuration(self)
            normalized_confirmation = _identifier(
                confirmation_request_id
            )
            normalized_run = _identifier(run_request_id)
            timeout = _timeout(
                timeout_seconds,
                maximum=GAZEBO_COMMAND_RUNNER_MAX_TIMEOUT_SECONDS,
            )
            deadline = _monotonic() + timeout
            with self._command_lock:
                GazeboMonitorRoomCommandRunner._attest_configuration(self)
                authority = GazeboMonitorRoomCommandRunner._resolve_authority(
                    self,
                    normalized_confirmation,
                    flow,
                )
                cursor = GazeboMonitorRoomCommandRunner._canonical_previous(
                    authority,
                    normalized_run,
                    flow,
                    previous,
                )
                selected_command = (
                    command
                    if command is not None
                    else GazeboMonitorRoomCommandRunner._next_cancel_command(
                        cursor
                    )
                )
                return GazeboMonitorRoomCommandRunner._invoke_locked(
                    self,
                    authority=authority,
                    run_request_id=normalized_run,
                    flow=flow,
                    command=selected_command,
                    previous=cursor,
                    deadline=deadline,
                )
        except GazeboMonitorRoomCommandRunnerError as error:
            failure = error
        except Exception:
            failure = _error('gazebo_command_runner_unavailable')
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def drive_once(
        self,
        confirmation_request_id: str,
        run_request_id: str,
        *,
        previous: Optional[GazeboMonitorRoomCommandStep] = None,
        timeout_seconds: float = (
            GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS
        ),
    ) -> GazeboMonitorRoomCommandStep:
        """Advance one deterministic drive step, never a background loop."""
        return GazeboMonitorRoomCommandRunner._single(
            self,
            confirmation_request_id=confirmation_request_id,
            run_request_id=run_request_id,
            flow='drive',
            command='drive',
            previous=previous,
            timeout_seconds=timeout_seconds,
        )

    def observe_once(
        self,
        confirmation_request_id: str,
        run_request_id: str,
        *,
        previous: Optional[GazeboMonitorRoomCommandStep] = None,
        timeout_seconds: float = (
            GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS
        ),
    ) -> GazeboMonitorRoomCommandStep:
        """Read one deterministic coordinate-free gateway observation."""
        return GazeboMonitorRoomCommandRunner._single(
            self,
            confirmation_request_id=confirmation_request_id,
            run_request_id=run_request_id,
            flow='observe',
            command='observe',
            previous=previous,
            timeout_seconds=timeout_seconds,
        )

    def cancel_once(
        self,
        confirmation_request_id: str,
        run_request_id: str,
        *,
        previous: Optional[GazeboMonitorRoomCommandStep] = None,
        timeout_seconds: float = (
            GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS
        ),
    ) -> GazeboMonitorRoomCommandStep:
        """Advance one cancel/reconcile step with a stable request chain."""
        return GazeboMonitorRoomCommandRunner._single(
            self,
            confirmation_request_id=confirmation_request_id,
            run_request_id=run_request_id,
            flow='cancel',
            command=None,
            previous=previous,
            timeout_seconds=timeout_seconds,
        )

    def _run(
        self,
        *,
        confirmation_request_id: Any,
        run_request_id: Any,
        flow: str,
        max_steps: Any,
        timeout_seconds: Any,
        resume_from: Any,
    ) -> GazeboMonitorRoomCommandRun:
        failure = None
        try:
            GazeboMonitorRoomCommandRunner._attest_configuration(self)
            normalized_confirmation = _identifier(
                confirmation_request_id
            )
            normalized_run = _identifier(run_request_id)
            limit = _step_limit(max_steps, maximum=self._max_steps)
            timeout = _timeout(
                timeout_seconds,
                maximum=GAZEBO_COMMAND_RUNNER_MAX_TIMEOUT_SECONDS,
            )
            deadline = _monotonic() + timeout
            made = 0
            stop_reason = 'step_limit'
            with self._command_lock:
                GazeboMonitorRoomCommandRunner._attest_configuration(self)
                authority = GazeboMonitorRoomCommandRunner._resolve_authority(
                    self,
                    normalized_confirmation,
                    flow,
                )
                cursor = GazeboMonitorRoomCommandRunner._canonical_previous(
                    authority,
                    normalized_run,
                    flow,
                    resume_from,
                )
                while made < limit:
                    remaining = deadline - _monotonic()
                    if not math.isfinite(remaining) or remaining <= 0.0:
                        stop_reason = 'deadline'
                        break
                    command = (
                        'drive'
                        if flow == 'drive'
                        else (
                            GazeboMonitorRoomCommandRunner
                            ._next_cancel_command(cursor)
                        )
                    )
                    cursor = GazeboMonitorRoomCommandRunner._invoke_locked(
                        self,
                        authority=authority,
                        run_request_id=normalized_run,
                        flow=flow,
                        command=command,
                        previous=cursor,
                        deadline=deadline,
                    )
                    made += 1
                    if cursor.terminal:
                        stop_reason = 'terminal'
                        break
                else:
                    stop_reason = 'step_limit'
            return GazeboMonitorRoomCommandRun(
                flow=flow,
                stop_reason=stop_reason,
                requests_made=made,
                request_limit=limit,
                last_step=cursor,
            )
        except GazeboMonitorRoomCommandRunnerError as error:
            failure = error
        except Exception:
            failure = _error('gazebo_command_runner_unavailable')
        assert failure is not None
        failure.__cause__ = None
        failure.__context__ = None
        failure.__traceback__ = None
        raise failure

    def drive_until_terminal(
        self,
        confirmation_request_id: str,
        run_request_id: str,
        *,
        max_steps: int = GAZEBO_COMMAND_RUNNER_DEFAULT_MAX_STEPS,
        timeout_seconds: float = (
            GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS
        ),
        resume_from: Optional[GazeboMonitorRoomCommandStep] = None,
    ) -> GazeboMonitorRoomCommandRun:
        """Run a bounded foreground drive loop and stop on any terminal."""
        return GazeboMonitorRoomCommandRunner._run(
            self,
            confirmation_request_id=confirmation_request_id,
            run_request_id=run_request_id,
            flow='drive',
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
            resume_from=resume_from,
        )

    def cancel_until_terminal(
        self,
        confirmation_request_id: str,
        run_request_id: str,
        *,
        max_steps: int = GAZEBO_COMMAND_RUNNER_DEFAULT_MAX_STEPS,
        timeout_seconds: float = (
            GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS
        ),
        resume_from: Optional[GazeboMonitorRoomCommandStep] = None,
    ) -> GazeboMonitorRoomCommandRun:
        """Run a bounded cancel/observe/drive reconciliation sequence."""
        return GazeboMonitorRoomCommandRunner._run(
            self,
            confirmation_request_id=confirmation_request_id,
            run_request_id=run_request_id,
            flow='cancel',
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
            resume_from=resume_from,
        )


__all__ = [
    'GAZEBO_COMMAND_RUNNER_DEFAULT_MAX_STEPS',
    'GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS',
    'GAZEBO_COMMAND_RUNNER_MAX_STEPS',
    'GAZEBO_COMMAND_RUNNER_MAX_TIMEOUT_SECONDS',
    'GAZEBO_COMMAND_RUNNER_SCHEMA_VERSION',
    'GazeboMonitorRoomCommandRun',
    'GazeboMonitorRoomCommandRunner',
    'GazeboMonitorRoomCommandRunnerError',
    'GazeboMonitorRoomCommandStep',
]
