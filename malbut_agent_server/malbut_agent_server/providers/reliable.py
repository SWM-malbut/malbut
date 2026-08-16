"""Safe retry, circuit breaker, and fallback for model providers."""

import math
import re
import socket
import threading
import time
import urllib.error
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, List, Optional, Sequence, Tuple

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
)
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
)
from malbut_agent_server.tools import ToolSpec
from malbut_agent_server.trusted_results import TrustedToolResult


class ProviderFailureCode(str, Enum):
    """Content-free failure categories shared by provider adapters."""

    TIMEOUT = 'timeout'
    NETWORK = 'network'
    RATE_LIMIT = 'rate_limit'
    UNAVAILABLE = 'unavailable'
    AUTHENTICATION = 'authentication'
    INVALID_REQUEST = 'invalid_request'
    INVALID_RESPONSE = 'invalid_response'
    INTERNAL = 'internal'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class ProviderFailure:
    """One normalized provider failure without raw error content."""

    code: ProviderFailureCode
    transient: bool
    affects_circuit: bool


_FAILURES = {
    ProviderFailureCode.TIMEOUT: ProviderFailure(
        ProviderFailureCode.TIMEOUT,
        transient=True,
        affects_circuit=True,
    ),
    ProviderFailureCode.NETWORK: ProviderFailure(
        ProviderFailureCode.NETWORK,
        transient=True,
        affects_circuit=True,
    ),
    ProviderFailureCode.RATE_LIMIT: ProviderFailure(
        ProviderFailureCode.RATE_LIMIT,
        transient=True,
        affects_circuit=True,
    ),
    ProviderFailureCode.UNAVAILABLE: ProviderFailure(
        ProviderFailureCode.UNAVAILABLE,
        transient=True,
        affects_circuit=True,
    ),
    ProviderFailureCode.AUTHENTICATION: ProviderFailure(
        ProviderFailureCode.AUTHENTICATION,
        transient=False,
        affects_circuit=True,
    ),
    ProviderFailureCode.INVALID_REQUEST: ProviderFailure(
        ProviderFailureCode.INVALID_REQUEST,
        transient=False,
        affects_circuit=False,
    ),
    ProviderFailureCode.INVALID_RESPONSE: ProviderFailure(
        ProviderFailureCode.INVALID_RESPONSE,
        transient=False,
        affects_circuit=True,
    ),
    ProviderFailureCode.INTERNAL: ProviderFailure(
        ProviderFailureCode.INTERNAL,
        transient=True,
        affects_circuit=True,
    ),
    ProviderFailureCode.UNKNOWN: ProviderFailure(
        ProviderFailureCode.UNKNOWN,
        transient=False,
        affects_circuit=False,
    ),
}


class NormalizedProviderError(ProviderError):
    """Provider error carrying only a safe normalized category."""

    def __init__(self, code: ProviderFailureCode) -> None:
        """Create an error without copying provider response content."""
        self.failure = _FAILURES[code]
        super().__init__('provider failure: ' + code.value)


def _exception_chain(error: BaseException) -> Tuple[BaseException, ...]:
    """Return a short, cycle-safe exception cause chain."""
    chain = []
    current: Optional[BaseException] = error
    seen = set()
    while current is not None and len(chain) < 10:
        identity = id(current)
        if identity in seen:
            break
        seen.add(identity)
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _http_failure(status_code: int) -> ProviderFailure:
    """Classify one HTTP status without preserving response content."""
    if status_code in (408, 409, 425):
        return _FAILURES[ProviderFailureCode.TIMEOUT]
    if status_code == 429:
        return _FAILURES[ProviderFailureCode.RATE_LIMIT]
    if status_code in (401, 403):
        return _FAILURES[ProviderFailureCode.AUTHENTICATION]
    if status_code in (400, 404, 405, 413, 415, 422):
        return _FAILURES[ProviderFailureCode.INVALID_REQUEST]
    if status_code >= 500:
        return _FAILURES[ProviderFailureCode.UNAVAILABLE]
    if status_code >= 400:
        return _FAILURES[ProviderFailureCode.INVALID_REQUEST]
    return _FAILURES[ProviderFailureCode.UNKNOWN]


