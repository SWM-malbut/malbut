"""Contracts for selecting one specialist without granting authority."""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from malbut_agent_server.application.front_routing import (
    FrontRoutingService,
)
from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.domain.front_route import (
    MAX_FRONT_HISTORY_CHARS,
    MAX_FRONT_HISTORY_MESSAGES,
    MAX_FRONT_HISTORY_MESSAGE_CHARS,
    FrontRoute,
    FrontRouteMatch,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
)
from malbut_agent_server.providers.routed import RoutedAgentProvider
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    RobotState,
)
from malbut_agent_server.tools import TOOL_SPECS, ToolSpec


class ScriptedFrontRouter:
    """Return one scripted match while recording its bounded request."""

    def __init__(
        self,
        result: Any = None,
        *,
        error: Exception | None = None,
    ) -> None:
        """Configure one result or one adapter failure."""
        self.result = result
        self.error = error
        self.calls = 0
        self.requests = []

    def try_route(self, request):
        """Record and return without retrying."""
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingProvider(AgentProvider):
    """Return one decision and record every legacy Provider argument."""

    def __init__(
        self,
        decision: AgentDecision | None = None,
        *,
        error: Exception | None = None,
        name: str = 'recording-provider',
    ) -> None:
        """Configure a successful result or one selected failure."""
        self.decision = decision or AgentDecision(
            type='message',
            message='확인했습니다.',
        )
        self.error = error
        self.name = name
        self.calls = 0
        self.requests = []
        self.memories = []
        self.histories = []
        self.tool_lists = []
        self.summaries = []
        self.results = []

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        """Record one logical call and return its normalized result."""
        self.calls += 1
        self.requests.append(request)
        self.memories.append(memories)
        self.histories.append(conversation_turns)
        self.tool_lists.append(tools)
        self.summaries.append(conversation_summary)
        if self.error is not None:
            raise self.error
        result = ProviderResult(
            decision=self.decision,
            provider=self.name,
            model='fixture-model',
            latency_ms=1.25,
            input_chars=len(request.utterance),
        )
        self.results.append(result)
        return result


def _request(
    *,
    utterance: str = '거실로 가줘',
) -> AgentRequest:
    return AgentRequest(
        request_id='request-151',
        user_id='user-151',
        conversation_id='conversation-151',
        turn_id='turn-151',
        utterance=utterance,
        robot_state=RobotState(),
        available_tools=('navigate',),
    )


def _turn(
    ordinal: int,
    user_content: str,
    assistant_content: str,
) -> ConversationTurn:
    return ConversationTurn(
        conversation_id='conversation-151',
        user_id='user-151',
        session_instance_id='session-151',
        turn_id=f'history-turn-{ordinal}',
        request_id=f'history-request-{ordinal}',
        request_fingerprint=f'fingerprint-{ordinal}',
        generation=1,
        ordinal=ordinal,
        user_content=user_content,
        assistant_content=assistant_content,
        response={},
        created_at=float(ordinal),
        completed_at=float(ordinal) + 0.5,
    )


def _routed(
    router: ScriptedFrontRouter,
    *,
    general: RecordingProvider | None = None,
    planner: RecordingProvider | None = None,
    fallback: RecordingProvider | None = None,
) -> tuple[
    RoutedAgentProvider,
    RecordingProvider,
    RecordingProvider,
    RecordingProvider,
]:
    general = general or RecordingProvider(name='general')
    planner = planner or RecordingProvider(name='planner')
    fallback = fallback or RecordingProvider(name='fallback')
    provider = RoutedAgentProvider(
        FrontRoutingService(router),
        general_provider=general,
        robot_planner_provider=planner,
        fallback_provider=fallback,
    )
    return provider, general, planner, fallback


def _complete(
    provider: RoutedAgentProvider,
    *,
    history: list[ConversationTurn] | None = None,
    utterance: str = '거실로 가줘',
) -> ProviderResult:
    return provider.complete(
        _request(utterance=utterance),
        [],
        history or [],
        [TOOL_SPECS['navigate']],
    )


