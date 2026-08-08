"""Server-owned capability registry and non-actuating Tool gateway."""

import copy
import hashlib
import json
import math
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

from malbut_agent_server.schemas import ValidationError, validate_user_id
from malbut_agent_server.tools import (
    TOOL_SPECS,
    ToolSpec,
    select_tool_specs,
    validate_tool_arguments,
)


PROPOSAL_ONLY = 'proposal_only'
READ_ONLY = 'read_only'
SIMULATION_ONLY = 'simulation_only'
CAPABILITY_MODES = frozenset(
    {PROPOSAL_ONLY, READ_ONLY, SIMULATION_ONLY}
)
PRODUCTION = 'production'
SIMULATION = 'simulation'
RUNTIME_MODES = frozenset({PRODUCTION, SIMULATION})

TOOL_RISK_LEVELS = {
    'get_robot_status': 'L0',
    'detect_pet': 'L1',
    'capture_photo': 'L2',
    'send_notification': 'L2',
    'navigate': 'L3',
}

TOOL_TIMEOUT_SECONDS = {
    'get_robot_status': 1.0,
    'navigate': 2.0,
    'detect_pet': 3.0,
    'capture_photo': 5.0,
    'send_notification': 5.0,
}

READ_ONLY_ELIGIBLE = frozenset(
    {'get_robot_status', 'detect_pet'}
)


class ToolAdapter(Protocol):
    """Narrow boundary implemented by trusted ROS or simulation code."""

    def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Return one bounded JSON object without model involvement."""


class ReadOnlyToolAdapter:
    """Explicit marker required for trusted non-mutating adapters."""

    def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Return one bounded read result."""
        raise NotImplementedError


