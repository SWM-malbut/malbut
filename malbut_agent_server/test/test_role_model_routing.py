"""Offline integration tests for role-specific OpenAI models."""

from __future__ import annotations

import json

from malbut_agent_server.config import Settings
from malbut_agent_server.domain.front_route import (
    FrontRoute,
    FrontRouteMatch,
)
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.providers.routed import RoutedAgentProvider
from malbut_agent_server.schemas import AgentRequest, RobotState
from malbut_agent_server.tools import TOOL_SPECS


class MutableFrontRouter:
    """Return one selected route without any external classification."""

    def __init__(self, route: FrontRoute | None) -> None:
        """Store the route selected by the current test step."""
        self.route = route
        self.calls = 0

    def try_route(self, request):
        """Return the current match while counting exact attempts."""
        del request
        self.calls += 1
        if self.route is None:
            return None
        return FrontRouteMatch(route=self.route)


class RecordingTransport:
    """Return a valid response while retaining content-free payloads."""

    def __init__(self, decision_type: str) -> None:
        """Select a message or navigate proposal response."""
        self.decision_type = decision_type
        self.calls = []

    def __call__(self, url, headers, payload, timeout_seconds):
        """Record one request and return a model-labelled response."""
        del url, headers, timeout_seconds
        self.calls.append(payload)
        if self.decision_type == 'tool_call':
            output = [{
                'type': 'function_call',
                'name': 'navigate',
                'arguments': json.dumps(
                    {'location': '거실'},
                    ensure_ascii=False,
                ),
            }]
        else:
            output = [{
                'type': 'message',
                'role': 'assistant',
                'status': 'completed',
                'content': [{
                    'type': 'output_text',
                    'text': json.dumps({
                        'type': 'message',
                        'message': '안녕하세요.',
                        'reason': 'ordinary_chat',
                        'confidence': 1.0,
                    }, ensure_ascii=False),
                }],
            }]
        return {
            'id': 'response-test-only',
            'status': 'completed',
            'model': payload['model'],
            'output': output,
            'usage': {
                'input_tokens': 10,
                'output_tokens': 5,
                'total_tokens': 15,
            },
        }


def _settings(**overrides) -> Settings:
    values = {
        'provider': 'openai',
        'openai_api_key': 'test-only-openai-key',
        'openai_model': 'legacy-primary',
        'openai_fallback_model': 'legacy-fallback',
        'openai_general_model': 'gpt-4.1-mini',
        'openai_robot_planner_model': 'gpt-5.6-terra',
        'database_path': ':memory:',
        'provider_max_retries': 0,
    }
    values.update(overrides)
    return Settings(**values)


def _request(request_id: str, utterance: str) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        user_id='user-152',
        conversation_id='conversation-152',
        turn_id='turn-' + request_id,
        utterance=utterance,
        robot_state=RobotState(),
        available_tools=('navigate',),
    )


def _underlying(provider: ReliableProvider):
    assert len(provider._providers) == 1
    return provider._providers[0]


