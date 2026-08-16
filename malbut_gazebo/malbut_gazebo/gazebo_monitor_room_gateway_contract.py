"""
Narrow, coordinate-free wire contract for the Gazebo gateway.

The future Unix-domain gateway accepts only an opaque operation identity and
one of three commands.  Map identifiers, coordinates, goals, lease values,
fences, and safety evidence are deliberately absent: the trusted gateway must
load those values from its own durable stores.

This module performs no I/O and grants no navigation authority.
"""

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Dict, Tuple

from malbut_gazebo.gazebo_monitor_room_store import OperationObservation


GATEWAY_CONTRACT_SCHEMA_VERSION = 1
GATEWAY_MAX_REQUEST_BYTES = 2048
GATEWAY_MAX_RESPONSE_BYTES = 4096

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


class GazeboMonitorRoomGatewayContractError(ValueError):
    """Content-free rejection from the gateway wire boundary."""

    def __init__(self, code: str) -> None:
        """Expose only one fixed contract error code."""
        allowed = {
            'gateway_request_invalid',
            'gateway_response_invalid',
        }
        normalized = code if code in allowed else 'gateway_request_invalid'
        super().__init__(normalized)
        self.code = normalized


class _InvalidRequestJSON(ValueError):
    """Internal control flow for duplicate or non-finite JSON values."""


def _fail_request() -> None:
    raise GazeboMonitorRoomGatewayContractError(
        'gateway_request_invalid'
    )


def _fail_response() -> None:
    raise GazeboMonitorRoomGatewayContractError(
        'gateway_response_invalid'
    )


def _identifier(value: Any, *, response: bool = False) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        if response:
            _fail_response()
        _fail_request()
    return value


def _digest_json(value: Any, *, response: bool = False) -> str:
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
        if response:
            _fail_response()
        _fail_request()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_bytes(value: Any, *, response: bool = False) -> bytes:
    invalid = False
    result = b''
    try:
        result = json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        ).encode('ascii')
    except (OverflowError, TypeError, UnicodeEncodeError, ValueError):
        invalid = True
    if invalid:
        if response:
            _fail_response()
        _fail_request()
    limit = (
        GATEWAY_MAX_RESPONSE_BYTES
        if response
        else GATEWAY_MAX_REQUEST_BYTES
    )
    if not result or len(result) > limit:
        if response:
            _fail_response()
        _fail_request()
    return result


