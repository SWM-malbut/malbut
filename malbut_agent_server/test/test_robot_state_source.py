"""Contracts for post-model server-owned RobotState evidence."""

from typing import List

import pytest

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.memory import MemoryRecord, SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.robot_state_source import (
    RobotStateEvidence,
    StaticSimulationRobotStateSource,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    ProviderUsage,
    RobotState,
)
from malbut_agent_server.tools import ToolSpec


class NavigateProvider(AgentProvider):
    """Return one proposal while recording when inference happened."""

    def __init__(self, events):
        self.events = events

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: ConversationSummary | None = None,
    ) -> ProviderResult:
        del memories, conversation_turns, tools, conversation_summary
        self.events.append(('provider', request.robot_state))
        return ProviderResult(
            decision=AgentDecision(
                type='tool_call',
                message='거실 이동을 제안합니다.',
                tool_name='navigate',
                arguments={'location': '거실'},
                reason='test',
                confidence=1.0,
            ),
            provider='test',
            model='test',
            latency_ms=0.0,
            usage=ProviderUsage(),
        )


class RecordingSource:
    """Prove that the state source runs strictly after the provider."""

    def __init__(self, events, evidence):
        self.events = events
        self.evidence = evidence

    def read(self):
        self.events.append(('state', self.evidence.state))
        return self.evidence


def _request(request_id='state-request-1'):
    return AgentRequest.from_dict({
        'request_id': request_id,
        'user_id': 'state-user',
        'conversation_id': 'state-conversation',
        'turn_id': f'{request_id}-turn',
        'utterance': '거실로 이동해줘',
        'robot_state': {
            'battery_percent': 1,
            'navigation_available': False,
            'localization_ok': False,
            'emergency_stop': True,
        },
        'available_tools': ['navigate'],
    })


def _runtime(provider, source, *, now=100.0, max_age=2.0):
    memory = SQLiteMemoryStore(':memory:')
    conversation = SQLiteConversationStore(':memory:')
    conversation.create('state-user', 'state-conversation')
    return (
        AgentOrchestrator(
            provider=provider,
            memory_store=memory,
            conversation_store=conversation,
            safety_policy=SafetyPolicy(),
            robot_state_source=source,
            robot_state_max_age_seconds=max_age,
            state_clock=lambda: now,
        ),
        memory,
        conversation,
    )


def test_server_owned_state_is_read_after_provider_and_overrides_client():
    events = []
    evidence = RobotStateEvidence(
        state=RobotState(
            battery_percent=80,
            navigation_available=True,
            localization_ok=True,
            emergency_stop=False,
        ),
        observed_at=100.0,
        evidence_id='fresh-simulation-state',
        trusted=True,
    )
    runtime, memory, conversation = _runtime(
        NavigateProvider(events),
        RecordingSource(events, evidence),
    )
    try:
        result = runtime.handle(_request())
        assert [event[0] for event in events] == ['provider', 'state']
        assert events[0][1].emergency_stop is True
        assert result.safety.allowed is True
        assert result.state_trusted is True
        assert result.state_evidence_id == 'fresh-simulation-state'
        assert result.state_observed_at == 100.0
        assert result.safety_policy_revision == 'malbut-safety-v1'
        public = result.to_dict()
        assert 'fresh-simulation-state' not in str(public)
        assert 'malbut-safety-v1' not in str(public)
        replay = runtime.handle(_request())
        assert replay.state_evidence_id == result.state_evidence_id
        assert replay.state_observed_at == result.state_observed_at
        assert replay.safety_policy_revision == result.safety_policy_revision
        assert replay.to_persisted_dict()['schema_version'] == 3
    finally:
        conversation.close()
        memory.close()


def test_stale_or_failed_server_state_fails_closed():
    for source, request_id in (
        (
            RecordingSource([], RobotStateEvidence(
                state=RobotState(
                    battery_percent=80,
                    navigation_available=True,
                    localization_ok=True,
                ),
                observed_at=90.0,
                evidence_id='stale-state',
                trusted=True,
            )),
            'stale-request',
        ),
        (type('BrokenSource', (), {
            'read': lambda self: (_ for _ in ()).throw(RuntimeError())
        })(), 'broken-request'),
    ):
        runtime, memory, conversation = _runtime(
            NavigateProvider([]), source
        )
        try:
            result = runtime.handle(_request(request_id))
            assert result.safety.allowed is False
            assert result.safety.code == 'untrusted_robot_state'
            assert result.state_trusted is False
            if request_id == 'stale-request':
                assert result.state_evidence_id == 'stale-state'
                assert result.state_observed_at == 90.0
            else:
                assert result.state_evidence_id is None
                assert result.state_observed_at is None
        finally:
            conversation.close()
            memory.close()


def test_future_state_fails_closed_and_exact_replay_remains_readable():
    evidence = RobotStateEvidence(
        state=RobotState(
            battery_percent=80,
            navigation_available=True,
            localization_ok=True,
        ),
        observed_at=101.0,
        evidence_id='future-state',
        trusted=True,
    )
    runtime, memory, conversation = _runtime(
        NavigateProvider([]),
        RecordingSource([], evidence),
    )
    try:
        first = runtime.handle(_request('future-request'))
        replay = runtime.handle(_request('future-request'))

        assert first.safety.code == 'untrusted_robot_state'
        assert first.state_evidence_id is None
        assert first.state_observed_at is None
        assert replay.safety.code == first.safety.code
        assert replay.state_evidence_id is None
        assert replay.state_observed_at is None
        assert replay.to_persisted_dict()['schema_version'] == 2
    finally:
        conversation.close()
        memory.close()


def test_confirmation_factory_cannot_rewrite_safety_provenance():
    evidence = RobotStateEvidence(
        state=RobotState(
            battery_percent=80,
            navigation_available=True,
            localization_ok=True,
        ),
        observed_at=100.0,
        evidence_id='trusted-state',
        trusted=True,
    )
    runtime, memory, conversation = _runtime(
        NavigateProvider([]),
        RecordingSource([], evidence),
    )

    def forge(result, token):
        del token
        result.state_evidence_id = 'forged-state'
        result.state_observed_at = 99.0
        result.safety_policy_revision = 'forged-policy'
        return None

    try:
        with pytest.raises(
            RuntimeError,
            match='modified safety provenance',
        ):
            runtime.handle(
                _request('forged-provenance-request'),
                confirmation_factory=forge,
            )
        assert conversation.list_turns(
            'state-user',
            'state-conversation',
        ) == []
    finally:
        conversation.close()
        memory.close()


def test_static_simulation_source_is_explicitly_nonphysical():
    source = StaticSimulationRobotStateSource(
        RobotState(
            battery_percent=70,
            navigation_available=True,
            localization_ok=True,
        ),
        clock=lambda: 10.0,
    )
    evidence = source.read()
    assert evidence.trusted is True
    assert evidence.evidence_id == 'swm25-131-static-simulation-state'
