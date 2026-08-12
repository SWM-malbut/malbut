"""Tests for provider normalization and deterministic safety gating."""

import math
import threading
import time
from typing import List, Optional

import pytest

from malbut_agent_server.conversation import (
    ConversationChangedError,
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.gateway import (
    CapabilityRegistry,
    ToolCapability,
)
from malbut_agent_server.memory import MemoryRecord, SQLiteMemoryStore
from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    MemoryChangedError,
    OrchestrationResult,
)
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    ValidationError,
)
from malbut_agent_server.tools import ToolSpec


def _request(
    utterance: str,
    state: dict,
    tools: List[str],
    *,
    request_id: str = 'test-request',
    turn_id: str = 'test-turn',
    conversation_id: str = 'test-conversation',
    user_id: str = 'test-user',
) -> AgentRequest:
    robot_state = {
        'battery_percent': 80,
        'navigation_available': True,
        'localization_ok': True,
        'camera_available': True,
    }
    robot_state.update(state)
    return AgentRequest.from_dict(
        {
            'request_id': request_id,
            'user_id': user_id,
            'conversation_id': conversation_id,
            'turn_id': turn_id,
            'utterance': utterance,
            'robot_state': robot_state,
            'available_tools': tools,
        }
    )


def _runtime(
    provider: AgentProvider,
    store: SQLiteMemoryStore,
    safety_policy: SafetyPolicy,
    trusted_robot_state: bool = False,
    capability_registry: CapabilityRegistry | None = None,
) -> tuple[AgentOrchestrator, SQLiteConversationStore]:
    conversation_store = SQLiteConversationStore(':memory:')
    conversation_store.create(
        'test-user',
        'test-conversation',
    )
    return (
        AgentOrchestrator(
            provider=provider,
            memory_store=store,
            conversation_store=conversation_store,
            safety_policy=safety_policy,
            trusted_robot_state=trusted_robot_state,
            capability_registry=capability_registry,
        ),
        conversation_store,
    )


def test_navigation_is_blocked_during_emergency_stop() -> None:
    """A valid model tool call cannot override current local state."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
            True,
        )
        result = orchestrator.handle(
            _request(
                '거실로 가줘',
                {'emergency_stop': True},
                ['navigate'],
            )
        )
        assert result.raw_decision.type == 'tool_call'
        assert result.decision.type == 'refusal'
        assert result.safety.code == 'emergency_stop'
    finally:
        conversation_store.close()
        store.close()


def test_privacy_mode_blocks_camera_capture() -> None:
    """Camera intent is preserved for audit but not authorized."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
            True,
        )
        result = orchestrator.handle(
            _request(
                '사진 찍어줘',
                {'privacy_mode': True},
                ['capture_photo'],
            )
        )
        assert result.raw_decision.tool_name == 'capture_photo'
        assert result.decision.type == 'refusal'
        assert result.safety.code == 'privacy_mode'
    finally:
        conversation_store.close()
        store.close()


class UnknownToolProvider(AgentProvider):
    """Fixture provider that simulates a hallucinated action."""

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        del (
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary,
        )
        return ProviderResult(
            decision=AgentDecision(
                type='tool_call',
                message='문을 열게.',
                tool_name='unlock_door',
                arguments={},
            ),
            provider='fixture',
            model='fixture',
            latency_ms=0,
        )


class MutatingProvider(AgentProvider):
    """Fixture attempting to corrupt the safety input after inference."""

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        del (
            memories,
            conversation_turns,
            tools,
            conversation_summary,
        )
        object.__setattr__(
            request,
            'available_tools',
            ('navigate',),
        )
        object.__setattr__(
            request.robot_state,
            'forbidden_zones',
            (),
        )
        return ProviderResult(
            decision=AgentDecision(
                type='tool_call',
                message='이동할게.',
                tool_name='navigate',
                arguments={'location': '거실'},
            ),
            provider='fixture',
            model='fixture',
            latency_ms=0,
        )