class SimulationToolAdapter:
    """Explicit marker required for side-effect-free simulation adapters."""

    def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Return one bounded result identified as simulated."""
        raise NotImplementedError


class GatewayConflictError(ValidationError):
    """Raised when one request ID is reused with different input."""


@dataclass(frozen=True)
class ToolCapability:
    """One server-owned Tool registration and adapter binding."""

    name: str
    mode: str = PROPOSAL_ONLY
    available: bool = True
    adapter: Optional[ToolAdapter] = None
    timeout_seconds: float = 1.0
    max_result_bytes: int = 16384
    max_state_age_seconds: float = 2.0

    def __post_init__(self) -> None:
        """Reject unsafe or ambiguous registrations at startup."""
        if self.name not in TOOL_SPECS:
            raise ValueError(f'unknown Tool capability: {self.name}')
        if self.mode not in CAPABILITY_MODES:
            raise ValueError(f'unsupported Tool mode: {self.mode}')
        if not isinstance(self.available, bool):
            raise ValueError('Tool availability must be a boolean')
        if self.mode == READ_ONLY and self.name not in READ_ONLY_ELIGIBLE:
            raise ValueError(
                f'{self.name} cannot be registered as read-only'
            )
        if self.mode == PROPOSAL_ONLY and self.adapter is not None:
            raise ValueError(
                'proposal-only capabilities cannot bind an adapter'
            )
        if (
            self.mode == READ_ONLY
            and self.adapter is not None
            and not isinstance(self.adapter, ReadOnlyToolAdapter)
        ):
            raise ValueError(
                'read-only adapters must declare ReadOnlyToolAdapter'
            )
        if (
            self.mode == SIMULATION_ONLY
            and self.adapter is not None
            and not isinstance(self.adapter, SimulationToolAdapter)
        ):
            raise ValueError(
                'simulation adapters must declare SimulationToolAdapter'
            )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 10
        ):
            raise ValueError(
                'Tool timeout_seconds must be between 0 and 10'
            )
        if (
            isinstance(self.max_result_bytes, bool)
            or not isinstance(self.max_result_bytes, int)
            or self.max_result_bytes < 128
            or self.max_result_bytes > 65536
        ):
            raise ValueError(
                'Tool max_result_bytes must be between 128 and 65536'
            )
        if (
            isinstance(self.max_state_age_seconds, bool)
            or not isinstance(
                self.max_state_age_seconds, (int, float)
            )
            or not math.isfinite(float(self.max_state_age_seconds))
            or self.max_state_age_seconds <= 0
            or self.max_state_age_seconds > 60
        ):
            raise ValueError(
                'max_state_age_seconds must be between 0 and 60'
            )

    def executable(self, runtime_mode: str) -> bool:
        """Return whether this exact binding may run in this process."""
        if not self.available or self.adapter is None:
            return False
        if self.mode == READ_ONLY:
            return True
        return (
            self.mode == SIMULATION_ONLY
            and runtime_mode == SIMULATION
        )


class CapabilityRegistry:
    """Immutable authoritative upper bound for model-visible Tools."""

    def __init__(
        self,
        capabilities: Iterable[ToolCapability],
        *,
        runtime_mode: str = PRODUCTION,
        revision: str = 'swm25-73-v1',
    ) -> None:
        """Validate one deterministic capability snapshot."""
        if runtime_mode not in RUNTIME_MODES:
            raise ValueError('unsupported Tool runtime mode')
        values: Dict[str, ToolCapability] = {}
        for capability in capabilities:
            if capability.name in values:
                raise ValueError(
                    f'duplicate Tool capability: {capability.name}'
                )
            values[capability.name] = capability
        if (
            not isinstance(revision, str)
            or not revision
            or len(revision) > 128
        ):
            raise ValueError('capability revision is invalid')
        self._capabilities = MappingProxyType(values)
        self.runtime_mode = runtime_mode
        self.revision = revision

    def get(self, name: str) -> Optional[ToolCapability]:
        """Return one immutable registration snapshot."""
        return self._capabilities.get(name)

    def effective_names(self, requested: Iterable[str]) -> List[str]:
        """Intersect an untrusted optional subset with server policy."""
        result = []
        for name in requested:
            if (
                name in self._capabilities
                and self._capabilities[name].available
                and name not in result
            ):
                result.append(name)
        return result

    def select_specs(self, requested: Iterable[str]) -> List[ToolSpec]:
        """Return provider schemas after the authoritative intersection."""
        return copy.deepcopy(
            select_tool_specs(self.effective_names(requested))
        )

    def to_dict(self) -> Dict[str, Any]:
        """Expose policy metadata without adapters or internal objects."""
        capabilities = []
        for name, capability in self._capabilities.items():
            executable = capability.executable(self.runtime_mode)
            blocked_by = None
            if not capability.available:
                blocked_by = 'tool_unavailable'
            elif capability.mode == PROPOSAL_ONLY:
                blocked_by = 'confirmation_required'
            elif (
                capability.mode == SIMULATION_ONLY
                and self.runtime_mode != SIMULATION
            ):
                blocked_by = 'confirmation_required'
            elif capability.adapter is None:
                blocked_by = 'executor_unavailable'
            capabilities.append(
                {
                    'name': name,
                    'risk_level': TOOL_RISK_LEVELS[name],
                    'mode': capability.mode,
                    'available_for_proposal': capability.available,
                    'executable': executable,
                    'blocked_by': blocked_by,
                    'timeout_ms': int(
                        capability.timeout_seconds * 1000
                    ),
                }
            )
        return {
            'source': 'server_owned_registry',
            'revision': self.revision,
            'runtime_mode': self.runtime_mode,
            'capabilities': capabilities,
        }


@dataclass(frozen=True)
class ToolQuery:
    """Validated read-only or simulation Gateway request."""

    request_id: str
    user_id: str
    tool_name: str
    arguments: Dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> 'ToolQuery':
        """Reject confirmation or execution fields owned by SWM25-74."""
        if not isinstance(value, dict):
            raise ValidationError('Tool query body must be an object')
        allowed = {'request_id', 'user_id', 'tool_name', 'arguments'}
        unknown = set(value) - allowed
        if unknown:
            names = ', '.join(sorted(unknown))
            raise ValidationError(
                f'unknown Tool query fields: {names}'
            )
        request_id = _identifier(value.get('request_id'), 'request_id')
        user_id = validate_user_id(value.get('user_id'))
        tool_name = _identifier(value.get('tool_name'), 'tool_name', 64)
        arguments = value.get('arguments')
        if not isinstance(arguments, dict):
            raise ValidationError('arguments must be an object')
        try:
            encoded = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            ).encode('utf-8')
        except (TypeError, ValueError) as error:
            raise ValidationError(
                'arguments must contain valid JSON values'
            ) from error
        if len(encoded) > 16384:
            raise ValidationError('arguments are too large')
        return cls(
            request_id=request_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=json.loads(encoded.decode('utf-8')),
        )


@dataclass(frozen=True)
class GatewayResult:
    """Terminal result for a bounded query or simulation call."""

    result_id: str
    request_id: str
    tool_name: str
    mode: str
    status: str
    started_at: str
    completed_at: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, str]] = None

    def to_dict(self, *, cached: bool = False) -> Dict[str, Any]:
        """Return the public contract without a SWM25-74 tool_call_id."""
        return {
            'result_id': self.result_id,
            'request_id': self.request_id,
            'tool_name': self.tool_name,
            'mode': self.mode,
            'status': self.status,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'result': self.result,
            'error': self.error,
            'cached': cached,
        }


@dataclass
class _InFlightQuery:
    """Coordinate duplicate request IDs without serializing all Tools."""

    fingerprint: str
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[GatewayResult] = None
    error: Optional[BaseException] = None


@dataclass
class MockToolAdapter(ReadOnlyToolAdapter, SimulationToolAdapter):
    """Side-effect-free deterministic adapter for explicit simulation."""

    tool_name: str
    calls: int = 0

    def invoke(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Return a simulation result without ROS, files, or network I/O."""
        self.calls += 1
        common: Dict[str, Any] = {
            'simulated': True,
            'source': 'malbut_mock_adapter',
        }
        if self.tool_name == 'get_robot_status':
            return {
                **common,
                'observed_at': _utc_now(),
                'battery_percent': 80.0,
                'emergency_stop': False,
                'subsystems_ok': True,
            }
        if self.tool_name == 'detect_pet':
            return {
                **common,
                'observed_at': _utc_now(),
                'detected': False,
                'privacy_checked': True,
            }
        if self.tool_name == 'navigate':
            return {
                **common,
                'accepted': False,
                'destination': arguments['location'],
                'nav2_goal_published': False,
            }
        if self.tool_name == 'capture_photo':
            return {
                **common,
                'image_created': False,
            }
        if self.tool_name == 'send_notification':
            return {
                **common,
                'delivered': False,
            }
        raise RuntimeError('unsupported simulation adapter')


