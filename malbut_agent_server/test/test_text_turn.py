"""End-to-end application tests for SWM25-131 text confirmation."""

import time

import pytest

from malbut_agent_server.conversation import (
    ConfirmationIntentConflictError,
    ConversationConflictError,
    SQLiteConversationStore,
)
from malbut_agent_server.gateway import production_registry
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.named_target import BoundNamedTarget
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.robot_state_source import (
    StaticSimulationRobotStateSource,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import RobotState, ValidationError
from malbut_agent_server.text_turn import TextTurnService


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


class CountingMockProvider(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        return super().complete(*args, **kwargs)


class MutableTargetResolver:
    def __init__(self) -> None:
        self.binding_digest = 'a' * 64
        self.available = True
        self.calls = 0

    def resolve(self, location: str) -> BoundNamedTarget:
        self.calls += 1
        if not self.available or location != '거실':
            raise ValueError('target unavailable')
        return BoundNamedTarget(
            room_name='거실',
            room_category='living_room',
            binding_digest=self.binding_digest,
        )


def _runtime(
    tmp_path,
    *,
    clock=None,
    maximum_confirmation_seconds=30.0,
):
    clock = clock or Clock()
    database = str(tmp_path / 'text-turn.sqlite3')
    memory = SQLiteMemoryStore(database)
    conversations = SQLiteConversationStore(database, clock=clock)
    provider = CountingMockProvider()
    resolver = MutableTargetResolver()
    state = RobotState(
        battery_percent=90.0,
        navigation_available=True,
        localization_ok=True,
        emergency_stop=False,
    )
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory,
        conversation_store=conversations,
        safety_policy=SafetyPolicy(),
        capability_registry=production_registry(),
        robot_state_source=StaticSimulationRobotStateSource(
            state,
            clock=clock,
        ),
        state_clock=clock,
    )
    service = TextTurnService(
        orchestrator,
        resolver,
        clock=clock,
        maximum_confirmation_seconds=maximum_confirmation_seconds,
    )
    return service, provider, resolver, conversations, memory, database


def _request(
    request_id: str,
    turn_id: str,
    text: str,
    conversation_id: str = 'conversation-1',
) -> dict:
    return {
        'request_id': request_id,
        'conversation_id': conversation_id,
        'turn_id': turn_id,
        'text': text,
    }


def test_request_ambiguous_confirmation_and_approval_call_llm_once(
    tmp_path,
) -> None:
    service, provider, resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        proposal = service.handle(
            user_id='user-1',
            value=_request('request-1', 'turn-1', '거실로 가줘'),
        )
        assert proposal['status'] == 'awaiting_confirmation'
        assert proposal['proposal']['arguments'] == {'location': '거실'}
        assert proposal['expires_at'] - proposal['issued_at'] == 30.0
        source_turn = store.list_turns('user-1', 'conversation-1')[0]
        source_expiry = source_turn.response['public']['execution'][
            'expires_at'
        ]
        assert source_expiry - source_turn.response['public'][
            'execution'
        ]['issued_at'] == 5.0
        assert source_expiry < proposal['expires_at']
        pending = store.pending_confirmation(
            'user-1',
            'conversation-1',
        )
        assert pending is not None
        assert pending.state_evidence_id == (
            'swm25-131-static-simulation-state'
        )
        assert pending.safety_policy_revision == 'malbut-safety-v1'
        assert provider.calls == 1
        assert resolver.calls == 1

        ambiguous = service.handle(
            user_id='user-1',
            value=_request('response-ambiguous', 'turn-2', '글쎄'),
        )
        assert ambiguous['status'] == 'awaiting_confirmation'
        assert (
            ambiguous['result_code']
            == 'confirmation_response_unrecognized'
        )
        assert provider.calls == 1

        approved = service.handle(
            user_id='user-1',
            value=_request('response-1', 'turn-3', '네'),
        )
        assert approved['status'] == 'approved'
        assert approved['result_code'] == 'confirmation_approved'
        assert approved['execution']['authorized'] is False
        assert approved['execution']['consume_once'] is False
        assert approved['execution']['tool_call_id'] is None
        assert approved['execution']['physical_authorized'] is False
        assert approved['execution']['nav2_start_count'] == 0
        assert provider.calls == 1

        replay = service.handle(
            user_id='user-1',
            value=_request('response-1', 'turn-3', '네'),
        )
        assert replay['status'] == 'approved'
        assert replay['cached'] is True
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_fixed_clock_and_one_second_confirmation_window_are_supported(
    tmp_path,
) -> None:
    class FixedClock:
        def __call__(self) -> float:
            return 100.0

    clock = FixedClock()
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path,
        clock=clock,
        maximum_confirmation_seconds=1.0,
    )
    try:
        store.create('user-1', 'conversation-1')
        proposal = service.handle(
            user_id='user-1',
            value=_request('fixed-request', 'fixed-turn', '거실로 가줘'),
        )
        assert proposal['status'] == 'awaiting_confirmation'
        assert proposal['issued_at'] == 100.0
        assert proposal['expires_at'] == 101.0
        source = store.list_turns('user-1', 'conversation-1')[0]
        assert source.response['public']['execution']['expires_at'] == 105.0
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_explicit_response_without_pending_never_calls_provider(
    tmp_path,
) -> None:
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        result = service.handle(
            user_id='user-1',
            value=_request('late-yes', 'turn-1', '네'),
        )
        assert result['status'] == 'no_pending_confirmation'
        assert result['execution']['nav2_start_count'] == 0
        assert service.handle(
            user_id='user-1',
            value=_request('late-yes', 'turn-1', '네'),
        ) == result
        with pytest.raises(ConfirmationIntentConflictError):
            service.handle(
                user_id='user-1',
                value=_request(
                    'late-yes',
                    'turn-1',
                    '거실로 가줘',
                ),
            )
        assert provider.calls == 0
    finally:
        store.close()
        memory.close()