class RecordingProvider(AgentProvider):
    """Fixture that records server-owned history passed to the model."""

    def __init__(self, delay_seconds: float = 0) -> None:
        """Create a thread-safe provider recorder."""
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.histories: List[List[ConversationTurn]] = []
        self._lock = threading.Lock()

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        del memories, tools, conversation_summary
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        with self._lock:
            self.calls += 1
            self.histories.append(list(conversation_turns))
        return ProviderResult(
            decision=AgentDecision(
                type='message',
                message=f'확인했어: {request.utterance}',
            ),
            provider='recording-fixture',
            model='fixture',
            latency_ms=0,
        )


def test_unknown_provider_tool_never_reaches_executor() -> None:
    """Hallucinated capabilities are rejected after model inference."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            UnknownToolProvider(),
            store,
            SafetyPolicy(),
            True,
        )
        result = orchestrator.handle(
            _request('문 열어줘', {}, ['navigate'])
        )
        assert result.decision.type == 'refusal'
        assert result.safety.code == 'unknown_tool'
    finally:
        conversation_store.close()
        store.close()


def test_provider_cannot_mutate_trusted_safety_snapshot() -> None:
    """Provider code never receives the object later used for safety."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MutatingProvider(),
            store,
            SafetyPolicy(),
            True,
        )
        result = orchestrator.handle(
            _request(
                '거실로 가줘',
                {'forbidden_zones': ['거실']},
                [],
            )
        )
        assert result.decision.type == 'refusal'
        assert result.safety.code == 'tool_unavailable'
    finally:
        conversation_store.close()
        store.close()


def test_untrusted_http_style_state_never_authorizes_action() -> None:
    """Client-asserted safety state is advisory until ROS verifies it."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
        )
        result = orchestrator.handle(
            _request('거실로 가줘', {}, ['navigate'])
        )
        public = result.to_dict()
        assert result.raw_decision.tool_name == 'navigate'
        assert result.decision.type == 'refusal'
        assert result.safety.code == 'untrusted_robot_state'
        assert public['execution']['authorized'] is False
        assert 'raw_decision' not in public
    finally:
        conversation_store.close()
        store.close()


def test_registry_limits_model_capability_claims() -> None:
    """An HTTP subset cannot add Tools outside server-owned policy."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
            capability_registry=CapabilityRegistry(
                [ToolCapability('get_robot_status')]
            ),
        )
        result = orchestrator.handle(
            _request(
                '무슨 기능을 할 수 있어?',
                {},
                ['navigate', 'unlock_door', 'get_robot_status'],
            )
        )
        assert result.decision.type == 'message'
        assert '상태 확인' in result.decision.message
        assert '이동' not in result.decision.message
        assert '사진' not in result.decision.message
        assert '알림' not in result.decision.message
    finally:
        conversation_store.close()
        store.close()


def test_policy_approval_is_not_gateway_execution_authority() -> None:
    """A valid proposal stays non-executable until SWM25-74."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
            True,
        )
        result = orchestrator.handle(
            _request('거실로 가줘', {}, ['navigate'])
        ).to_dict()
        assert result['decision']['type'] == 'tool_call'
        assert result['safety']['allowed'] is True
        assert result['execution']['proposal_authorized'] is True
        assert result['execution']['authorized'] is False
        assert result['execution']['consume_once'] is False
        assert result['execution']['tool_call_id'] is None
    finally:
        conversation_store.close()
        store.close()


def test_request_id_is_idempotent_and_cannot_change_input() -> None:
    """Retries reuse one decision ID while conflicting reuse is rejected."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
            True,
        )
        first_request = _request(
            '거실로 가줘',
            {},
            ['navigate'],
        )
        first = orchestrator.handle(first_request)
        second = orchestrator.handle(first_request)
        assert first.decision_id == second.decision_id
        persisted = first.to_persisted_dict()
        assert persisted['schema_version'] == 2
        persisted['schema_version'] = 1
        legacy = OrchestrationResult.from_persisted_dict(persisted)
        assert legacy.to_dict()['execution']['authorized'] is False

        conflicting = AgentRequest.from_dict(
            {
                **first_request.to_dict(),
                'utterance': '주방으로 가줘',
            }
        )
        try:
            orchestrator.handle(conflicting)
            raise AssertionError('conflicting request_id was accepted')
        except ValidationError:
            pass

        first.expires_at = 0
        assert first.to_dict()['execution']['authorized'] is False
        expired_retry = orchestrator.handle(first_request)
        assert expired_retry.decision_id == first.decision_id
        assert expired_retry.expires_at > 0
    finally:
        conversation_store.close()
        store.close()