def classify_exception(error: BaseException) -> ProviderFailure:
    """Normalize an exception without copying its message or payload."""
    chain = _exception_chain(error)

    for item in chain:
        if isinstance(item, NormalizedProviderError):
            return item.failure

    for item in chain:
        if isinstance(item, urllib.error.HTTPError):
            return _http_failure(item.code)

    for item in chain:
        if isinstance(item, (TimeoutError, socket.timeout)):
            return _FAILURES[ProviderFailureCode.TIMEOUT]

    for item in chain:
        if isinstance(item, urllib.error.URLError):
            reason = item.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return _FAILURES[ProviderFailureCode.TIMEOUT]
            return _FAILURES[ProviderFailureCode.NETWORK]

    for item in chain:
        if isinstance(item, (ConnectionError, OSError)):
            return _FAILURES[ProviderFailureCode.NETWORK]

    for item in chain:
        if isinstance(item, ProviderError):
            status_match = re.search(
                r'\bHTTP status ([1-5][0-9]{2})\b',
                str(item),
            )
            if status_match is not None:
                return _http_failure(int(status_match.group(1)))
            return _FAILURES[ProviderFailureCode.INVALID_RESPONSE]

    for item in chain:
        if isinstance(item, (TypeError, ValueError)):
            return _FAILURES[ProviderFailureCode.INVALID_RESPONSE]

    return _FAILURES[ProviderFailureCode.UNKNOWN]


class CircuitState(str, Enum):
    """State of one provider circuit."""

    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'


@dataclass
class _Circuit:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: Optional[float] = None