def test_pending_confirmation_survives_restart(tmp_path) -> None:
    service, provider, resolver, store, memory, database = _runtime(
        tmp_path
    )
    store.create('user-1', 'conversation-1')
    service.handle(
        user_id='user-1',
        value=_request('request-1', 'turn-1', '거실로 가줘'),
    )
    store.close()
    memory.close()

    memory = SQLiteMemoryStore(database)
    store = SQLiteConversationStore(database)
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory,
        conversation_store=store,
        safety_policy=SafetyPolicy(),
        capability_registry=production_registry(),
        robot_state_source=StaticSimulationRobotStateSource(
            RobotState(
                battery_percent=90.0,
                navigation_available=True,
                localization_ok=True,
            )
        ),
    )
    service = TextTurnService(orchestrator, resolver)
    try:
        approved = service.handle(
            user_id='user-1',
            value=_request('response-after-restart', 'turn-2', '네'),
        )
        assert approved['status'] == 'approved'
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_wrong_conversation_does_not_consume_pending_confirmation(
    tmp_path,
) -> None:
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        store.create('user-1', 'conversation-2')
        service.handle(
            user_id='user-1',
            value=_request('request-1', 'turn-1', '거실로 가줘'),
        )
        wrong = service.handle(
            user_id='user-1',
            value=_request(
                'wrong-session-response',
                'turn-1',
                '네',
                conversation_id='conversation-2',
            ),
        )
        assert wrong['status'] == 'no_pending_confirmation'
        assert store.pending_confirmation(
            'user-1', 'conversation-1'
        ) is not None
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_target_binding_change_invalidates_only_pending_ticket(
    tmp_path,
) -> None:
    service, provider, resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        service.handle(
            user_id='user-1',
            value=_request('request-1', 'turn-1', '거실로 가줘'),
        )
        resolver.binding_digest = 'b' * 64
        result = service.handle(
            user_id='user-1',
            value=_request('response-1', 'turn-2', '네'),
        )
        assert result['status'] == 'invalidated'
        assert result['result_code'] == 'confirmation_target_changed'
        assert store.pending_confirmation(
            'user-1', 'conversation-1'
        ) is None
        replay = service.handle(
            user_id='user-1',
            value=_request('response-1', 'turn-2', '네'),
        )
        assert replay['status'] == 'invalidated'
        assert replay['cached'] is True
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_unresolved_target_is_persisted_as_refusal_without_ticket(
    tmp_path,
) -> None:
    service, provider, resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        resolver.available = False
        result = service.handle(
            user_id='user-1',
            value=_request('request-1', 'turn-1', '거실로 가줘'),
        )
        assert result['status'] == 'completed'
        assert result['decision']['type'] == 'refusal'
        assert result['safety']['code'] == 'named_target_unavailable'
        assert result['execution']['authorized'] is False
        assert result['execution']['execution_authorized'] is False
        assert store.pending_confirmation(
            'user-1', 'conversation-1'
        ) is None
        assert provider.calls == 1

        cached = service.handle(
            user_id='user-1',
            value=_request('request-1', 'turn-1', '거실로 가줘'),
        )
        assert cached['decision']['type'] == 'refusal'
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_authority_and_user_fields_are_rejected_from_text_body(
    tmp_path,
) -> None:
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        base = _request('request-1', 'turn-1', '거실로 가줘')
        for injected in (
            {'user_id': 'other-user'},
            {'robot_state': {'navigation_available': True}},
            {'approved': True},
            {'goal_id': 'caller-goal'},
        ):
            with pytest.raises(ValidationError, match='unknown fields'):
                service.handle(
                    user_id='user-1',
                    value={**base, **injected},
                )
        assert provider.calls == 0
    finally:
        store.close()
        memory.close()