@pytest.mark.parametrize(
    ('route', 'expected_decision', 'expected_calls'),
    (
        (
            FrontRoute.GENERAL_CONVERSATION,
            'message',
            (1, 0, 0),
        ),
        (
            FrontRoute.CLARIFICATION_REQUIRED,
            'clarification',
            (0, 0, 0),
        ),
        (
            FrontRoute.ROBOT_STATUS_QUERY,
            'refusal',
            (0, 0, 0),
        ),
        (
            FrontRoute.CURRENT_ACTION_QUERY,
            'refusal',
            (0, 0, 0),
        ),
        (
            FrontRoute.ROBOT_ACTION_REQUEST,
            'message',
            (0, 1, 0),
        ),
    ),
)
def test_each_route_selects_only_its_owned_handler(
    route: FrontRoute,
    expected_decision: str,
    expected_calls: tuple[int, int, int],
) -> None:
    """A closed match invokes one provider branch or one local branch."""
    router = ScriptedFrontRouter(FrontRouteMatch(route=route))
    provider, general, planner, fallback = _routed(router)

    result = _complete(provider)

    assert router.calls == 1
    assert result.decision.type == expected_decision
    assert (general.calls, planner.calls, fallback.calls) == (
        expected_calls
    )
    if route in {
        FrontRoute.CLARIFICATION_REQUIRED,
        FrontRoute.ROBOT_STATUS_QUERY,
        FrontRoute.CURRENT_ACTION_QUERY,
    }:
        assert result.provider == 'malbut-front-policy'
        assert result.model == 'malbut-front-provider-routing-v1'
        assert result.latency_ms == 0.0
        assert result.input_chars == 0


def test_abstain_calls_only_the_existing_fallback_once() -> None:
    """None is the sole normal path back to the universal Provider."""
    router = ScriptedFrontRouter(None)
    provider, general, planner, fallback = _routed(router)

    result = _complete(provider)

    assert router.calls == 1
    assert (general.calls, planner.calls, fallback.calls) == (0, 0, 1)
    assert result is fallback.results[0]
    assert fallback.requests[0].available_tools == ('navigate',)
    assert fallback.tool_lists[0] == [TOOL_SPECS['navigate']]


@pytest.mark.parametrize(
    'router',
    (
        ScriptedFrontRouter(error=RuntimeError('private adapter error')),
        ScriptedFrontRouter(result='robot_action_request'),
    ),
    ids=['exception', 'invalid-result'],
)
def test_router_failure_returns_local_refusal_without_fallback(
    router: ScriptedFrontRouter,
) -> None:
    """A malformed Router outcome cannot masquerade as abstention."""
    provider, general, planner, fallback = _routed(router)

    result = _complete(provider)

    assert router.calls == 1
    assert (general.calls, planner.calls, fallback.calls) == (0, 0, 0)
    assert result.decision.type == 'refusal'
    assert result.decision.reason == 'front_route:router_unavailable'
    assert 'private adapter error' not in result.decision.message


@pytest.mark.parametrize(
    'route',
    (
        FrontRoute.GENERAL_CONVERSATION,
        FrontRoute.ROBOT_ACTION_REQUEST,
    ),
)
def test_selected_provider_failure_never_cascades(
    route: FrontRoute,
) -> None:
    """A failed selected attempt cannot trigger a second Provider."""
    selected = RecordingProvider(
        error=ProviderError('selected failed'),
        name='selected',
    )
    router = ScriptedFrontRouter(FrontRouteMatch(route=route))
    provider, general, planner, fallback = _routed(
        router,
        general=(
            selected
            if route is FrontRoute.GENERAL_CONVERSATION
            else None
        ),
        planner=(
            selected
            if route is FrontRoute.ROBOT_ACTION_REQUEST
            else None
        ),
    )

    with pytest.raises(ProviderError, match='selected failed'):
        _complete(provider)

    assert router.calls == 1
    assert selected.calls == 1
    assert fallback.calls == 0
    assert general.calls + planner.calls == 1


