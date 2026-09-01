"""Contracts for server-owned canonical utterance resolution."""

import hashlib
import json
from typing import Callable, Optional, Sequence

import pytest

from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.memory import MemoryRecord, SQLiteMemoryStore
from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    ServerClarification,
)
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    MAX_UTTERANCE_LENGTH,
    AgentDecision,
    AgentRequest,
    ProviderResult,
    ValidationError,
)
from malbut_agent_server.tools import ToolSpec


class RecordingProvider(AgentProvider):
    """Record effective requests and propose navigation only when explicit."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[AgentRequest] = []
        self.histories: list[list[ConversationTurn]] = []

    def complete(
        self,
        request: AgentRequest,
        memories: list[MemoryRecord],
        conversation_turns: list[ConversationTurn],
        tools: list[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        del memories, tools, conversation_summary
        self.calls += 1
        self.requests.append(request)
        self.histories.append(list(conversation_turns))
        decision = (
            AgentDecision(
                type='tool_call',
                message='거실로 이동할까요?',
                tool_name='navigate',
                arguments={'location': '거실'},
            )
            if request.utterance == '거실로 가줘'
            else AgentDecision(
                type='message',
                message=f'확인했어: {request.utterance}',
            )
        )
        return ProviderResult(
            decision=decision,
            provider='utterance-resolver-fixture',
            model='fixture',
            latency_ms=0,
        )


def _request(
    utterance: str,
    *,
    request_id: str = 'request-1',
    turn_id: str = 'turn-1',
) -> AgentRequest:
    return AgentRequest.from_dict({
        'request_id': request_id,
        'user_id': 'user-1',
        'conversation_id': 'conversation-1',
        'turn_id': turn_id,
        'utterance': utterance,
        'robot_state': {
            'battery_percent': 80,
            'navigation_available': True,
            'localization_ok': True,
            'emergency_stop': False,
        },
        'available_tools': ['navigate'],
    })


def _runtime() -> tuple[
    AgentOrchestrator,
    RecordingProvider,
    SQLiteConversationStore,
    SQLiteMemoryStore,
]:
    provider = RecordingProvider()
    memory = SQLiteMemoryStore(':memory:')
    conversations = SQLiteConversationStore(':memory:')
    conversations.create('user-1', 'conversation-1')
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory,
        conversation_store=conversations,
        safety_policy=SafetyPolicy(),
        trusted_robot_state=True,
    )
    return orchestrator, provider, conversations, memory


def _fingerprint(request: AgentRequest) -> str:
    encoded = json.dumps(
        request.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    'resolver',
    [None, lambda _request, _history, _token: None],
    ids=['no-hook', 'none-result'],
)
def test_no_resolution_preserves_the_original_request(
    resolver: Optional[
        Callable[
            [AgentRequest, Sequence[ConversationTurn], BeginTurnToken],
            str | None,
        ]
    ],
) -> None:
    orchestrator, provider, conversations, memory = _runtime()
    request = _request('원본 요청')
    try:
        result = orchestrator.handle(
            request,
            utterance_resolver=resolver,
        )

        assert result.decision.message == '확인했어: 원본 요청'
        assert provider.calls == 1
        assert provider.requests[0] == request
        turns = conversations.list_turns('user-1', 'conversation-1')
        assert len(turns) == 1
        assert turns[0].user_content == '원본 요청'
        assert turns[0].request_fingerprint == _fingerprint(request)
    finally:
        conversations.close()
        memory.close()


def test_server_clarification_skips_provider_and_replays_durably() -> None:
    orchestrator, provider, conversations, memory = _runtime()
    request = _request('여기로 가줘')
    clarification = ServerClarification(
        message='등록된 공간 이름 하나를 말해 주세요.',
        code='navigation_destination_missing',
        policy_revision='navigation-clarification-v1',
    )
    try:
        first = orchestrator.handle(
            request,
            server_clarification=clarification,
        )
        replay = orchestrator.handle(
            request,
            server_clarification=clarification,
        )

        assert provider.calls == 0
        assert first.decision.type == 'clarification'
        assert first.raw_decision == first.decision
        assert first.safety.allowed is True
        assert first.safety.code == 'not_an_action'
        assert first.state_trusted is False
        assert first.provider_result.provider == 'malbut-server-policy'
        assert first.provider_result.model == (
            'navigation-clarification-v1'
        )
        assert replay.decision_id == first.decision_id
        assert len(
            conversations.list_turns('user-1', 'conversation-1')
        ) == 1
    finally:
        conversations.close()
        memory.close()


def test_invalid_server_clarification_is_rejected_before_begin_turn() -> None:
    orchestrator, provider, conversations, memory = _runtime()
    request = _request('여기로 가줘')
    try:
        with pytest.raises(TypeError, match='ServerClarification'):
            orchestrator.handle(
                request,
                server_clarification=object(),
            )

        assert provider.calls == 0
        assert conversations.list_turns(
            'user-1', 'conversation-1'
        ) == []
    finally:
        conversations.close()
        memory.close()


@pytest.mark.parametrize(
    'values',
    (
        {'message': '', 'code': 'valid', 'policy_revision': 'v1'},
        {'message': '질문', 'code': 'bad code', 'policy_revision': 'v1'},
        {'message': '질문', 'code': 'valid', 'policy_revision': 'v 1'},
    ),
    ids=['empty-message', 'invalid-code', 'invalid-revision'],
)
def test_server_clarification_rejects_malformed_fields(values: dict) -> None:
    with pytest.raises(ValueError):
        ServerClarification(**values)


def test_resolver_receives_consistent_history_and_changes_only_utterance(
) -> None:
    orchestrator, provider, conversations, memory = _runtime()
    try:
        orchestrator.handle(
            _request('첫 번째 요청', request_id='request-1', turn_id='turn-1')
        )
        before = conversations.get('user-1', 'conversation-1')
        request = _request(
            '두 번째 선택지',
            request_id='request-2',
            turn_id='turn-2',
        )
        observed: dict[str, object] = {}

        def resolve(
            candidate: AgentRequest,
            history: Sequence[ConversationTurn],
            token: BeginTurnToken,
        ) -> str:
            observed['request'] = candidate
            observed['history'] = tuple(history)
            observed['token'] = token
            object.__setattr__(candidate, 'user_id', 'attacker')
            object.__setattr__(candidate, 'available_tools', ('shell',))
            object.__setattr__(candidate.robot_state, 'emergency_stop', True)
            return '거실로 가줘'

        result = orchestrator.handle(
            request,
            utterance_resolver=resolve,
        )

        history = observed['history']
        token = observed['token']
        assert isinstance(history, tuple)
        assert len(history) == 1
        assert history[0].user_content == '첫 번째 요청'
        assert history[0].session_instance_id == before.session_instance_id
        assert isinstance(token, BeginTurnToken)
        assert token.session_instance_id == before.session_instance_id
        assert token.generation == before.generation == 1
        assert token.revision == before.revision == 1
        assert token.ordinal == 2

        effective = provider.requests[-1]
        assert effective.utterance == '거실로 가줘'
        assert effective.user_id == request.user_id
        assert effective.request_id == request.request_id
        assert effective.conversation_id == request.conversation_id
        assert effective.turn_id == request.turn_id
        assert effective.available_tools == request.available_tools
        assert effective.robot_state.emergency_stop is False
        assert result.raw_decision.tool_name == 'navigate'
        assert result.safety.allowed is True

        turns = conversations.list_turns('user-1', 'conversation-1')
        assert [turn.user_content for turn in turns] == [
            '첫 번째 요청',
            '두 번째 선택지',
        ]
        assert turns[-1].request_fingerprint == _fingerprint(request)
    finally:
        conversations.close()
        memory.close()


def test_exact_cached_retry_skips_resolver_and_provider() -> None:
    orchestrator, provider, conversations, memory = _runtime()
    request = _request('두 번째 선택지')
    resolver_calls = 0

    def resolve(
        _request: AgentRequest,
        _history: Sequence[ConversationTurn],
        _token: BeginTurnToken,
    ) -> str:
        nonlocal resolver_calls
        resolver_calls += 1
        return '거실로 가줘'

    def unexpected_resolver(
        _request: AgentRequest,
        _history: Sequence[ConversationTurn],
        _token: BeginTurnToken,
    ) -> str:
        raise AssertionError('cached retry called utterance_resolver')

    try:
        first = orchestrator.handle(request, utterance_resolver=resolve)
        replay = orchestrator.handle(
            request,
            utterance_resolver=unexpected_resolver,
        )

        assert resolver_calls == 1
        assert provider.calls == 1
        assert replay.decision_id == first.decision_id
        assert replay.raw_decision == first.raw_decision
        turns = conversations.list_turns('user-1', 'conversation-1')
        assert len(turns) == 1
        assert turns[0].user_content == '두 번째 선택지'
        assert turns[0].request_fingerprint == _fingerprint(request)
    finally:
        conversations.close()
        memory.close()


@pytest.mark.parametrize(
    ('resolved', 'error_type'),
    [
        (42, TypeError),
        ('', ValidationError),
        ('   ', ValidationError),
        ('가' * (MAX_UTTERANCE_LENGTH + 1), ValidationError),
    ],
    ids=['non-string', 'empty', 'blank', 'too-long'],
)
def test_invalid_resolver_result_cleans_pending_turn(
    resolved: object,
    error_type: type[Exception],
) -> None:
    orchestrator, provider, conversations, memory = _runtime()
    request = _request('원본 요청')
    try:
        with pytest.raises(error_type):
            orchestrator.handle(
                request,
                utterance_resolver=(
                    lambda _request, _history, _token: resolved
                ),
            )

        assert provider.calls == 0
        assert conversations.list_turns(
            'user-1', 'conversation-1'
        ) == []
        assert conversations._connection.execute(
            'SELECT COUNT(*) FROM conversation_turns'
        ).fetchone()[0] == 0

        retry = orchestrator.handle(request)
        assert retry.decision.type == 'message'
        assert provider.calls == 1
        assert len(
            conversations.list_turns('user-1', 'conversation-1')
        ) == 1
    finally:
        conversations.close()
        memory.close()


def test_resolver_exception_cleans_pending_turn() -> None:
    orchestrator, provider, conversations, memory = _runtime()
    request = _request('원본 요청')

    def fail(
        _request: AgentRequest,
        _history: Sequence[ConversationTurn],
        _token: BeginTurnToken,
    ) -> str:
        raise RuntimeError('resolver failed')

    try:
        with pytest.raises(RuntimeError, match='resolver failed'):
            orchestrator.handle(request, utterance_resolver=fail)

        assert provider.calls == 0
        assert conversations.list_turns(
            'user-1', 'conversation-1'
        ) == []
        assert conversations._connection.execute(
            'SELECT COUNT(*) FROM conversation_turns'
        ).fetchone()[0] == 0

        orchestrator.handle(request)
        assert provider.calls == 1
        assert len(
            conversations.list_turns('user-1', 'conversation-1')
        ) == 1
    finally:
        conversations.close()
        memory.close()