def test_mock_does_not_turn_negation_into_navigation() -> None:
    """The offline demo provider must understand simple move negation."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
            True,
        )
        result = orchestrator.handle(
            _request(
                '거실로 가지 마',
                {},
                ['navigate'],
            )
        )
        assert result.decision.type == 'message'
        assert result.decision.tool_name is None
    finally:
        conversation_store.close()
        store.close()


def test_empty_location_allowlist_fails_closed() -> None:
    """An explicitly empty allowlist must not restore defaults."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(allowed_locations=[]),
            True,
        )
        result = orchestrator.handle(
            _request('거실로 가줘', {}, ['navigate'])
        )
        assert result.decision.type == 'refusal'
        assert result.safety.code == 'location_not_allowed'
    finally:
        conversation_store.close()
        store.close()


def test_invalid_safety_thresholds_are_rejected() -> None:
    """Non-finite policy configuration must not disable comparisons."""
    with pytest.raises(ValueError):
        SafetyPolicy(minimum_navigation_battery=math.nan)
    with pytest.raises(ValueError):
        SafetyPolicy(maximum_action_ttl_ms=0)
    with pytest.raises(ValueError):
        SafetyPolicy(allowed_locations=[''])


def test_unverified_image_notification_is_blocked() -> None:
    """Image IDs require a future user-scoped media registry."""
    request = _request(
        '사진을 가족에게 보내줘',
        {'privacy_mode': False},
        ['send_notification'],
    )
    decision = AgentDecision(
        type='tool_call',
        message='사진을 보낼게.',
        tool_name='send_notification',
        arguments={
            'message': '사진을 확인해줘.',
            'image_id': 'unverified-image',
        },
    )
    result = SafetyPolicy().evaluate(
        request,
        decision,
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == 'image_attachment_unverified'


def test_memory_change_during_inference_discards_result() -> None:
    """A deleted memory cannot become a newly cached model answer."""
    store = SQLiteMemoryStore(':memory:')
    record = store.add('test-user', '반려견 이름은 초코')

    class DeletingProvider(AgentProvider):
        def complete(
            self,
            request: AgentRequest,
            memories: List[MemoryRecord],
            conversation_turns: List[ConversationTurn],
            tools: List[ToolSpec],
            conversation_summary: Optional[
                ConversationSummary
            ] = None,
        ) -> ProviderResult:
            del (
                request,
                memories,
                conversation_turns,
                tools,
                conversation_summary,
            )
            store.delete('test-user', record.id)
            return ProviderResult(
                decision=AgentDecision(
                    type='message',
                    message='이름은 초코야.',
                ),
                provider='fixture',
                model='fixture',
                latency_ms=0,
            )

    try:
        orchestrator, conversation_store = _runtime(
            DeletingProvider(),
            store,
            SafetyPolicy(),
        )
        with pytest.raises(MemoryChangedError):
            orchestrator.handle(
                _request(
                    '강아지 이름이 뭐였지?',
                    {},
                    [],
                )
            )
    finally:
        conversation_store.close()
        store.close()


def test_other_user_memory_change_does_not_discard_result() -> None:
    """Another owner's mutation must not invalidate this user's context."""
    store = SQLiteMemoryStore(':memory:')
    store.add('test-user', '반려견 이름은 초코')

    class OtherUserWritingProvider(AgentProvider):
        def complete(
            self,
            request: AgentRequest,
            memories: List[MemoryRecord],
            conversation_turns: List[ConversationTurn],
            tools: List[ToolSpec],
            conversation_summary: Optional[
                ConversationSummary
            ] = None,
        ) -> ProviderResult:
            del (
                request,
                memories,
                conversation_turns,
                tools,
                conversation_summary,
            )
            store.add('other-user', '다른 사용자의 독립 기억')
            return ProviderResult(
                decision=AgentDecision(
                    type='message',
                    message='이름은 초코야.',
                ),
                provider='fixture',
                model='fixture',
                latency_ms=0,
            )

    try:
        orchestrator, conversation_store = _runtime(
            OtherUserWritingProvider(),
            store,
            SafetyPolicy(),
        )
        result = orchestrator.handle(
            _request('강아지 이름이 뭐였지?', {}, [])
        )
        assert result.decision.message == '이름은 초코야.'
    finally:
        conversation_store.close()
        store.close()


def test_memory_expiring_during_inference_discards_result() -> None:
    """Time expiry must invalidate a response even without a DB mutation."""
    current_time = [100.0]
    store = SQLiteMemoryStore(
        ':memory:',
        clock=lambda: current_time[0],
    )
    store.add(
        'test-user',
        '반려견 이름은 초코',
        expires_at=101.0,
        created_at=99.0,
    )

    class ExpiringProvider(AgentProvider):
        def complete(
            self,
            request: AgentRequest,
            memories: List[MemoryRecord],
            conversation_turns: List[ConversationTurn],
            tools: List[ToolSpec],
            conversation_summary: Optional[
                ConversationSummary
            ] = None,
        ) -> ProviderResult:
            del (
                request,
                conversation_turns,
                tools,
                conversation_summary,
            )
            assert len(memories) == 1
            current_time[0] = 101.0
            return ProviderResult(
                decision=AgentDecision(
                    type='message',
                    message='이름은 초코야.',
                ),
                provider='fixture',
                model='fixture',
                latency_ms=0,
            )

    try:
        orchestrator, conversation_store = _runtime(
            ExpiringProvider(),
            store,
            SafetyPolicy(),
        )
        with pytest.raises(MemoryChangedError):
            orchestrator.handle(
                _request('강아지 이름이 뭐였지?', {}, [])
            )
    finally:
        conversation_store.close()
        store.close()


def test_provider_cannot_mutate_memory_snapshot_to_bypass_fence() -> None:
    """Post-provider checks use an immutable server-owned memory snapshot."""
    current_time = [100.0]
    store = SQLiteMemoryStore(
        ':memory:',
        clock=lambda: current_time[0],
    )
    record = store.add(
        'test-user',
        '반려견 이름은 초코',
        expires_at=101.0,
        created_at=99.0,
        metadata={'owner': 'server'},
    )

    class MutatingMemoryListProvider(AgentProvider):
        def complete(
            self,
            request: AgentRequest,
            memories: List[MemoryRecord],
            conversation_turns: List[ConversationTurn],
            tools: List[ToolSpec],
            conversation_summary: Optional[
                ConversationSummary
            ] = None,
        ) -> ProviderResult:
            del request, conversation_turns, tools, conversation_summary
            assert memories[0].id == record.id
            memories[0].metadata['owner'] = 'provider'
            memories.clear()
            current_time[0] = 101.0
            return ProviderResult(
                decision=AgentDecision(
                    type='message',
                    message='이름은 초코야.',
                ),
                provider='fixture',
                model='fixture',
                latency_ms=0,
            )

    try:
        orchestrator, conversation_store = _runtime(
            MutatingMemoryListProvider(),
            store,
            SafetyPolicy(),
        )
        with pytest.raises(MemoryChangedError):
            orchestrator.handle(
                _request('강아지 이름이 뭐였지?', {}, [])
            )
        assert record.metadata == {'owner': 'server'}
    finally:
        conversation_store.close()
        store.close()


def test_retrieved_memory_expiry_bounds_decision_ttl() -> None:
    """A decision must not outlive the memory used to produce it."""
    store = SQLiteMemoryStore(':memory:')
    expires_at = time.time() + 10
    record = store.add(
        'test-user',
        '강아지 이름은 초코',
        expires_at=expires_at,
    )
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
        )
        result = orchestrator.handle(
            _request(
                '강아지 이름이 뭐였지?',
                {},
                [],
            )
        )
        assert record.id in result.memory_ids
        assert result.issued_at < result.expires_at <= expires_at
    finally:
        conversation_store.close()
        store.close()