class ReliableProvider(AgentProvider):
    """Route calls across providers with bounded reliability controls."""

    _MAX_RETRIES_LIMIT = 10
    _SAFE_MESSAGE = (
        '현재 응답 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.'
    )

    @staticmethod
    def _trusted_result_snapshot(
        results: Sequence[TrustedToolResult],
    ) -> Tuple[TrustedToolResult, ...]:
        """Validate and detach one immutable trusted-result sequence."""
        if not isinstance(results, (list, tuple)):
            raise TypeError(
                'trusted_server_tool_results must be a list or tuple'
            )
        if any(type(result) is not TrustedToolResult for result in results):
            raise TypeError(
                'trusted_server_tool_results contains an invalid result'
            )
        return tuple(replace(result) for result in results)

    def __init__(
        self,
        providers: Sequence[AgentProvider],
        *,
        max_retries: int = 2,
        base_delay_seconds: float = 0.25,
        max_delay_seconds: float = 2.0,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        attempt_timeout_seconds: float = 30.0,
        total_timeout_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure bounded retries and ordered provider fallback."""
        self._providers = tuple(providers)
        self._validate_configuration(
            max_retries=max_retries,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
            attempt_timeout_seconds=attempt_timeout_seconds,
            total_timeout_seconds=total_timeout_seconds,
        )
        if not self._providers:
            raise ValueError('at least one provider is required')
        if any(
            not callable(getattr(provider, 'complete', None))
            for provider in self._providers
        ):
            raise TypeError('each provider must implement complete')

        self._max_retries = max_retries
        self._base_delay_seconds = float(base_delay_seconds)
        self._max_delay_seconds = float(max_delay_seconds)
        self._failure_threshold = failure_threshold
        self._recovery_timeout_seconds = float(
            recovery_timeout_seconds
        )
        self._attempt_timeout_seconds = float(
            attempt_timeout_seconds
        )
        self._total_timeout_seconds = float(total_timeout_seconds)
        self._clock = clock
        self._sleep = sleep
        self._circuits = [_Circuit() for _ in self._providers]
        self._circuit_lock = threading.Lock()

    @classmethod
    def _validate_configuration(
        cls,
        *,
        max_retries: int,
        base_delay_seconds: float,
        max_delay_seconds: float,
        failure_threshold: int,
        recovery_timeout_seconds: float,
        attempt_timeout_seconds: float,
        total_timeout_seconds: float,
    ) -> None:
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
            or max_retries > cls._MAX_RETRIES_LIMIT
        ):
            raise ValueError('max_retries must be between 0 and 10')
        cls._require_finite_nonnegative(
            base_delay_seconds,
            'base_delay_seconds',
        )
        cls._require_finite_nonnegative(
            max_delay_seconds,
            'max_delay_seconds',
        )
        if max_delay_seconds < base_delay_seconds:
            raise ValueError(
                'max_delay_seconds must be at least base_delay_seconds'
            )
        if (
            isinstance(failure_threshold, bool)
            or not isinstance(failure_threshold, int)
            or failure_threshold < 1
        ):
            raise ValueError('failure_threshold must be a positive integer')
        cls._require_finite_positive(
            recovery_timeout_seconds,
            'recovery_timeout_seconds',
        )
        cls._require_finite_positive(
            attempt_timeout_seconds,
            'attempt_timeout_seconds',
        )
        cls._require_finite_positive(
            total_timeout_seconds,
            'total_timeout_seconds',
        )
        if total_timeout_seconds < attempt_timeout_seconds:
            raise ValueError(
                'total_timeout_seconds must be at least '
                'attempt_timeout_seconds'
            )

    @staticmethod
    def _require_finite_nonnegative(
        value: float,
        field_name: str,
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError(
                field_name + ' must be a finite non-negative number'
            )

    @staticmethod
    def _require_finite_positive(
        value: float,
        field_name: str,
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(
                field_name + ' must be a finite positive number'
            )

    def circuit_state(self, provider_index: int) -> CircuitState:
        """Return content-free circuit state for diagnostics."""
        with self._circuit_lock:
            return self._circuits[provider_index].state

    def _admit_provider(self, provider_index: int) -> bool:
        now = self._clock()
        with self._circuit_lock:
            circuit = self._circuits[provider_index]
            if circuit.state is CircuitState.CLOSED:
                return True
            if circuit.state is CircuitState.HALF_OPEN:
                return False
            if circuit.opened_at is None:
                return False
            if (
                now - circuit.opened_at
                < self._recovery_timeout_seconds
            ):
                return False
            circuit.state = CircuitState.HALF_OPEN
            return True

    def _record_success(self, provider_index: int) -> None:
        with self._circuit_lock:
            circuit = self._circuits[provider_index]
            circuit.state = CircuitState.CLOSED
            circuit.consecutive_failures = 0
            circuit.opened_at = None

    def _record_failure(
        self,
        provider_index: int,
        failure: ProviderFailure,
    ) -> None:
        with self._circuit_lock:
            circuit = self._circuits[provider_index]
            if circuit.state is CircuitState.HALF_OPEN:
                if not failure.affects_circuit:
                    circuit.state = CircuitState.CLOSED
                    circuit.consecutive_failures = 0
                    circuit.opened_at = None
                    return
                circuit.state = CircuitState.OPEN
                circuit.consecutive_failures = (
                    self._failure_threshold
                )
                circuit.opened_at = self._clock()
                return
            if not failure.affects_circuit:
                return
            circuit.consecutive_failures += 1
            if (
                circuit.consecutive_failures
                >= self._failure_threshold
            ):
                circuit.state = CircuitState.OPEN
                circuit.opened_at = self._clock()

    def _retry_delay(
        self,
        retry_index: int,
        error: BaseException,
    ) -> Optional[float]:
        delay = self._base_delay_seconds * (2 ** retry_index)
        retry_after = self._retry_after_seconds(error)
        if retry_after is not None:
            delay = max(delay, retry_after)
        if delay > self._max_delay_seconds:
            return None
        return delay

    @staticmethod
    def _retry_after_seconds(
        error: BaseException,
    ) -> Optional[float]:
        """Read a bounded delta-seconds Retry-After from an HTTP cause."""
        for item in _exception_chain(error):
            if not isinstance(item, urllib.error.HTTPError):
                continue
            headers = getattr(item, 'headers', None)
            if headers is None:
                continue
            raw_value = headers.get('Retry-After')
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return None
            if math.isfinite(value) and value >= 0:
                return value
            return None
        return None

    @staticmethod
    def _validated_result(result: object) -> ProviderResult:
        if not isinstance(result, ProviderResult):
            raise NormalizedProviderError(
                ProviderFailureCode.INVALID_RESPONSE
            )
        try:
            result.validate()
        except Exception as error:
            raise NormalizedProviderError(
                ProviderFailureCode.INVALID_RESPONSE
            ) from error
        return result

    def _complete_one(
        self,
        provider: AgentProvider,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary],
        trusted_server_tool_results: Sequence[TrustedToolResult],
        deadline: float,
    ) -> Tuple[Optional[ProviderResult], ProviderFailure]:
        failure = _FAILURES[ProviderFailureCode.UNKNOWN]
        for attempt in range(self._max_retries + 1):
            if (
                deadline - self._clock()
                < self._attempt_timeout_seconds
            ):
                break
            caught_error: Optional[BaseException] = None
            try:
                complete_with_context = getattr(
                    provider,
                    'complete_with_context',
                    None,
                )
                provider_type = type(provider)
                legacy_subclass_override = (
                    'complete' in provider_type.__dict__
                    and 'complete_with_context'
                    not in provider_type.__dict__
                    and getattr(
                        provider_type,
                        'complete_with_context',
                        None,
                    ) is not AgentProvider.complete_with_context
                )
                if (
                    callable(complete_with_context)
                    and not legacy_subclass_override
                ):
                    attempt_trusted_results = list(
                        self._trusted_result_snapshot(
                            trusted_server_tool_results
                        )
                    )
                    result = complete_with_context(
                        request,
                        memories,
                        conversation_turns,
                        tools,
                        conversation_summary,
                        trusted_server_tool_results=(
                            attempt_trusted_results
                        ),
                    )
                else:
                    result = provider.complete(
                        request,
                        memories,
                        conversation_turns,
                        tools,
                        conversation_summary,
                    )
                return self._validated_result(result), failure
            except Exception as error:
                caught_error = error
                failure = classify_exception(error)

            can_retry = (
                failure.transient
                and attempt < self._max_retries
            )
            if not can_retry:
                break
            if caught_error is None:
                break
            delay = self._retry_delay(attempt, caught_error)
            if delay is None:
                break
            if (
                self._clock()
                + delay
                + self._attempt_timeout_seconds
                > deadline
            ):
                break
            self._sleep(delay)
        return None, failure

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        """Return the first valid result or a safe non-action response."""
        return self.complete_with_context(
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary,
        )

    def complete_with_context(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
        trusted_server_tool_results: Sequence[TrustedToolResult] = (),
    ) -> ProviderResult:
        """Forward trusted result context through retry and fallback."""
        sealed_trusted_results = self._trusted_result_snapshot(
            trusted_server_tool_results
        )
        started_at = self._clock()
        deadline = started_at + self._total_timeout_seconds
        for index, provider in enumerate(self._providers):
            if (
                deadline - self._clock()
                < self._attempt_timeout_seconds
            ):
                break
            if not self._admit_provider(index):
                continue
            result, failure = self._complete_one(
                provider,
                request,
                memories,
                conversation_turns,
                tools,
                conversation_summary,
                sealed_trusted_results,
                deadline,
            )
            if result is not None:
                self._record_success(index)
                elapsed_ms = max(
                    0.0,
                    (self._clock() - started_at) * 1000.0,
                )
                return replace(result, latency_ms=elapsed_ms)
            self._record_failure(index, failure)
            if failure.code is ProviderFailureCode.AUTHENTICATION:
                break

        elapsed_ms = max(
            0.0,
            (self._clock() - started_at) * 1000.0,
        )
        decision = AgentDecision(
            type='refusal',
            message=self._SAFE_MESSAGE,
            reason='provider_unavailable',
            confidence=1.0,
        )
        decision.validate()
        return ProviderResult(
            decision=decision,
            provider='reliable-fallback',
            model='safe-non-action',
            latency_ms=elapsed_ms,
        )