def test_general_route_removes_tools_from_both_input_channels() -> None:
    """Chat cannot see Tool names through either legacy Provider input."""
    router = ScriptedFrontRouter(FrontRouteMatch(
        route=FrontRoute.GENERAL_CONVERSATION,
    ))
    provider, general, planner, fallback = _routed(router)
    original = _request(utterance='오늘 기분 어때?')

    provider.complete(
        original,
        [],
        [],
        [TOOL_SPECS['navigate']],
    )

    assert general.calls == 1
    assert general.requests[0] is not original
    assert general.requests[0].available_tools == ()
    assert general.tool_lists[0] == []
    assert original.available_tools == ('navigate',)
    assert (planner.calls, fallback.calls) == (0, 0)


def test_general_route_blocks_a_wrong_route_tool_proposal() -> None:
    """A Chat Provider cannot turn a route hint into confirmation."""
    general = RecordingProvider(AgentDecision(
        type='tool_call',
        message='거실로 이동할까요?',
        tool_name='navigate',
        arguments={'location': '거실'},
    ))
    router = ScriptedFrontRouter(FrontRouteMatch(
        route=FrontRoute.GENERAL_CONVERSATION,
    ))
    provider, general, planner, fallback = _routed(
        router,
        general=general,
    )

    result = _complete(provider, utterance='안녕')

    assert general.calls == 1
    assert (planner.calls, fallback.calls) == (0, 0)
    assert result.decision.type == 'refusal'
    assert result.decision.tool_name is None
    assert result.decision.arguments == {}
    assert result.decision.reason == (
        'front_route:general_tool_forbidden'
    )
    assert result.provider == 'recording-provider'
    assert result.model == 'fixture-model'
    assert result.latency_ms == 1.25
    assert result.input_chars == len('안녕')


def test_front_projection_skips_blank_assistant_and_keeps_newest() -> None:
    """Only a bounded chronological suffix reaches the Router."""
    history = [
        _turn(1, 'old-' + ('가' * 400), ''),
        *[
            _turn(index, f'user-{index}', f'assistant-{index}')
            for index in range(2, 11)
        ],
    ]
    router = ScriptedFrontRouter(FrontRouteMatch(
        route=FrontRoute.GENERAL_CONVERSATION,
    ))
    provider, _general, _planner, _fallback = _routed(router)

    _complete(
        provider,
        history=history,
        utterance='current-message',
    )

    front_request = router.requests[0]
    assert front_request.user_message == 'current-message'
    assert len(front_request.recent_messages) == 16
    assert [
        message.content
        for message in front_request.recent_messages
    ] == [
        item
        for index in range(3, 11)
        for item in (f'user-{index}', f'assistant-{index}')
    ]
    assert all(
        message.content != 'current-message'
        for message in front_request.recent_messages
    )
    assert not hasattr(front_request, 'user_id')
    assert not hasattr(front_request, 'robot_state')
    assert not hasattr(front_request, 'available_tools')


def test_front_projection_enforces_the_total_history_budget() -> None:
    """Large valid turns retain the newest whole messages under 4000."""
    history = [
        _turn(
            index,
            f'u{index}-' + ('가' * 400),
            f'a{index}-' + ('나' * 400),
        )
        for index in range(1, 13)
    ]
    router = ScriptedFrontRouter(None)
    provider, _general, _planner, _fallback = _routed(router)

    _complete(provider, history=history)

    messages = router.requests[0].recent_messages
    assert len(messages) <= MAX_FRONT_HISTORY_MESSAGES
    assert all(
        len(message.content) <= MAX_FRONT_HISTORY_MESSAGE_CHARS
        for message in messages
    )
    assert sum(len(message.content) for message in messages) <= (
        MAX_FRONT_HISTORY_CHARS
    )
    assert messages[-1].content.startswith('a12-')


@pytest.mark.parametrize(
    'policy_revision',
    ('', ' leading', 'trailing ', '한글-policy', 'x' * 129),
)
def test_policy_revision_is_bounded_ascii(
    policy_revision: str,
) -> None:
    """Local route metadata cannot carry arbitrary or private text."""
    router = ScriptedFrontRouter(None)
    provider = RecordingProvider()
    with pytest.raises(ValueError, match='policy_revision'):
        RoutedAgentProvider(
            FrontRoutingService(router),
            general_provider=provider,
            robot_planner_provider=provider,
            fallback_provider=provider,
            policy_revision=policy_revision,
        )