def test_provider_receives_latest_ten_turns_in_order() -> None:
    """The model receives exactly the latest ten completed exchanges."""
    store = SQLiteMemoryStore(':memory:')
    provider = RecordingProvider()
    try:
        orchestrator, conversation_store = _runtime(
            provider,
            store,
            SafetyPolicy(),
        )
        for number in range(1, 13):
            orchestrator.handle(
                _request(
                    f'발화 {number}',
                    {},
                    [],
                    request_id=f'request-{number}',
                    turn_id=f'turn-{number}',
                )
            )

        assert [len(history) for history in provider.histories] == [
            0,
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            10,
        ]
        assert [
            turn.user_content
            for turn in provider.histories[-1]
        ] == [f'발화 {number}' for number in range(2, 12)]
    finally:
        conversation_store.close()
        store.close()


def test_mock_resolves_person_and_result_follow_ups() -> None:
    """Korean person/result pronouns use only the current session."""
    store = SQLiteMemoryStore(':memory:')
    try:
        orchestrator, conversation_store = _runtime(
            MockProvider(),
            store,
            SafetyPolicy(),
        )
        orchestrator.handle(
            _request(
                '민수는 내 친구야',
                {},
                [],
                request_id='follow-up-1',
                turn_id='turn-1',
            )
        )
        person = orchestrator.handle(
            _request(
                '그 사람은 누구야?',
                {},
                [],
                request_id='follow-up-2',
                turn_id='turn-2',
            )
        )
        assert '민수는 내 친구야' in person.decision.message

        result = orchestrator.handle(
            _request(
                '그거 다시 말해줘',
                {},
                [],
                request_id='follow-up-3',
                turn_id='turn-3',
            )
        )
        assert person.decision.message in result.decision.message
    finally:
        conversation_store.close()
        store.close()


