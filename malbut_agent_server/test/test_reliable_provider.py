"""Offline tests for provider retry, circuit breaker, and fallback."""

import urllib.error
from typing import Any, Dict, List, Optional

import pytest

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
)
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.providers.reliable import (
    CircuitState,
    NormalizedProviderError,
    ProviderFailureCode,
    ReliableProvider,
    classify_exception,
)
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
)
from malbut_agent_server.tools import ToolSpec, select_tool_specs


def _request() -> AgentRequest:
    return AgentRequest.from_dict(
        {
            'request_id': 'reliable-provider-test',
            'user_id': 'private-user',
            'conversation_id': 'private-conversation',
            'turn_id': 'private-turn',
            'utterance': '거실로 가줘',
            'robot_state': {
                'battery_percent': 80,
                'navigation_available': True,
                'localization_ok': True,
            },
            'available_tools': ['navigate'],
        }
    )


def _message_result(provider: str = 'test') -> ProviderResult:
    return ProviderResult(
        decision=AgentDecision(
            type='message',
            message='정상 응답',
            reason='test',
            confidence=1.0,
        ),
        provider=provider,
        model='offline-test',
        latency_ms=99.0,
    )


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedProvider(AgentProvider):
    def __init__(self, outcomes: List[object]) -> None:
        self.outcomes = list(outcomes)
        self.call_count = 0
        self.received_tools: List[List[ToolSpec]] = []

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        del request, memories, conversation_turns, conversation_summary
        self.call_count += 1
        self.received_tools.append(list(tools))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class _TimedTimeoutProvider(AgentProvider):
    """Advance a fake clock by one bounded attempt, then time out."""

    def __init__(self, fake_time: _FakeTime, duration: float) -> None:
        self.fake_time = fake_time
        self.duration = duration
        self.call_count = 0

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        del request, memories, conversation_turns, tools
        del conversation_summary
        self.call_count += 1
        self.fake_time.advance(self.duration)
        raise TimeoutError('content-free timed failure')


def _complete(provider: ReliableProvider) -> ProviderResult:
    return provider.complete(
        _request(),
        [],
        [],
        select_tool_specs(['navigate']),
    )


def test_classifies_existing_provider_errors_without_exposing_text() -> None:
    """Existing HTTP errors become content-free normalized failures."""
    secret = 'sk-test-never-expose'
    http_error = urllib.error.HTTPError(
        'https://private.example/' + secret,
        429,
        secret,
        {},
        None,
    )
    wrapped = ProviderError(
        'OpenAI request failed with HTTP status 429 ' + secret
    )
    wrapped.__cause__ = http_error

    failure = classify_exception(wrapped)

    assert failure.code is ProviderFailureCode.RATE_LIMIT
    assert failure.transient is True
    assert secret not in repr(failure)


def test_classifies_status_when_existing_error_lost_its_cause() -> None:
    """A sanitized status in legacy ProviderError remains classifiable."""
    failure = classify_exception(
        ProviderError('OpenAI request failed with HTTP status 503')
    )

    assert failure.code is ProviderFailureCode.UNAVAILABLE
    assert failure.transient is True