def _unique_object(pairs: list[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _InvalidRequestJSON()
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    """Reject NaN and infinities without constructing a public error."""
    raise _InvalidRequestJSON()


def _request_mapping_from_bytes(payload: Any) -> Dict[str, Any]:
    if type(payload) is not bytes or not payload or (
        len(payload) > GATEWAY_MAX_REQUEST_BYTES
    ):
        _fail_request()
    invalid = False
    value: Any = None
    try:
        value = json.loads(
            payload.decode('utf-8'),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (
        _InvalidRequestJSON,
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        invalid = True
    if invalid:
        _fail_request()
    if type(value) is not dict or set(value) != _REQUEST_FIELDS:
        _fail_request()
    return value


def _response_mapping_from_bytes(payload: Any) -> Dict[str, Any]:
    if type(payload) is not bytes or not payload or (
        len(payload) > GATEWAY_MAX_RESPONSE_BYTES
    ):
        _fail_response()
    invalid = False
    value: Any = None
    try:
        value = json.loads(
            payload.decode('utf-8'),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (
        _InvalidRequestJSON,
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        invalid = True
    if invalid or type(value) is not dict or set(value) != _RESPONSE_FIELDS:
        _fail_response()
    return value


def _canonical_observation(value: Any) -> OperationObservation:
    """Rebuild a store observation before projecting it to the wire."""
    invalid = False
    canonical: Any = None
    try:
        if type(value) is not OperationObservation:
            invalid = True
        else:
            canonical = OperationObservation(
                operation_id=value.operation_id,
                robot_id=value.robot_id,
                state=value.state,
                current_sample_index=value.current_sample_index,
                current_sample_state=value.current_sample_state,
                current_goal_uuid=value.current_goal_uuid,
                navigation_samples_total=(
                    value.navigation_samples_total
                ),
                navigation_samples_reached=(
                    value.navigation_samples_reached
                ),
                fence_epoch=value.fence_epoch,
                lease_owner=value.lease_owner,
                lease_expires_at=value.lease_expires_at,
                deadline=value.deadline,
                terminal_code=value.terminal_code,
                cancel_request_id=value.cancel_request_id,
                created_at=value.created_at,
                updated_at=value.updated_at,
                replayed=value.replayed,
            )
            invalid = (
                canonical.__dict__.keys() != value.__dict__.keys()
                or any(
                    type(value.__dict__[name])
                    is not type(canonical_value)
                    or value.__dict__[name] != canonical_value
                    for name, canonical_value
                    in canonical.__dict__.items()
                )
            )
    except (AttributeError, OverflowError, TypeError, ValueError):
        invalid = True
    if invalid:
        _fail_response()
    return canonical


def _observation_evidence_digest(
    request_fingerprint: str,
    observation: OperationObservation,
) -> str:
    """Bind private state needed for exact response correlation."""
    return _digest_json(
        {
            'contract': 'gazebo-monitor-room-gateway-observation-v1',
            'request_fingerprint': request_fingerprint,
            'operation_id': observation.operation_id,
            'robot_id': observation.robot_id,
            'state': observation.state,
            'current_sample_index': observation.current_sample_index,
            'current_sample_state': observation.current_sample_state,
            'current_goal_uuid': observation.current_goal_uuid,
            'navigation_samples_total': (
                observation.navigation_samples_total
            ),
            'navigation_samples_reached': (
                observation.navigation_samples_reached
            ),
            'fence_epoch': observation.fence_epoch,
            'lease_owner': observation.lease_owner,
            'lease_expires_at': observation.lease_expires_at,
            'deadline': observation.deadline,
            'terminal_code': observation.terminal_code,
            'cancel_request_id': observation.cancel_request_id,
            'created_at': observation.created_at,
            'updated_at': observation.updated_at,
            'replayed': observation.replayed,
        },
        response=True,
    )


@dataclass(frozen=True)
class GazeboMonitorRoomGatewayRequest:
    """One idempotent command over a server-owned operation."""

    request_id: str = field(repr=False)
    operation_id: str = field(repr=False)
    command: str
    schema_version: int = GATEWAY_CONTRACT_SCHEMA_VERSION
    _request_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Require the exact minimal command shape."""
        if (
            type(self.schema_version) is not int
            or self.schema_version != GATEWAY_CONTRACT_SCHEMA_VERSION
        ):
            _fail_request()
        _identifier(self.request_id)
        _identifier(self.operation_id)
        if type(self.command) is not str or self.command not in _COMMANDS:
            _fail_request()
        object.__setattr__(
            self,
            '_request_fingerprint',
            _digest_json(self.to_dict()),
        )

    @classmethod
    def from_wire_bytes(
        cls, payload: Any
    ) -> 'GazeboMonitorRoomGatewayRequest':
        """Parse a bounded JSON request with duplicate-key rejection."""
        value = _request_mapping_from_bytes(payload)
        return cls(
            schema_version=value['schema_version'],
            request_id=value['request_id'],
            operation_id=value['operation_id'],
            command=value['command'],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the complete coordinate-free wire value."""
        return {
            'schema_version': self.schema_version,
            'request_id': self.request_id,
            'operation_id': self.operation_id,
            'command': self.command,
        }

    def to_wire_bytes(self) -> bytes:
        """Return deterministic bounded JSON bytes."""
        self.request_fingerprint
        return _canonical_json_bytes(self.to_dict())

    @property
    def request_fingerprint(self) -> str:
        """Return the canonical request digest and detect mutation."""
        current = _digest_json(self.to_dict())
        if current != self._request_fingerprint:
            _fail_request()
        return current

    @property
    def cancel_request_id(self) -> str:
        """Derive a stable server-side cancellation identity."""
        if self.command != 'cancel':
            _fail_request()
        digest = _digest_json(
            {
                'contract': 'gazebo-monitor-room-gateway-cancel-v1',
                'request_fingerprint': self.request_fingerprint,
            }
        )
        return f'gateway-cancel-{digest}'


@dataclass(frozen=True)
class GazeboMonitorRoomGatewayResponse:
    """Coordinate-free observation returned by the future gateway."""

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
    schema_version: int = GATEWAY_CONTRACT_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _response_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate a bounded observation without execution claims."""
        if (
            type(self.schema_version) is not int
            or self.schema_version != GATEWAY_CONTRACT_SCHEMA_VERSION
        ):
            _fail_response()
        _identifier(self.request_id, response=True)
        _identifier(self.operation_id, response=True)
        if type(self.command) is not str or self.command not in _COMMANDS:
            _fail_response()
        if (
            type(self.state) is not str
            or self.state not in _OPERATION_STATES
        ):
            _fail_response()
        for name in (
            'current_sample_index',
            'navigation_samples_total',
            'navigation_samples_reached',
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 4096:
                _fail_response()
        if (
            self.navigation_samples_total < 1
            or self.current_sample_index >= self.navigation_samples_total
            or self.navigation_samples_reached
            > self.navigation_samples_total
            or type(self.terminal) is not bool
            or type(self.robot_blocked) is not bool
        ):
            _fail_response()
        expected_terminal = self.state in _TERMINAL_STATES
        expected_blocked = self.state in (
            _NONTERMINAL_STATES | _UNKNOWN_STATES
        )
        if (
            self.terminal is not expected_terminal
            or self.robot_blocked is not expected_blocked
            or (self.terminal_code is not None) is not expected_terminal
        ):
            _fail_response()
        if self.terminal_code is not None and (
            type(self.terminal_code) is not str
            or _STATE.fullmatch(self.terminal_code) is None
        ):
            _fail_response()
        if self.state == 'succeeded':
            if (
                self.navigation_samples_reached
                != self.navigation_samples_total
            ):
                _fail_response()
        elif self.navigation_samples_reached != self.current_sample_index:
            _fail_response()
        if (
            type(self.evidence_digest) is not str
            or _DIGEST.fullmatch(self.evidence_digest) is None
        ):
            _fail_response()
        if (
            self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            _fail_response()
        object.__setattr__(
            self,
            '_response_fingerprint',
            _digest_json(self.to_dict(), response=True),
        )

    @classmethod
    def from_wire_bytes(
        cls, payload: Any
    ) -> 'GazeboMonitorRoomGatewayResponse':
        """Parse one exact bounded response without trusting JSON aliases."""
        value = _response_mapping_from_bytes(payload)
        if (
            value['runtime_mode'] != 'gazebo'
            or value['simulation'] is not True
            or value['physical_authorized'] is not False
            or value['physical_effects'] is not False
            or value['viewer_live'] is not False
            or value['camera_coverage_validated'] is not False
            or value['coverage_achieved'] is not False
        ):
            _fail_response()
        return cls(
            schema_version=value['schema_version'],
            request_id=value['request_id'],
            operation_id=value['operation_id'],
            command=value['command'],
            state=value['state'],
            current_sample_index=value['current_sample_index'],
            navigation_samples_total=value['navigation_samples_total'],
            navigation_samples_reached=(
                value['navigation_samples_reached']
            ),
            terminal=value['terminal'],
            robot_blocked=value['robot_blocked'],
            terminal_code=value['terminal_code'],
            evidence_digest=value['evidence_digest'],
        )

    @classmethod
    def from_observation(
        cls,
        request: GazeboMonitorRoomGatewayRequest,
        observation: OperationObservation,
    ) -> 'GazeboMonitorRoomGatewayResponse':
        """Project one exact store observation without caller-owned fields."""
        if type(request) is not GazeboMonitorRoomGatewayRequest:
            _fail_response()
        invalid = False
        canonical_request: Any = None
        try:
            canonical_request = GazeboMonitorRoomGatewayRequest(
                schema_version=request.schema_version,
                request_id=request.request_id,
                operation_id=request.operation_id,
                command=request.command,
            )
            invalid = (
                canonical_request != request
                or canonical_request.request_fingerprint
                != request.request_fingerprint
            )
        except (
            AttributeError,
            GazeboMonitorRoomGatewayContractError,
        ):
            invalid = True
        if invalid:
            _fail_response()
        canonical = _canonical_observation(observation)
        if canonical.operation_id != canonical_request.operation_id:
            _fail_response()
        return cls(
            request_id=canonical_request.request_id,
            operation_id=canonical.operation_id,
            command=canonical_request.command,
            state=canonical.state,
            current_sample_index=canonical.current_sample_index,
            navigation_samples_total=(
                canonical.navigation_samples_total
            ),
            navigation_samples_reached=(
                canonical.navigation_samples_reached
            ),
            terminal=canonical.terminal,
            robot_blocked=canonical.robot_blocked,
            terminal_code=canonical.terminal_code,
            evidence_digest=_observation_evidence_digest(
                canonical_request.request_fingerprint,
                canonical,
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a bounded response without private navigation data."""
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
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }

    def to_wire_bytes(self) -> bytes:
        """Return deterministic bounded response JSON bytes."""
        self.response_fingerprint
        return _canonical_json_bytes(self.to_dict(), response=True)

    @property
    def response_fingerprint(self) -> str:
        """Return the canonical response digest and detect mutation."""
        current = _digest_json(self.to_dict(), response=True)
        if current != self._response_fingerprint:
            _fail_response()
        return current