def test_each_route_sends_only_its_model_and_tool_scope() -> None:
    """Chat is tool-free while Planner receives the existing schema."""
    router = MutableFrontRouter(FrontRoute.GENERAL_CONVERSATION)
    runtime = build_orchestrator(
        _settings(),
        front_router=router,
    )
    try:
        routed = runtime.provider
        assert isinstance(routed, RoutedAgentProvider)
        general = RecordingTransport('message')
        planner = RecordingTransport('tool_call')
        _underlying(routed.general_provider).transport = general
        _underlying(routed.robot_planner_provider).transport = planner

        general_result = routed.complete(
            _request('general', '안녕'),
            [],
            [],
            [TOOL_SPECS['navigate']],
        )

        assert general_result.model == 'gpt-4.1-mini'
        assert general_result.decision.type == 'message'
        assert len(general.calls) == 1
        assert planner.calls == []
        assert general.calls[0]['model'] == 'gpt-4.1-mini'
        assert 'tools' not in general.calls[0]
        assert 'tool_choice' not in general.calls[0]
        assert 'reasoning' not in general.calls[0]

        router.route = FrontRoute.ROBOT_ACTION_REQUEST
        planner_result = routed.complete(
            _request('planner', '거실로 가줘'),
            [],
            [],
            [TOOL_SPECS['navigate']],
        )

        assert planner_result.model == 'gpt-5.6-terra'
        assert planner_result.decision.type == 'tool_call'
        assert len(general.calls) == 1
        assert len(planner.calls) == 1
        assert planner.calls[0]['model'] == 'gpt-5.6-terra'
        assert len(planner.calls[0]['tools']) == 1
        assert planner.calls[0]['tools'][0]['name'] == 'navigate'
        assert 'reasoning' not in planner.calls[0]
        assert router.calls == 2
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_explicit_planner_model_uses_model_neutral_payload() -> None:
    """A non-reasoning model can be selected for the Planner role."""
    router = MutableFrontRouter(FrontRoute.ROBOT_ACTION_REQUEST)
    runtime = build_orchestrator(
        _settings(
            openai_general_model='',
            openai_robot_planner_model='gpt-4.1-mini',
            openai_reasoning_effort='high',
        ),
        front_router=router,
    )
    planner = RecordingTransport('tool_call')
    try:
        routed = runtime.provider
        _underlying(routed.robot_planner_provider).transport = planner

        result = routed.complete(
            _request('mini-planner', '거실로 가줘'),
            [],
            [],
            [TOOL_SPECS['navigate']],
        )

        assert result.model == 'gpt-4.1-mini'
        assert result.decision.type == 'tool_call'
        assert len(planner.calls) == 1
        assert planner.calls[0]['model'] == 'gpt-4.1-mini'
        assert 'reasoning' not in planner.calls[0]
        assert len(planner.calls[0]['tools']) == 1
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_general_failure_does_not_call_or_open_the_planner() -> None:
    """One Chat failure cannot cascade across the role boundary."""
    router = MutableFrontRouter(FrontRoute.GENERAL_CONVERSATION)
    runtime = build_orchestrator(
        _settings(provider_failure_threshold=1),
        front_router=router,
    )
    fallback_calls = []
    planner = RecordingTransport('tool_call')

    def fail_general(*args):
        del args
        raise TimeoutError('test-only timeout')

    def fail_if_fallback_called(*args):
        fallback_calls.append(args)
        raise AssertionError('abstain fallback must not be called')

    try:
        routed = runtime.provider
        general_provider = routed.general_provider
        planner_provider = routed.robot_planner_provider
        _underlying(general_provider).transport = fail_general
        _underlying(planner_provider).transport = planner
        for adapter in routed.fallback_provider._providers:
            adapter.transport = fail_if_fallback_called

        failed = routed.complete(
            _request('general-failure', '안녕'),
            [],
            [],
            [TOOL_SPECS['navigate']],
        )

        assert failed.provider == 'reliable-fallback'
        assert failed.model == 'safe-non-action'
        assert general_provider._circuits[0].state.value == 'open'
        assert planner_provider._circuits[0].state.value == 'closed'
        assert fallback_calls == []

        router.route = FrontRoute.ROBOT_ACTION_REQUEST
        planned = routed.complete(
            _request('planner-after-failure', '거실로 가줘'),
            [],
            [],
            [TOOL_SPECS['navigate']],
        )

        assert planned.model == 'gpt-5.6-terra'
        assert planned.decision.type == 'tool_call'
        assert len(planner.calls) == 1
        assert fallback_calls == []
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_planner_failure_does_not_call_or_open_general_chat() -> None:
    """One Planner failure cannot make Chat inherit its circuit."""
    router = MutableFrontRouter(FrontRoute.ROBOT_ACTION_REQUEST)
    runtime = build_orchestrator(
        _settings(provider_failure_threshold=1),
        front_router=router,
    )
    fallback_calls = []
    general = RecordingTransport('message')

    def fail_planner(*args):
        del args
        raise TimeoutError('test-only timeout')

    def fail_if_fallback_called(*args):
        fallback_calls.append(args)
        raise AssertionError('abstain fallback must not be called')

    try:
        routed = runtime.provider
        general_provider = routed.general_provider
        planner_provider = routed.robot_planner_provider
        _underlying(general_provider).transport = general
        _underlying(planner_provider).transport = fail_planner
        for adapter in routed.fallback_provider._providers:
            adapter.transport = fail_if_fallback_called

        failed = routed.complete(
            _request('planner-failure', '거실로 가줘'),
            [],
            [],
            [TOOL_SPECS['navigate']],
        )

        assert failed.provider == 'reliable-fallback'
        assert planner_provider._circuits[0].state.value == 'open'
        assert general_provider._circuits[0].state.value == 'closed'
        assert fallback_calls == []

        router.route = FrontRoute.GENERAL_CONVERSATION
        chatted = routed.complete(
            _request('general-after-failure', '안녕'),
            [],
            [],
            [TOOL_SPECS['navigate']],
        )

        assert chatted.model == 'gpt-4.1-mini'
        assert chatted.decision.type == 'message'
        assert len(general.calls) == 1
        assert fallback_calls == []
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()


def test_abstain_keeps_role_models_idle_and_uses_legacy_chain() -> None:
    """Only an explicit Router abstention reaches the old Provider."""
    router = MutableFrontRouter(None)
    runtime = build_orchestrator(
        _settings(),
        front_router=router,
    )
    fallback = RecordingTransport('message')
    general_calls = []
    planner_calls = []

    def fail_general(*args):
        general_calls.append(args)
        raise AssertionError('general role must stay idle')

    def fail_planner(*args):
        planner_calls.append(args)
        raise AssertionError('planner role must stay idle')

    try:
        routed = runtime.provider
        _underlying(routed.general_provider).transport = fail_general
        _underlying(routed.robot_planner_provider).transport = fail_planner
        routed.fallback_provider._providers[0].transport = fallback

        result = routed.complete(
            _request('abstain', '분류 보류 요청'),
            [],
            [],
            [TOOL_SPECS['navigate']],
        )

        assert result.model == 'legacy-primary'
        assert result.decision.type == 'message'
        assert len(fallback.calls) == 1
        assert fallback.calls[0]['model'] == 'legacy-primary'
        assert len(fallback.calls[0]['tools']) == 1
        assert general_calls == []
        assert planner_calls == []
    finally:
        runtime.conversation_store.close()
        runtime.memory_store.close()