class ToolGateway:
    """Fail-closed dispatcher for read-only and explicit simulation Tools."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        cache_size: int = 256,
        max_workers: int = 4,
    ) -> None:
        """Create bounded in-process idempotency and adapter workers."""
        if cache_size < 1 or cache_size > 10000:
            raise ValueError('Tool query cache_size is invalid')
        if max_workers < 1 or max_workers > 16:
            raise ValueError('Tool max_workers is invalid')
        self.registry = registry
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[str, GatewayResult]] = (
            OrderedDict()
        )
        self._inflight: Dict[str, _InFlightQuery] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix='malbut-tool-query',
        )
        self._closed = False

    def query(self, query: ToolQuery) -> GatewayResult:
        """Run at most once per process after every local policy check."""
        result, _cached = self.query_with_cache_state(query)
        return result

    def query_with_cache_state(
        self,
        query: ToolQuery,
    ) -> Tuple[GatewayResult, bool]:
        """Return one result plus whether it came from retry cache."""
        fingerprint = _query_fingerprint(query)
        with self._lock:
            cached = self._cache.get(query.request_id)
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != fingerprint:
                    raise GatewayConflictError(
                        'request_id was already used with different input'
                    )
                self._cache.move_to_end(query.request_id)
                return cached_result, True
            inflight = self._inflight.get(query.request_id)
            if inflight is not None:
                if inflight.fingerprint != fingerprint:
                    raise GatewayConflictError(
                        'request_id is in flight with different input'
                    )
                owns_query = False
            else:
                if self._closed:
                    raise RuntimeError('Tool Gateway is closed')
                inflight = _InFlightQuery(fingerprint=fingerprint)
                self._inflight[query.request_id] = inflight
                owns_query = True
        if not owns_query:
            inflight.event.wait()
            if inflight.error is not None:
                raise inflight.error
            if inflight.result is None:
                raise RuntimeError('Tool query ended without a result')
            return inflight.result, True
        try:
            result = self._query_uncached(query)
        except BaseException as error:
            with self._lock:
                inflight.error = error
                self._inflight.pop(query.request_id, None)
                inflight.event.set()
            raise
        with self._lock:
            self._cache[query.request_id] = (fingerprint, result)
            self._cache.move_to_end(query.request_id)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            inflight.result = result
            self._inflight.pop(query.request_id, None)
            inflight.event.set()
        return result, False

    def _query_uncached(self, query: ToolQuery) -> GatewayResult:
        started_at = _utc_now()
        capability = self.registry.get(query.tool_name)
        if query.tool_name not in TOOL_SPECS:
            return self._rejected(
                query,
                PROPOSAL_ONLY,
                started_at,
                'unknown_tool',
                'The Tool is not registered.',
            )
        if capability is None or not capability.available:
            return self._rejected(
                query,
                PROPOSAL_ONLY,
                started_at,
                'tool_unavailable',
                'The Tool is not available in this runtime.',
            )
        try:
            arguments = validate_tool_arguments(
                query.tool_name,
                query.arguments,
            )
        except ValidationError:
            return self._rejected(
                query,
                capability.mode,
                started_at,
                'invalid_arguments',
                'The Tool arguments are invalid.',
            )
        if capability.mode == PROPOSAL_ONLY or (
            capability.mode == SIMULATION_ONLY
            and self.registry.runtime_mode != SIMULATION
        ):
            return self._rejected(
                query,
                capability.mode,
                started_at,
                'confirmation_required',
                'SWM25-74 confirmation is required for this Tool.',
            )
        if capability.adapter is None:
            return self._rejected(
                query,
                capability.mode,
                started_at,
                'executor_unavailable',
                'A trusted Tool adapter is not available.',
            )
        future = self._executor.submit(
            capability.adapter.invoke,
            arguments,
        )
        try:
            adapter_result = future.result(
                timeout=capability.timeout_seconds
            )
            result = _validated_adapter_result(
                query.tool_name,
                adapter_result,
                capability,
            )
        except TimeoutError:
            future.cancel()
            return self._failure(
                query,
                capability.mode,
                started_at,
                'timed_out',
                'timed_out',
                'The Tool adapter exceeded its deadline.',
            )
        except StaleStateError:
            return self._failure(
                query,
                capability.mode,
                started_at,
                'failed',
                'stale_state',
                'The trusted robot state is stale.',
            )
        except Exception:
            return self._failure(
                query,
                capability.mode,
                started_at,
                'failed',
                'adapter_failed',
                'The Tool adapter returned an invalid result.',
            )
        return GatewayResult(
            result_id=str(uuid.uuid4()),
            request_id=query.request_id,
            tool_name=query.tool_name,
            mode=capability.mode,
            status='succeeded',
            started_at=started_at,
            completed_at=_utc_now(),
            result=result,
        )

    @staticmethod
    def _rejected(
        query: ToolQuery,
        mode: str,
        started_at: str,
        code: str,
        message: str,
    ) -> GatewayResult:
        return ToolGateway._failure(
            query,
            mode,
            started_at,
            'rejected',
            code,
            message,
        )

    @staticmethod
    def _failure(
        query: ToolQuery,
        mode: str,
        started_at: str,
        status: str,
        code: str,
        message: str,
    ) -> GatewayResult:
        return GatewayResult(
            result_id=str(uuid.uuid4()),
            request_id=query.request_id,
            tool_name=query.tool_name,
            mode=mode,
            status=status,
            started_at=started_at,
            completed_at=_utc_now(),
            error={'code': code, 'message': message},
        )

    def close(self) -> None:
        """Reject new calls without waiting on a stuck trusted adapter."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