def test_retries_only_transient_failures_with_bounded_backoff() -> None:
    """Transient calls stop after the configured bounded retries."""
    fake_time = _FakeTime()
    primary = _ScriptedProvider(
        [
            TimeoutError('first private failure'),
            TimeoutError('second private failure'),
            _message_result('primary'),
        ]
    )
    provider = ReliableProvider(
        [primary],
        max_retries=2,
        base_delay_seconds=1.0,
        max_delay_seconds=2.0,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = _complete(provider)

    assert result.provider == 'primary'
    assert result.latency_ms == 3000.0
    assert primary.call_count == 3
    assert fake_time.sleeps == [1.0, 2.0]
    assert provider.circuit_state(0) is CircuitState.CLOSED


def test_rate_limit_retry_after_is_honored_within_bound() -> None:
    """A short server-requested delay overrides exponential backoff."""
    fake_time = _FakeTime()
    http_error = urllib.error.HTTPError(
        'https://api.openai.com/v1/responses',
        429,
        'rate limited',
        {'Retry-After': '1.5'},
        None,
    )
    wrapped = ProviderError(
        'OpenAI request failed with HTTP status 429'
    )
    wrapped.__cause__ = http_error
    primary = _ScriptedProvider(
        [wrapped, _message_result('primary')]
    )
    provider = ReliableProvider(
        [primary],
        max_retries=1,
        base_delay_seconds=0.25,
        max_delay_seconds=2.0,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = _complete(provider)

    assert result.provider == 'primary'
    assert primary.call_count == 2
    assert fake_time.sleeps == [1.5]


def test_retry_after_beyond_latency_budget_is_not_retried() -> None:
    """A long rate-limit wait fails closed instead of blocking voice UX."""
    fake_time = _FakeTime()
    http_error = urllib.error.HTTPError(
        'https://api.openai.com/v1/responses',
        429,
        'rate limited',
        {'Retry-After': '30'},
        None,
    )
    wrapped = ProviderError(
        'OpenAI request failed with HTTP status 429'
    )
    wrapped.__cause__ = http_error
    primary = _ScriptedProvider(
        [wrapped, _message_result('must-not-run')]
    )
    provider = ReliableProvider(
        [primary],
        max_retries=1,
        base_delay_seconds=0.25,
        max_delay_seconds=2.0,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = _complete(provider)

    assert result.provider == 'reliable-fallback'
    assert primary.call_count == 1
    assert fake_time.sleeps == []


def test_wraps_openai_adapter_without_changing_provider_contract() -> None:
    """The existing adapter can be retried and still returns its schema."""
    fake_time = _FakeTime()
    call_count = 0

    def transport(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        nonlocal call_count
        del url, headers, payload, timeout
        call_count += 1
        if call_count == 1:
            http_error = urllib.error.HTTPError(
                'https://api.openai.com/v1/responses',
                503,
                'unavailable',
                {},
                None,
            )
            raise ProviderError(
                'OpenAI request failed with HTTP status 503'
            ) from http_error
        return {
            'id': 'offline-response',
            'status': 'completed',
            'model': 'offline-openai-model',
            'output': [
                {
                    'type': 'function_call',
                    'call_id': 'offline-call',
                    'name': 'navigate',
                    'arguments': '{"location":"거실"}',
                }
            ],
            'usage': {
                'input_tokens': 10,
                'output_tokens': 3,
                'total_tokens': 13,
            },
        }

    openai = OpenAIResponsesProvider(
        api_key='test-only-key',
        model='offline-openai-model',
        transport=transport,
    )
    provider = ReliableProvider(
        [openai],
        max_retries=1,
        base_delay_seconds=0.25,
        max_delay_seconds=0.25,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = _complete(provider)

    assert call_count == 2
    assert fake_time.sleeps == [0.25]
    assert result.provider == 'openai'
    assert result.decision.type == 'tool_call'
    assert result.decision.tool_name == 'navigate'
    assert result.decision.arguments == {'location': '거실'}
    assert result.usage.total_tokens == 13


def test_authentication_failure_skips_retry_and_same_vendor_fallback() -> None:
    """A shared credential failure stops the ordered model chain."""
    fake_time = _FakeTime()
    primary = _ScriptedProvider(
        [
            NormalizedProviderError(
                ProviderFailureCode.AUTHENTICATION
            ),
            _message_result('must-not-run'),
        ]
    )
    fallback = _ScriptedProvider([_message_result('fallback')])
    provider = ReliableProvider(
        [primary, fallback],
        max_retries=2,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = _complete(provider)

    assert result.provider == 'reliable-fallback'
    assert primary.call_count == 1
    assert fallback.call_count == 0
    assert fake_time.sleeps == []


def test_circuit_opens_skips_primary_and_recovers_half_open() -> None:
    """Only one post-timeout call probes an open provider."""
    fake_time = _FakeTime()
    primary = _ScriptedProvider(
        [
            TimeoutError('private-1'),
            TimeoutError('private-2'),
            _message_result('primary'),
        ]
    )
    fallback = _ScriptedProvider(
        [
            _message_result('fallback'),
            _message_result('fallback'),
            _message_result('fallback'),
        ]
    )
    provider = ReliableProvider(
        [primary, fallback],
        max_retries=0,
        failure_threshold=2,
        recovery_timeout_seconds=10.0,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    assert _complete(provider).provider == 'fallback'
    assert provider.circuit_state(0) is CircuitState.CLOSED
    assert _complete(provider).provider == 'fallback'
    assert provider.circuit_state(0) is CircuitState.OPEN
    assert _complete(provider).provider == 'fallback'
    assert primary.call_count == 2

    fake_time.advance(10.0)
    assert _complete(provider).provider == 'primary'
    assert primary.call_count == 3
    assert provider.circuit_state(0) is CircuitState.CLOSED


def test_non_circuit_half_open_failure_restores_closed_state() -> None:
    """A reachable provider's request error must not extend an outage."""
    fake_time = _FakeTime()
    primary = _ScriptedProvider(
        [
            TimeoutError('open circuit'),
            NormalizedProviderError(
                ProviderFailureCode.INVALID_REQUEST
            ),
            _message_result('primary'),
        ]
    )
    fallback = _ScriptedProvider(
        [
            _message_result('fallback'),
            _message_result('fallback'),
        ]
    )
    provider = ReliableProvider(
        [primary, fallback],
        max_retries=0,
        failure_threshold=1,
        recovery_timeout_seconds=10.0,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    assert _complete(provider).provider == 'fallback'
    assert provider.circuit_state(0) is CircuitState.OPEN
    fake_time.advance(10.0)
    assert _complete(provider).provider == 'fallback'
    assert provider.circuit_state(0) is CircuitState.CLOSED
    assert _complete(provider).provider == 'primary'


def test_total_budget_limits_ordered_provider_attempts() -> None:
    """Do not start another full attempt outside the scheduling budget."""
    fake_time = _FakeTime()
    providers = [
        _TimedTimeoutProvider(fake_time, 5.0)
        for _index in range(3)
    ]
    provider = ReliableProvider(
        providers,
        max_retries=0,
        attempt_timeout_seconds=5.0,
        total_timeout_seconds=11.0,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = _complete(provider)

    assert result.provider == 'reliable-fallback'
    assert result.decision.type == 'refusal'
    assert fake_time.now == 10.0
    assert [item.call_count for item in providers] == [1, 1, 0]


def test_invalid_normalized_result_falls_back_without_retry() -> None:
    """Malformed decisions are permanent failures for one request."""
    invalid = ProviderResult(
        decision=AgentDecision(
            type='tool_call',
            message='invalid',
            tool_name=None,
        ),
        provider='invalid',
        model='invalid',
        latency_ms=1.0,
    )
    primary = _ScriptedProvider([invalid, _message_result('unused')])
    fallback = _ScriptedProvider([_message_result('fallback')])
    provider = ReliableProvider(
        [primary, fallback],
        max_retries=2,
    )

    result = _complete(provider)

    assert result.provider == 'fallback'
    assert primary.call_count == 1
    assert fallback.call_count == 1


def test_all_failures_return_safe_non_action_without_error_details() -> None:
    """Exhaustion cannot expose credentials or produce a tool action."""
    secret = 'sk-live-private-value'
    fake_time = _FakeTime()
    primary = _ScriptedProvider(
        [
            TimeoutError(secret),
            TimeoutError(secret),
            TimeoutError(secret),
        ]
    )
    fallback = _ScriptedProvider(
        [
            ProviderError(
                'private endpoint and credential: ' + secret
            )
        ]
    )
    provider = ReliableProvider(
        [primary, fallback],
        max_retries=2,
        base_delay_seconds=0.5,
        max_delay_seconds=1.0,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = _complete(provider)
    public_output = {
        'decision': result.decision.to_dict(),
        'provider': result.to_dict(),
    }

    assert primary.call_count == 3
    assert fallback.call_count == 1
    assert result.decision.type == 'refusal'
    assert result.decision.tool_name is None
    assert result.decision.arguments == {}
    assert result.decision.reason == 'provider_unavailable'
    assert result.provider == 'reliable-fallback'
    assert secret not in repr(public_output)
    assert 'private endpoint' not in repr(public_output)


@pytest.mark.parametrize(
    'keyword,value',
    [
        ('max_retries', -1),
        ('max_retries', 11),
        ('base_delay_seconds', -0.1),
        ('max_delay_seconds', float('inf')),
        ('failure_threshold', 0),
        ('recovery_timeout_seconds', 0),
    ],
)
def test_rejects_unbounded_or_invalid_configuration(
    keyword: str,
    value: object,
) -> None:
    """Reliability bounds cannot be disabled by invalid configuration."""
    with pytest.raises(ValueError):
        ReliableProvider(
            [_ScriptedProvider([_message_result()])],
            **{keyword: value},
        )


@pytest.mark.parametrize(
    ('status_code', 'expected'),
    (
        (408, ProviderFailureCode.TIMEOUT),
        (401, ProviderFailureCode.AUTHENTICATION),
        (400, ProviderFailureCode.INVALID_REQUEST),
        (418, ProviderFailureCode.INVALID_REQUEST),
        (200, ProviderFailureCode.UNKNOWN),
    ),
)
def test_classifies_http_status_families_without_response_text(
    status_code: int,
    expected: ProviderFailureCode,
) -> None:
    """HTTP categories normalize without retaining a response body."""
    error = urllib.error.HTTPError(
        'https://api.openai.com/v1/responses',
        status_code,
        'private response text',
        {},
        None,
    )
    assert classify_exception(error).code is expected


@pytest.mark.parametrize(
    ('error', 'expected'),
    (
        (
            urllib.error.URLError(TimeoutError('private timeout')),
            ProviderFailureCode.TIMEOUT,
        ),
        (
            urllib.error.URLError('private DNS failure'),
            ProviderFailureCode.NETWORK,
        ),
        (ConnectionError('private connection'), ProviderFailureCode.NETWORK),
        (
            ValueError('private malformed output'),
            ProviderFailureCode.INVALID_RESPONSE,
        ),
        (RuntimeError('private unknown'), ProviderFailureCode.UNKNOWN),
    ),
)
def test_classifies_non_http_failure_families(
    error: BaseException,
    expected: ProviderFailureCode,
) -> None:
    """Timeout, transport, validation, and unknown failures stay distinct."""
    assert classify_exception(error).code is expected


def test_rejects_missing_or_non_provider_entries() -> None:
    """Fallback chains require at least one callable provider."""
    with pytest.raises(ValueError, match='at least one provider'):
        ReliableProvider([])
    with pytest.raises(TypeError, match='must implement complete'):
        ReliableProvider([object()])


@pytest.mark.parametrize(
    ('overrides', 'message'),
    (
        (
            {'base_delay_seconds': 2.0, 'max_delay_seconds': 1.0},
            'max_delay_seconds must be at least',
        ),
        (
            {
                'attempt_timeout_seconds': 2.0,
                'total_timeout_seconds': 1.0,
            },
            'total_timeout_seconds must be at least',
        ),
    ),
)
def test_rejects_internally_inconsistent_timeout_configuration(
    overrides,
    message: str,
) -> None:
    """Individually valid limits must also form a schedulable budget."""
    with pytest.raises(ValueError, match=message):
        ReliableProvider(
            [_ScriptedProvider([_message_result()])],
            **overrides,
        )


@pytest.mark.parametrize('retry_after', (None, 'invalid', '-1'))
def test_invalid_retry_after_metadata_is_ignored(retry_after) -> None:
    """Missing, malformed, or negative retry metadata never adds delay."""
    headers = None
    if retry_after is not None:
        headers = {'Retry-After': retry_after}
    error = urllib.error.HTTPError(
        'https://api.openai.com/v1/responses',
        429,
        'rate limited',
        headers,
        None,
    )
    assert ReliableProvider._retry_after_seconds(error) is None


def test_raw_non_result_falls_back_without_retry() -> None:
    """A provider returning an arbitrary object is a permanent bad response."""
    primary = _ScriptedProvider([object(), _message_result('unused')])
    fallback = _ScriptedProvider([_message_result('fallback')])
    provider = ReliableProvider(
        [primary, fallback],
        max_retries=2,
    )

    result = _complete(provider)

    assert result.provider == 'fallback'
    assert primary.call_count == 1
    assert fallback.call_count == 1


def test_invalid_request_does_not_open_closed_circuit() -> None:
    """Caller errors fall back but do not count as provider outages."""
    primary = _ScriptedProvider(
        [NormalizedProviderError(ProviderFailureCode.INVALID_REQUEST)]
    )
    fallback = _ScriptedProvider([_message_result('fallback')])
    provider = ReliableProvider(
        [primary, fallback],
        max_retries=0,
        failure_threshold=1,
    )

    assert _complete(provider).provider == 'fallback'
    assert provider.circuit_state(0) is CircuitState.CLOSED
