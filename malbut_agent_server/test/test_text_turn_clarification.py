"""Text-turn integration contracts for one-hop navigation clarification."""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from malbut_agent_server.adapters.outbound import SQLiteActionRepository
from malbut_agent_server.conversation import (
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
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    RobotState,
)
from malbut_agent_server.text_turn import TextTurnService


class Clock:
    """One shared finite clock for conversation and state evidence."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


class CountingMockProvider(MockProvider):
    """Record the exact utterance that crosses the provider boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.utterances: list[str] = []
        self.decision_types: list[str] = []

    def complete(self, request: AgentRequest, *args, **kwargs):
        self.calls += 1
        self.utterances.append(request.utterance)
        result = super().complete(request, *args, **kwargs)
        self.decision_types.append(result.decision.type)
        return result


class ExactTargetResolver:
    """Resolve only the registered living-room fixture."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, location: str) -> BoundNamedTarget:
        self.calls += 1
        if location != '거실':
            raise ValueError('target unavailable')
        return BoundNamedTarget(
            room_name='거실',
            room_category='living_room',
            binding_digest='a' * 64,
        )


@dataclass
class Runtime:
    """Resources owned by one service process in a restartable fixture."""

    database: str
    service: TextTurnService
    provider: CountingMockProvider
    resolver: ExactTargetResolver
    conversations: SQLiteConversationStore
    memory: SQLiteMemoryStore
    actions: SQLiteActionRepository

    def close(self) -> None:
        self.actions.close()
        self.conversations.close()
        self.memory.close()


def _runtime(
    database: str,
    *,
    provider: CountingMockProvider | None = None,
    resolver: ExactTargetResolver | None = None,
    clock: Clock | None = None,
) -> Runtime:
    provider = provider or CountingMockProvider()
    resolver = resolver or ExactTargetResolver()
    clock = clock or Clock()
    memory = SQLiteMemoryStore(database)
    conversations = SQLiteConversationStore(database, clock=clock)
    actions = SQLiteActionRepository(database)
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
    return Runtime(
        database=database,
        service=TextTurnService(
            orchestrator,
            resolver,
            clock=clock,
            create_robot_actions=True,
        ),
        provider=provider,
        resolver=resolver,
        conversations=conversations,
        memory=memory,
        actions=actions,
    )


def _request(
    request_id: str,
    turn_id: str,
    text: str,
    *,
    conversation_id: str = 'conversation-1',
) -> dict:
    return {
        'request_id': request_id,
        'conversation_id': conversation_id,
        'turn_id': turn_id,
        'text': text,
    }


def _row_count(database: str, table: str) -> int:
    if table not in {
        'confirmation_intents',
        'robot_actions',
        'text_turn_request_claims',
    }:
        raise ValueError('unsupported fixture table')
    with sqlite3.connect(database) as connection:
        return int(
            connection.execute(
                f'SELECT COUNT(*) FROM {table}'
            ).fetchone()[0]
        )


def _assert_no_effects(runtime: Runtime, result: dict) -> None:
    assert result['execution']['execution_authorized'] is False
    assert result['execution']['physical_authorized'] is False
    assert result['execution']['nav2_start_count'] == 0
    assert result['execution']['nav2_cancel_count'] == 0
    assert _row_count(runtime.database, 'robot_actions') == 0


def _start_clarification(runtime: Runtime) -> dict:
    result = runtime.service.handle(
        user_id='user-1',
        value=_request('question-1', 'turn-1', '여기로 가줘'),
    )
    assert result['status'] == 'completed'
    assert result['decision']['type'] == 'clarification'
    assert result['safety']['allowed'] is True
    assert result['safety']['code'] == 'not_an_action'
    assert runtime.conversations.pending_confirmation(
        'user-1', 'conversation-1'
    ) is None
    assert _row_count(runtime.database, 'confirmation_intents') == 0
    _assert_no_effects(runtime, result)
    return result


def test_clear_destination_keeps_existing_confirmation_flow(tmp_path) -> None:
    runtime = _runtime(str(tmp_path / 'clear.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        result = runtime.service.handle(
            user_id='user-1',
            value=_request('request-1', 'turn-1', '거실로 가줘'),
        )

        assert result['status'] == 'awaiting_confirmation'
        assert result['proposal']['tool_name'] == 'navigate'
        assert result['proposal']['arguments'] == {'location': '거실'}
        assert runtime.provider.calls == 1
        assert runtime.provider.utterances == ['거실로 가줘']
        assert runtime.conversations.pending_confirmation(
            'user-1', 'conversation-1'
        ) is not None
        assert _row_count(runtime.database, 'confirmation_intents') == 1
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()


def test_deictic_request_is_clarification_without_effects(tmp_path) -> None:
    runtime = _runtime(str(tmp_path / 'clarification.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')

        result = _start_clarification(runtime)

        assert runtime.provider.calls == 0
        assert runtime.provider.utterances == []
        assert runtime.resolver.calls == 0
        turns = runtime.conversations.list_turns(
            'user-1', 'conversation-1'
        )
        assert [turn.user_content for turn in turns] == ['여기로 가줘']
        assert turns[0].response['public']['decision']['type'] == (
            'clarification'
        )
        assert result['provider']['provider'] == 'malbut-server-policy'
        assert result['provider']['model'] == (
            'navigation-clarification-v1'
        )
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    'would_return',
    (
        AgentDecision(type='message', message='그냥 답변'),
        AgentDecision(type='refusal', message='요청을 거절합니다.'),
        AgentDecision(
            type='tool_call',
            message='거실로 이동할까요?',
            tool_name='navigate',
            arguments={'location': '거실'},
        ),
    ),
    ids=['message', 'refusal', 'tool-call'],
)
def test_initial_deictic_request_never_calls_adversarial_provider(
    tmp_path,
    would_return: AgentDecision,
) -> None:
    provider = CountingMockProvider()

    def adversarial_complete(request, *args, **kwargs):
        del args, kwargs
        provider.calls += 1
        provider.utterances.append(request.utterance)
        return ProviderResult(
            decision=would_return,
            provider='adversarial-fixture',
            model='fixture',
            latency_ms=0.0,
        )

    provider.complete = adversarial_complete
    runtime = _runtime(
        str(tmp_path / 'adversarial-initial.sqlite3'),
        provider=provider,
    )
    try:
        runtime.conversations.create('user-1', 'conversation-1')

        result = _start_clarification(runtime)

        assert provider.calls == 0
        assert provider.utterances == []
        assert result['provider']['provider'] == 'malbut-server-policy'
        assert result['provider']['model'] == (
            'navigation-clarification-v1'
        )
        assert _row_count(runtime.database, 'confirmation_intents') == 0
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()


def test_raw_answer_is_canonicalized_once_and_creates_confirmation(
    tmp_path,
) -> None:
    runtime = _runtime(str(tmp_path / 'answer.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        _start_clarification(runtime)

        result = runtime.service.handle(
            user_id='user-1',
            value=_request('answer-1', 'turn-2', '거실'),
        )

        assert result['status'] == 'awaiting_confirmation'
        assert result['proposal']['arguments'] == {'location': '거실'}
        assert runtime.provider.calls == 1
        assert runtime.provider.utterances == ['거실로 이동해줘']
        assert runtime.conversations.pending_confirmation(
            'user-1', 'conversation-1'
        ) is not None
        assert _row_count(runtime.database, 'confirmation_intents') == 1
        assert [
            turn.user_content
            for turn in runtime.conversations.list_turns(
                'user-1', 'conversation-1'
            )
        ] == ['여기로 가줘', '거실']
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()


def test_exact_answer_replay_has_no_additional_model_call(tmp_path) -> None:
    runtime = _runtime(str(tmp_path / 'replay.sqlite3'))
    answer = _request('answer-1', 'turn-2', '거실')
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        _start_clarification(runtime)
        first = runtime.service.handle(user_id='user-1', value=answer)
        replay = runtime.service.handle(user_id='user-1', value=answer)

        assert first['status'] == replay['status'] == (
            'awaiting_confirmation'
        )
        assert first['proposal'] == replay['proposal']
        assert runtime.provider.calls == 1
        assert _row_count(runtime.database, 'confirmation_intents') == 1
        assert len(runtime.conversations.list_turns(
            'user-1', 'conversation-1'
        )) == 2
        _assert_no_effects(runtime, replay)
    finally:
        runtime.close()


@pytest.mark.parametrize('intervening_text', ('네', '아니요', '취소'))
def test_intervening_confirmation_word_consumes_clarification_hop(
    tmp_path,
    intervening_text: str,
) -> None:
    runtime = _runtime(str(tmp_path / 'intervening-input.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        _start_clarification(runtime)

        intervening = runtime.service.handle(
            user_id='user-1',
            value=_request(
                'intervening-input',
                'confirmation-turn',
                intervening_text,
            ),
        )
        answer = runtime.service.handle(
            user_id='user-1',
            value=_request('late-answer', 'turn-2', '거실'),
        )

        assert intervening['status'] == 'no_pending_confirmation'
        assert answer['status'] == 'completed'
        assert answer['decision']['type'] == 'clarification'
        assert runtime.provider.calls == 1
        assert runtime.provider.utterances == ['거실']
        assert runtime.conversations.pending_confirmation(
            'user-1', 'conversation-1'
        ) is None
        assert _row_count(runtime.database, 'confirmation_intents') == 0
        _assert_no_effects(runtime, answer)
    finally:
        runtime.close()


def test_intervening_input_barrier_survives_process_restart(tmp_path) -> None:
    database = str(tmp_path / 'intervening-restart.sqlite3')
    clock = Clock()
    provider = CountingMockProvider()
    resolver = ExactTargetResolver()
    first = _runtime(
        database,
        provider=provider,
        resolver=resolver,
        clock=clock,
    )
    first.conversations.create('user-1', 'conversation-1')
    _start_clarification(first)
    no_pending = first.service.handle(
        user_id='user-1',
        value=_request('intervening-input', 'confirmation-turn', '네'),
    )
    assert no_pending['status'] == 'no_pending_confirmation'
    first.close()

    second = _runtime(
        database,
        provider=provider,
        resolver=resolver,
        clock=clock,
    )
    try:
        answer = second.service.handle(
            user_id='user-1',
            value=_request('late-answer', 'turn-2', '거실'),
        )

        assert answer['status'] == 'completed'
        assert answer['decision']['type'] == 'clarification'
        assert provider.calls == 1
        assert provider.utterances == ['거실']
        assert _row_count(database, 'confirmation_intents') == 0
        _assert_no_effects(second, answer)
    finally:
        second.close()


def test_confirmation_word_cannot_cross_pending_clarification_answer(
    tmp_path,
) -> None:
    database = str(tmp_path / 'pending-answer-race.sqlite3')
    answer_runtime = _runtime(database)
    response_runtime = None
    try:
        answer_runtime.conversations.create('user-1', 'conversation-1')
        _start_clarification(answer_runtime)
        response_runtime = _runtime(database)
        entered_provider = threading.Event()
        release_provider = threading.Event()
        original_complete = answer_runtime.provider.complete

        def blocked_complete(request, *args, **kwargs):
            if request.utterance == '거실로 이동해줘':
                entered_provider.set()
                if not release_provider.wait(timeout=5.0):
                    raise RuntimeError('test did not release provider')
            return original_complete(request, *args, **kwargs)

        answer_runtime.provider.complete = blocked_complete
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                answer_runtime.service.handle,
                user_id='user-1',
                value=_request('answer-1', 'turn-2', '거실'),
            )
            assert entered_provider.wait(timeout=5.0)
            with pytest.raises(
                ConversationConflictError,
                match='another turn is already in progress',
            ):
                response_runtime.service.handle(
                    user_id='user-1',
                    value=_request('approval-1', 'turn-3', '네'),
                )
            release_provider.set()
            answer = future.result(timeout=5.0)

        assert answer['status'] == 'awaiting_confirmation'
        assert answer_runtime.provider.calls == 1
        assert response_runtime.provider.calls == 0
        assert _row_count(database, 'confirmation_intents') == 1
        assert _row_count(database, 'text_turn_request_claims') == 0
        _assert_no_effects(answer_runtime, answer)
    finally:
        if response_runtime is not None:
            response_runtime.close()
        answer_runtime.close()


def test_answer_can_be_resolved_after_process_restart(tmp_path) -> None:
    database = str(tmp_path / 'restart.sqlite3')
    clock = Clock()
    provider = CountingMockProvider()
    resolver = ExactTargetResolver()
    first = _runtime(
        database,
        provider=provider,
        resolver=resolver,
        clock=clock,
    )
    first.conversations.create('user-1', 'conversation-1')
    _start_clarification(first)
    first.close()

    second = _runtime(
        database,
        provider=provider,
        resolver=resolver,
        clock=clock,
    )
    try:
        result = second.service.handle(
            user_id='user-1',
            value=_request('answer-after-restart', 'turn-2', '거실'),
        )

        assert result['status'] == 'awaiting_confirmation'
        assert provider.calls == 1
        assert provider.utterances[-1] == '거실로 이동해줘'
        assert _row_count(database, 'confirmation_intents') == 1
        assert [
            turn.user_content
            for turn in second.conversations.list_turns(
                'user-1', 'conversation-1'
            )
        ] == ['여기로 가줘', '거실']
        _assert_no_effects(second, result)
    finally:
        second.close()


def test_concurrent_answers_have_one_winner_and_one_confirmation(
    tmp_path,
) -> None:
    database = str(tmp_path / 'concurrent.sqlite3')
    winner = _runtime(database)
    contender = None
    try:
        winner.conversations.create('user-1', 'conversation-1')
        _start_clarification(winner)
        contender = _runtime(database)

        entered_provider = threading.Event()
        release_provider = threading.Event()
        original_complete = winner.provider.complete

        def blocked_complete(request, *args, **kwargs):
            if request.utterance == '거실로 이동해줘':
                entered_provider.set()
                if not release_provider.wait(timeout=5.0):
                    raise RuntimeError('test did not release provider')
            return original_complete(request, *args, **kwargs)

        winner.provider.complete = blocked_complete
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                winner.service.handle,
                user_id='user-1',
                value=_request('winner-answer', 'turn-2', '거실'),
            )
            assert entered_provider.wait(timeout=5.0)
            with pytest.raises(
                ConversationConflictError,
                match='another turn is already in progress',
            ):
                contender.service.handle(
                    user_id='user-1',
                    value=_request(
                        'contender-answer',
                        'turn-3',
                        '거실',
                    ),
                )
            release_provider.set()
            result = future.result(timeout=5.0)

        assert result['status'] == 'awaiting_confirmation'
        assert winner.provider.calls == 1
        assert contender.provider.calls == 0
        assert _row_count(database, 'confirmation_intents') == 1
        assert len(winner.conversations.list_turns(
            'user-1', 'conversation-1'
        )) == 2
        _assert_no_effects(winner, result)
    finally:
        if contender is not None:
            contender.close()
        winner.close()


def test_wrong_conversation_does_not_resolve_clarification(tmp_path) -> None:
    runtime = _runtime(str(tmp_path / 'wrong-conversation.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        runtime.conversations.create('user-1', 'conversation-2')
        _start_clarification(runtime)

        result = runtime.service.handle(
            user_id='user-1',
            value=_request(
                'wrong-answer',
                'turn-1',
                '거실',
                conversation_id='conversation-2',
            ),
        )

        assert result['status'] == 'completed'
        assert result['decision']['type'] == 'clarification'
        assert runtime.provider.calls == 1
        assert runtime.provider.utterances[-1] == '거실'
        assert runtime.conversations.pending_confirmation(
            'user-1', 'conversation-1'
        ) is None
        assert runtime.conversations.pending_confirmation(
            'user-1', 'conversation-2'
        ) is None
        assert _row_count(runtime.database, 'confirmation_intents') == 0
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    'answer',
    (
        '서재',
        '거실, 주방',
        '거실 말고 주방',
        '거실로 가지 마',
    ),
    ids=['unknown', 'multiple', 'alternative', 'negated'],
)
def test_invalid_answer_never_creates_confirmation_or_action(
    tmp_path,
    answer: str,
) -> None:
    runtime = _runtime(str(tmp_path / 'invalid-answer.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        _start_clarification(runtime)

        result = runtime.service.handle(
            user_id='user-1',
            value=_request('answer-1', 'turn-2', answer),
        )

        assert result['status'] == 'completed'
        assert runtime.provider.calls == 1
        assert runtime.provider.utterances[-1] == answer
        assert runtime.conversations.pending_confirmation(
            'user-1', 'conversation-1'
        ) is None
        assert _row_count(runtime.database, 'confirmation_intents') == 0
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()


def test_invalid_answer_cannot_authorize_a_hallucinated_tool_call(
    tmp_path,
) -> None:
    runtime = _runtime(str(tmp_path / 'invalid-tool-call.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        _start_clarification(runtime)

        def hallucinate_navigation(request, *args, **kwargs):
            del args, kwargs
            runtime.provider.calls += 1
            runtime.provider.utterances.append(request.utterance)
            runtime.provider.decision_types.append('tool_call')
            decision = AgentDecision(
                type='tool_call',
                message='거실로 이동할까요?',
                tool_name='navigate',
                arguments={'location': '거실'},
            )
            return ProviderResult(
                decision=decision,
                provider='adversarial-fixture',
                model='fixture',
                latency_ms=0.0,
            )

        runtime.provider.complete = hallucinate_navigation
        result = runtime.service.handle(
            user_id='user-1',
            value=_request(
                'answer-1',
                'turn-2',
                '거실로 가지 마',
            ),
        )

        assert result['status'] == 'completed'
        assert result['decision']['type'] == 'refusal'
        assert result['safety']['allowed'] is False
        assert result['safety']['code'] == (
            'clarification_answer_invalid'
        )
        assert runtime.provider.calls == 1
        assert runtime.conversations.pending_confirmation(
            'user-1', 'conversation-1'
        ) is None
        assert _row_count(runtime.database, 'confirmation_intents') == 0
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()


def test_valid_answer_cannot_be_changed_to_another_model_target(
    tmp_path,
) -> None:
    runtime = _runtime(str(tmp_path / 'target-mismatch.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        _start_clarification(runtime)

        def substitute_target(request, *args, **kwargs):
            del args, kwargs
            runtime.provider.calls += 1
            runtime.provider.utterances.append(request.utterance)
            runtime.provider.decision_types.append('tool_call')
            decision = AgentDecision(
                type='tool_call',
                message='주방으로 이동할까요?',
                tool_name='navigate',
                arguments={'location': '주방'},
            )
            return ProviderResult(
                decision=decision,
                provider='adversarial-fixture',
                model='fixture',
                latency_ms=0.0,
            )

        runtime.provider.complete = substitute_target
        result = runtime.service.handle(
            user_id='user-1',
            value=_request('answer-1', 'turn-2', '거실'),
        )

        assert result['status'] == 'completed'
        assert result['decision']['type'] == 'refusal'
        assert result['safety']['allowed'] is False
        assert result['safety']['code'] == 'current_turn_intent_missing'
        assert runtime.provider.calls == 1
        assert runtime.provider.utterances[-1] == '거실로 이동해줘'
        assert runtime.conversations.pending_confirmation(
            'user-1', 'conversation-1'
        ) is None
        assert _row_count(runtime.database, 'confirmation_intents') == 0
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()


def test_second_clarification_is_bounded_refusal(tmp_path) -> None:
    runtime = _runtime(str(tmp_path / 'bounded.sqlite3'))
    try:
        runtime.conversations.create('user-1', 'conversation-1')
        _start_clarification(runtime)

        result = runtime.service.handle(
            user_id='user-1',
            value=_request('answer-1', 'turn-2', '서재'),
        )

        assert result['status'] == 'completed'
        assert runtime.provider.decision_types[-1] == 'clarification'
        assert result['decision']['type'] == 'refusal'
        assert result['safety']['allowed'] is False
        assert result['safety']['code'] == 'clarification_limit_reached'
        assert runtime.provider.calls == 1
        assert len(runtime.conversations.list_turns(
            'user-1', 'conversation-1'
        )) == 2
        assert _row_count(runtime.database, 'confirmation_intents') == 0
        _assert_no_effects(runtime, result)
    finally:
        runtime.close()