def test_ambiguous_response_is_durably_claimed_and_cannot_become_yes(
    tmp_path,
) -> None:
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        service.handle(
            user_id='user-1',
            value=_request('proposal-1', 'turn-1', '거실로 가줘'),
        )
        ambiguous_request = _request(
            'ambiguous-1',
            'turn-2',
            '글쎄',
        )
        first = service.handle(
            user_id='user-1',
            value=ambiguous_request,
        )
        replay = service.handle(
            user_id='user-1',
            value=ambiguous_request,
        )

        assert first['result_code'] == (
            'confirmation_response_unrecognized'
        )
        assert replay['result_code'] == first['result_code']
        assert replay['cached'] is True
        with pytest.raises(ConfirmationIntentConflictError):
            service.handle(
                user_id='user-1',
                value=_request('ambiguous-1', 'turn-2', '네'),
            )
        assert store.pending_confirmation(
            'user-1',
            'conversation-1',
        ) is not None
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_terminal_response_replay_binds_response_turn_id(tmp_path) -> None:
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        service.handle(
            user_id='user-1',
            value=_request('proposal-1', 'turn-1', '거실로 가줘'),
        )
        service.handle(
            user_id='user-1',
            value=_request('answer-1', 'turn-2', '네'),
        )

        with pytest.raises(ConfirmationIntentConflictError):
            service.handle(
                user_id='user-1',
                value=_request('answer-1', 'forged-turn', '네'),
            )
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_terminal_response_exact_replay_survives_later_turn(
    tmp_path,
) -> None:
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        service.handle(
            user_id='user-1',
            value=_request('proposal-1', 'turn-1', '거실로 가줘'),
        )
        approval = _request('answer-1', 'turn-2', '네')
        first = service.handle(user_id='user-1', value=approval)
        later = service.handle(
            user_id='user-1',
            value=_request('request-2', 'turn-3', '안녕하세요'),
        )
        replay = service.handle(user_id='user-1', value=approval)

        assert first['status'] == 'approved'
        assert later['status'] == 'completed'
        assert replay['status'] == 'approved'
        assert replay['cached'] is True
        assert replay['execution']['execution_authorized'] is False
        assert provider.calls == 2
    finally:
        store.close()
        memory.close()


def test_agent_and_confirmation_request_ids_share_one_namespace(
    tmp_path,
) -> None:
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        proposal_request = _request(
            'shared-request',
            'turn-1',
            '거실로 가줘',
        )
        service.handle(user_id='user-1', value=proposal_request)

        with pytest.raises(ConversationConflictError):
            service.handle(
                user_id='user-1',
                value=_request('shared-request', 'turn-2', '네'),
            )
        with pytest.raises(ConfirmationIntentConflictError):
            service.handle(
                user_id='user-1',
                value=_request('new-response', 'turn-1', '네'),
            )
        replay = service.handle(
            user_id='user-1',
            value=proposal_request,
        )
        assert replay['status'] == 'awaiting_confirmation'
        assert store.pending_confirmation(
            'user-1',
            'conversation-1',
        ) is not None
        assert provider.calls == 1
    finally:
        store.close()
        memory.close()


def test_confirmation_claim_cannot_be_reused_as_agent_request(
    tmp_path,
) -> None:
    service, provider, _resolver, store, memory, _database = _runtime(
        tmp_path
    )
    try:
        store.create('user-1', 'conversation-1')
        service.handle(
            user_id='user-1',
            value=_request('claimed-response', 'turn-1', '네'),
        )

        with pytest.raises(ConfirmationIntentConflictError):
            service.handle(
                user_id='user-1',
                value=_request(
                    'claimed-response',
                    'turn-2',
                    '거실로 가줘',
                ),
            )
        with pytest.raises(ConversationConflictError):
            service.handle(
                user_id='user-1',
                value=_request(
                    'new-agent-request',
                    'turn-1',
                    '거실로 가줘',
                ),
            )
        assert provider.calls == 0
    finally:
        store.close()
        memory.close()