class StaleStateError(RuntimeError):
    """Raised when a status adapter returns an old observation."""


def production_registry() -> CapabilityRegistry:
    """Build the safe production default with no executable adapters."""
    return CapabilityRegistry(
        [
            ToolCapability(
                name=name,
                mode=(
                    READ_ONLY
                    if name in READ_ONLY_ELIGIBLE
                    else PROPOSAL_ONLY
                ),
                timeout_seconds=TOOL_TIMEOUT_SECONDS[name],
            )
            for name in TOOL_SPECS
        ],
        runtime_mode=PRODUCTION,
    )


def simulation_registry() -> CapabilityRegistry:
    """Build explicit side-effect-free adapters for local demonstrations."""
    capabilities = []
    for name in TOOL_SPECS:
        capabilities.append(
            ToolCapability(
                name=name,
                mode=SIMULATION_ONLY,
                adapter=MockToolAdapter(name),
                timeout_seconds=TOOL_TIMEOUT_SECONDS[name],
            )
        )
    return CapabilityRegistry(
        capabilities,
        runtime_mode=SIMULATION,
        revision='swm25-73-simulation-v1',
    )


def _identifier(
    value: Any,
    field_name: str,
    max_length: int = 128,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f'{field_name} must be a string')
    result = value.strip()
    if not result or len(result) > max_length:
        raise ValidationError(f'{field_name} is invalid')
    if any(ord(item) < 32 or ord(item) == 127 for item in result):
        raise ValidationError(
            f'{field_name} must not contain control characters'
        )
    return result