def test_concurrent_requests_are_ordered_and_exact_retry_runs_once() -> None:
    """One session serializes turns and deduplicates simultaneous retry."""
    store = SQLiteMemoryStore(':memory:')
    provider = RecordingProvider(delay_seconds=0.03)
    results = []
    errors = []
    result_lock = threading.Lock()
    barrier = threading.Barrier(3)
    try:
        orchestrator, conversation_store = _runtime(
            provider,
            store,
            SafetyPolicy(),
        )
        duplicate_request = _request(
            '동시에 한 번만 처리해줘',
            {},
            [],
        )

        def invoke() -> None:
            barrier.wait()
            try:
                response = orchestrator.handle(duplicate_request)
                with result_lock:
                    results.append(response)
            except Exception as error:  # noqa: B902
                with result_lock:
                    errors.append(error)

        threads = [
            threading.Thread(target=invoke)
            for _index in range(2)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        assert errors == []
        assert len(results) == 2
        assert provider.calls == 1
        assert (
            results[0].decision_id
            == results[1].decision_id
        )

        second_results = []
        second_barrier = threading.Barrier(3)

        def invoke_unique(number: int) -> None:
            second_barrier.wait()
            response = orchestrator.handle(
                _request(
                    f'고유 발화 {number}',
                    {},
                    [],
                    request_id=f'unique-request-{number}',
                    turn_id=f'unique-turn-{number}',
                )
            )
            with result_lock:
                second_results.append(response)

        unique_threads = [
            threading.Thread(
                target=invoke_unique,
                args=(number,),
            )
            for number in (1, 2)
        ]
        for thread in unique_threads:
            thread.start()
        second_barrier.wait()
        for thread in unique_threads:
            thread.join(timeout=5)

        assert len(second_results) == 2
        assert sorted(
            result.conversation_ordinal
            for result in second_results
        ) == [2, 3]
        turns = conversation_store.list_turns(
            'test-user',
            'test-conversation',
        )
        assert [turn.ordinal for turn in turns] == [1, 2, 3]
        assert provider.calls == 3
    finally:
        conversation_store.close()
        store.close()


def test_durable_retry_survives_runtime_restart(tmp_path) -> None:
    """A restarted process reuses the stored answer without a model call."""
    database = str(tmp_path / 'agent.sqlite3')
    request = _request('재시작 후에도 한 번만', {}, [])
    first_provider = RecordingProvider()
    first_memory = SQLiteMemoryStore(database)
    first_conversations = SQLiteConversationStore(database)
    try:
        first_conversations.create(
            'test-user',
            'test-conversation',
        )
        first_runtime = AgentOrchestrator(
            provider=first_provider,
            memory_store=first_memory,
            conversation_store=first_conversations,
            safety_policy=SafetyPolicy(),
        )
        first = first_runtime.handle(request)
        assert first_provider.calls == 1
    finally:
        first_conversations.close()
        first_memory.close()

    second_provider = RecordingProvider()
    second_memory = SQLiteMemoryStore(database)
    second_conversations = SQLiteConversationStore(database)
    try:
        second_runtime = AgentOrchestrator(
            provider=second_provider,
            memory_store=second_memory,
            conversation_store=second_conversations,
            safety_policy=SafetyPolicy(),
        )
        retried = second_runtime.handle(request)
        assert second_provider.calls == 0
        assert retried.decision_id == first.decision_id
        assert len(
            second_conversations.list_turns(
                'test-user',
                'test-conversation',
            )
        ) == 1
    finally:
        second_conversations.close()
        second_memory.close()


def test_reset_during_inference_discards_late_answer() -> None:
    """Reset is a generation boundary even while a provider is running."""
    class BlockingProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def complete(
            self,
            request: AgentRequest,
            memories: List[MemoryRecord],
            conversation_turns: List[ConversationTurn],
            tools: List[ToolSpec],
            conversation_summary: Optional[
                ConversationSummary
            ] = None,
        ) -> ProviderResult:
            self.entered.set()
            assert self.release.wait(timeout=5)
            return super().complete(
                request,
                memories,
                conversation_turns,
                tools,
                conversation_summary,
            )

    store = SQLiteMemoryStore(':memory:')
    provider = BlockingProvider()
    errors = []
    try:
        orchestrator, conversation_store = _runtime(
            provider,
            store,
            SafetyPolicy(),
        )

        def invoke() -> None:
            try:
                orchestrator.handle(
                    _request('늦은 답변을 만들 요청', {}, [])
                )
            except Exception as error:  # noqa: B902
                errors.append(error)

        thread = threading.Thread(target=invoke)
        thread.start()
        assert provider.entered.wait(timeout=5)
        conversation_store.reset(
            'test-user',
            'test-conversation',
        )
        provider.release.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ConversationChangedError)
        assert conversation_store.list_turns(
            'test-user',
            'test-conversation',
        ) == []
    finally:
        provider.release.set()
        conversation_store.close()
        store.close()