def _query_fingerprint(query: ToolQuery) -> str:
    encoded = json.dumps(
        {
            'user_id': query.user_id,
            'tool_name': query.tool_name,
            'arguments': query.arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _validated_adapter_result(
    tool_name: str,
    value: Any,
    capability: ToolCapability,
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError('adapter result must be an object')
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    if len(encoded) > capability.max_result_bytes:
        raise ValueError('adapter result is too large')
    result = json.loads(encoded.decode('utf-8'))
    if (
        capability.mode == SIMULATION_ONLY
        and result.get('simulated') is not True
    ):
        raise ValueError('simulation result is not identified')
    _validate_result_fields(tool_name, result)
    if tool_name in {'get_robot_status', 'detect_pet'}:
        _validate_fresh_observation(
            result['observed_at'],
            capability.max_state_age_seconds,
        )
    return result


def _validate_result_fields(
    tool_name: str,
    result: Dict[str, Any],
) -> None:
    """Validate strict per-Tool output fields before public serialization."""
    allowed = {
        'get_robot_status': {
            'simulated',
            'source',
            'observed_at',
            'battery_percent',
            'emergency_stop',
            'navigation_available',
            'localization_ok',
            'camera_available',
            'privacy_mode',
            'docked',
            'subsystems_ok',
        },
        'detect_pet': {
            'simulated',
            'source',
            'observed_at',
            'privacy_checked',
            'detected',
            'confidence',
        },
        'navigate': {
            'simulated',
            'source',
            'accepted',
            'destination',
            'nav2_goal_published',
        },
        'capture_photo': {
            'simulated',
            'source',
            'image_created',
        },
        'send_notification': {
            'simulated',
            'source',
            'delivered',
        },
    }[tool_name]
    unknown = set(result) - allowed
    if unknown:
        raise ValueError('adapter result contains unknown fields')
    _require_result_string(result, 'source')
    if 'simulated' in result:
        _require_result_bool(result, 'simulated')

    if tool_name == 'get_robot_status':
        _require_result_string(result, 'observed_at')
        _require_result_number(
            result,
            'battery_percent',
            minimum=0,
            maximum=100,
        )
        _require_result_bool(result, 'emergency_stop')
        for field_name in (
            'navigation_available',
            'localization_ok',
            'camera_available',
            'privacy_mode',
            'docked',
            'subsystems_ok',
        ):
            if field_name in result:
                _require_result_bool(result, field_name)
        return
    if tool_name == 'detect_pet':
        _require_result_string(result, 'observed_at')
        _require_result_bool(result, 'privacy_checked', expected=True)
        _require_result_bool(result, 'detected')
        if 'confidence' in result:
            _require_result_number(
                result,
                'confidence',
                minimum=0,
                maximum=1,
            )
        return
    if tool_name == 'navigate':
        _require_result_bool(result, 'accepted')
        _require_result_string(result, 'destination')
        _require_result_bool(
            result,
            'nav2_goal_published',
            expected=False,
        )
        return
    if tool_name == 'capture_photo':
        _require_result_bool(
            result,
            'image_created',
            expected=False,
        )
        return
    _require_result_bool(result, 'delivered', expected=False)


def _require_result_string(
    result: Dict[str, Any],
    field_name: str,
) -> None:
    value = result.get(field_name)
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2000
    ):
        raise ValueError('adapter result string is invalid')


def _require_result_bool(
    result: Dict[str, Any],
    field_name: str,
    *,
    expected: Optional[bool] = None,
) -> None:
    value = result.get(field_name)
    if not isinstance(value, bool):
        raise ValueError('adapter result boolean is invalid')
    if expected is not None and value is not expected:
        raise ValueError('adapter result safety evidence is invalid')


def _require_result_number(
    result: Dict[str, Any],
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> None:
    value = result.get(field_name)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < minimum
        or value > maximum
    ):
        raise ValueError('adapter result number is invalid')


def _validate_fresh_observation(
    observed_at: str,
    max_age_seconds: float,
) -> None:
    observed = datetime.fromisoformat(
        observed_at.replace('Z', '+00:00')
    )
    if observed.tzinfo is None:
        raise ValueError('adapter timestamp has no timezone')
    age = time.time() - observed.timestamp()
    if age < -1 or age > max_age_seconds:
        raise StaleStateError('adapter observation is stale')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
