"""Require model Tool selection and a Manager round trip before weather answers."""

from concurrent.futures import CancelledError
import copy
from threading import Thread
import time
import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.application.front_routing import FrontRoutingService
from malbut_agent_server.domain.front_route import FrontRoute, FrontRouteMatch
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.gateway import (
    PROPOSAL_ONLY, READ_ONLY, SIMULATION_ONLY, ToolCapability, ToolGateway,
    ToolQuery, production_registry, simulation_registry,
)
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.providers.routed import RoutedAgentProvider
from malbut_agent_server.schemas import AgentDecision, AgentRequest, ProviderResult, ProviderUsage
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.tools import TOOL_SPECS, validate_tool_arguments
from malbut_agent_server.weather_query import ManagerWeatherQuery


class WeatherProvider(AgentProvider):
    """Record two model calls without using inference or any network."""

    def __init__(self, first=None, second=None):
        self.calls = []
        self.first = first or AgentDecision(
            type='tool_call', message='날씨를 확인할게요.',
            tool_name='get_weather', arguments={},
        )
        self.second = second

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, weather_context=None):
        self.calls.append({
            'request': copy.deepcopy(request), 'weather': copy.deepcopy(weather_context),
            'tools': [tool.name for tool in tools],
        })
        if weather_context is None:
            return ProviderResult(copy.deepcopy(self.first), 'weather-fixture', 'fixed', 12,
                                  usage=ProviderUsage(10, 2, 12), input_chars=30)
        answer = self.second or AgentDecision('message', weather_context['status'])
        return ProviderResult(copy.deepcopy(answer), 'weather-fixture', 'fixed', 23,
                              usage=ProviderUsage(20, 3, 23), input_chars=40)


def request(*, request_id='weather-1', tools=('get_weather',), text='지금 날씨가 어때?'):
    return AgentRequest.from_dict({
        'request_id': request_id, 'user_id': 'weather-user',
        'conversation_id': 'weather-conversation', 'turn_id': request_id,
        'utterance': text, 'robot_state': {'emergency_stop': True},
        'available_tools': list(tools),
    })


@pytest.fixture
def runtime():
    value = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
    value.conversation_store.create('weather-user', 'weather-conversation')
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize('status', ['fresh', 'stale', 'unavailable'])
def test_tool_selection_precedes_one_manager_read_and_one_answer(runtime, status):
    provider = WeatherProvider()
    calls = []
    weather = {'status': status, 'location': '시험 지역'}

    def execute(request_id):
        assert len(provider.calls) == 1
        assert provider.calls[0]['weather'] is None
        calls.append(request_id)
        return weather

    runtime.provider, runtime.weather_executor = provider, execute
    result = runtime.handle(request())
    assert calls == ['weather-1']
    assert len(provider.calls) == 2
    assert provider.calls[0]['tools'] == ['get_weather']
    assert provider.calls[1]['tools'] == []
    assert provider.calls[1]['request'].available_tools == ()
    assert provider.calls[1]['weather'] == weather
    assert all(call['request'].utterance == '지금 날씨가 어때?' for call in provider.calls)
    assert result.raw_decision.tool_name == 'get_weather'
    assert result.raw_decision.type == 'tool_call'
    assert result.decision.type == 'message' and result.decision.message == status
    assert result.provider_result.decision == result.decision
    assert result.provider_result.usage == ProviderUsage(30, 5, 35)
    assert result.provider_result.latency_ms == 35
    assert result.provider_result.input_chars == 70
    assert result.safety.allowed and result.state_trusted is False
    cached = runtime.handle(request())
    assert cached.raw_decision == result.raw_decision
    assert len(provider.calls) == 2 and len(calls) == 1


def test_plain_message_does_not_prefetch_weather(runtime):
    provider = WeatherProvider(first=AgentDecision('message', '안녕하세요.'))
    calls = []
    runtime.provider = provider
    runtime.weather_executor = lambda value: calls.append(value)
    result = runtime.handle(request(text='안녕'))
    assert result.decision.message == '안녕하세요.'
    assert len(provider.calls) == 1 and calls == []
    assert provider.calls[0]['weather'] is None


@pytest.mark.parametrize('first,tools', [
    (AgentDecision('tool_call', '조회', tool_name='get_weather', arguments={'location': '서울'}),
     ('get_weather',)),
    (AgentDecision('tool_call', '조회', tool_name='get_weather', arguments={}), ()),
])
def test_unavailable_or_invalid_weather_tool_never_reaches_manager(runtime, first, tools):
    calls = []
    provider = WeatherProvider(first=first)
    runtime.provider, runtime.weather_executor = provider, lambda value: calls.append(value)
    result = runtime.handle(request(tools=tools))
    assert result.decision.type == 'refusal' and not result.safety.allowed
    assert calls == [] and len(provider.calls) == 1


def test_no_manager_executor_removes_weather_tool(runtime):
    provider = WeatherProvider()
    runtime.provider = provider
    result = runtime.handle(request())
    assert provider.calls[0]['tools'] == []
    assert 'get_weather' not in provider.calls[0]['request'].available_tools
    assert len(provider.calls) == 1
    assert result.decision.type == 'refusal'


@pytest.mark.parametrize('outcome', [TimeoutError('manager offline'), None, {'status': 'unknown'}])
def test_manager_failure_becomes_unavailable_without_direct_cache(runtime, outcome):
    calls = []

    def execute(request_id):
        calls.append(request_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    provider = WeatherProvider()
    runtime.provider, runtime.weather_executor = provider, execute
    result = runtime.handle(request())
    assert calls == ['weather-1'] and len(provider.calls) == 2
    assert provider.calls[1]['weather'] == {'status': 'unavailable'}
    assert result.decision.type == 'message' and result.decision.message == 'unavailable'


def test_shutdown_cancels_wait_without_a_second_model_call(runtime):
    query = ManagerWeatherQuery(None, timeout_s=2.0)
    provider = WeatherProvider()
    runtime.provider, runtime.weather_executor = provider, query.execute

    def close_when_waiting():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with query._lock:
                if query._pending:
                    query.close()
                    return
            time.sleep(0.001)

    closer = Thread(target=close_when_waiting)
    closer.start()
    try:
        with pytest.raises(CancelledError):
            runtime.handle(request())
        assert len(provider.calls) == 1
        assert provider.calls[0]['weather'] is None
        assert query._pending == {}
    finally:
        query.close()
        closer.join(3)
    assert not closer.is_alive()


@pytest.mark.parametrize('tool', ['get_weather', 'navigate'])
def test_followup_cannot_request_another_tool(runtime, tool):
    provider = WeatherProvider(second=AgentDecision(
        'tool_call', '다시 실행', tool_name=tool,
        arguments={'location': '거실'} if tool == 'navigate' else {},
    ))
    calls = []
    runtime.provider = provider
    runtime.weather_executor = lambda value: (calls.append(value) or {'status': 'fresh'})
    result = runtime.handle(request())
    assert len(provider.calls) == 2 and calls == ['weather-1']
    assert result.raw_decision.tool_name == 'get_weather'
    assert result.decision.type == 'refusal'
    assert result.decision.reason == 'weather_followup_tool_forbidden'
    assert result.provider_result.usage == ProviderUsage(30, 5, 35)


def test_new_turn_performs_a_new_manager_round_trip(runtime):
    statuses = iter(['fresh', 'stale'])
    runtime.provider = WeatherProvider()
    runtime.weather_executor = lambda _: {'status': next(statuses)}
    assert runtime.handle(request()).decision.message == 'fresh'
    assert runtime.handle(request(request_id='weather-2')).decision.message == 'stale'
    assert len(runtime.provider.calls) == 4


def test_mock_weather_uses_tool_loop_and_no_robot_state(runtime):
    runtime.provider = MockProvider()
    calls = []
    runtime.weather_executor = lambda value: (calls.append(value) or {'status': 'fresh'})
    result = runtime.handle(request())
    assert calls == ['weather-1']
    assert result.raw_decision.tool_name == 'get_weather'
    assert result.decision.reason == 'mock_weather_result'
    assert result.decision.type == 'message'


def test_weather_safety_exception_does_not_relax_robot_safety():
    policy = SafetyPolicy()
    weather = AgentDecision('tool_call', '조회', tool_name='get_weather', arguments={})
    assert policy.evaluate(request(), weather, state_trusted=False).allowed
    navigate = AgentDecision('tool_call', '이동', tool_name='navigate', arguments={'location': '거실'})
    assert policy.evaluate(request(tools=('navigate',)), navigate,
                           state_trusted=False).code == 'untrusted_robot_state'
    assert policy.evaluate(request(tools=('navigate',)), navigate,
                           state_trusted=True).code == 'emergency_stop'


def test_weather_schema_and_gateway_require_manager_even_in_simulation():
    assert TOOL_SPECS['get_weather'].parameters['additionalProperties'] is False
    assert validate_tool_arguments('get_weather', {}) == {}
    for registry in (production_registry(), simulation_registry()):
        capability = registry.get('get_weather')
        assert capability.mode == PROPOSAL_ONLY and capability.adapter is None
        gateway = ToolGateway(registry)
        try:
            result = gateway.query(ToolQuery('weather-query', 'user', 'get_weather', {}))
            assert result.status == 'rejected'
        finally:
            gateway.close()
    for mode in (READ_ONLY, SIMULATION_ONLY):
        with pytest.raises(ValueError, match='Manager'):
            ToolCapability('get_weather', mode=mode)


def test_factory_only_accepts_explicit_manager_executor():
    calls = []
    value = build_orchestrator(Settings(database_path=':memory:'), http_server=False,
                               weather_executor=lambda request_id: calls.append(request_id))
    try:
        assert callable(value.weather_executor) and calls == []
        assert not hasattr(value, 'weather_source')
    finally:
        value.close()


@pytest.mark.parametrize('reliable', [False, True])
def test_general_routing_keeps_only_weather_and_completes_the_tool_loop(runtime, reliable):
    class GeneralRouter:
        def try_route(self, request):
            return FrontRouteMatch(FrontRoute.GENERAL_CONVERSATION)

    child = WeatherProvider()
    provider = RoutedAgentProvider(
        FrontRoutingService(GeneralRouter()), general_provider=child,
        robot_planner_provider=child, fallback_provider=child,
    )
    runtime.provider = ReliableProvider([provider]) if reliable else provider
    calls = []
    runtime.weather_executor = lambda value: (calls.append(value) or {'status': 'fresh'})
    result = runtime.handle(request(tools=('navigate', 'get_weather')))
    assert calls == ['weather-1']
    assert [call['tools'] for call in child.calls] == [['get_weather'], []]
    assert result.raw_decision.tool_name == 'get_weather'
    assert result.decision.message == 'fresh'
    assert result.provider_result.usage == ProviderUsage(30, 5, 35)


def test_unknown_usage_is_not_counted_as_zero(runtime):
    class PartialUsageProvider(WeatherProvider):
        def complete(self, *args, **kwargs):
            result = super().complete(*args, **kwargs)
            if len(self.calls) == 1:
                result.usage = ProviderUsage(10, None, None)
            return result

    runtime.provider = PartialUsageProvider()
    runtime.weather_executor = lambda _: {'status': 'fresh'}
    result = runtime.handle(request())
    assert result.provider_result.usage == ProviderUsage(30, None, None)


@pytest.mark.parametrize('context', [
    {'status': 'location_set', 'location': '경기도 수원시 팔달구 우만1동'},
    {'status': 'location_ambiguous', 'candidates': ['수원시 우만1동', '수원시 우만2동']},
    {'status': 'location_not_found'},
])
@pytest.mark.parametrize('utterance', [
    '여기 성남 아닌데 수원시 우만1동이야',
    '여기 성남이 아니라 수원시 우만1동이야',
    '날씨 위치를 수원시 우만1동으로 바꿔줘',
    '날씨 위치를 수원시 우만1동으로 저장해줘',
])
def test_location_setting_uses_manager_result_without_weather_or_robot_execution(
    runtime, context, utterance,
):
    calls = []
    provider = WeatherProvider(first=AgentDecision(
        'tool_call', '위치를 바꿀게요.', tool_name='set_weather_location',
        arguments={'location': '수원시 우만1동'},
    ))
    runtime.provider = provider
    runtime.weather_location_executor = lambda key, place: (calls.append((key, place)) or context)
    result = runtime.handle(request(
        text=utterance,
        tools=('get_weather', 'set_weather_location'),
    ))
    assert calls == [('weather-1', '수원시 우만1동')]
    assert result.raw_decision.tool_name == 'set_weather_location'
    if context['status'] == 'location_set':
        assert result.decision.reason == 'weather_location_saved'
        assert context['location'] in result.decision.message
        assert '저장했어요' in result.decision.message
    else:
        assert result.decision.type == 'clarification'
    assert result.safety.allowed and not result.state_trusted
    assert [call['tools'] for call in provider.calls] == [['set_weather_location']]
    cached = runtime.handle(request(
        text=utterance,
        tools=('get_weather', 'set_weather_location'),
    ))
    assert len(calls) == 1
    assert cached.raw_decision.tool_name == 'set_weather_location'


def test_location_tool_is_removed_without_manager_setter(runtime):
    provider = WeatherProvider(first=AgentDecision('message', '안녕하세요.'))
    runtime.provider = provider
    runtime.handle(request(text='여기 수원이야', tools=('set_weather_location',)))
    assert provider.calls[0]['tools'] == []


def test_missing_saved_location_reaches_provider_as_clarification_context(runtime):
    runtime.provider = WeatherProvider()
    runtime.weather_executor = lambda _: {'status': 'location_required'}
    result = runtime.handle(request())
    assert result.decision.message == 'location_required'
    assert runtime.provider.calls[1]['weather'] == {'status': 'location_required'}


def test_location_tool_cannot_bypass_manager_in_production_or_simulation():
    for registry in (production_registry(), simulation_registry()):
        capability = registry.get('set_weather_location')
        assert capability.mode == PROPOSAL_ONLY and capability.adapter is None
    for mode in (READ_ONLY, SIMULATION_ONLY):
        with pytest.raises(ValueError, match='Manager'):
            ToolCapability('set_weather_location', mode=mode)


def test_failed_location_write_cannot_preserve_a_model_save_claim(runtime):
    runtime.provider = WeatherProvider(first=AgentDecision(
        'tool_call', '수원으로 저장했어요.', reason='weather_location_saved',
        tool_name='set_weather_location', arguments={'location': '수원시 우만1동'},
    ))
    runtime.weather_location_executor = lambda *_: {'status': 'unavailable'}
    result = runtime.handle(request(
        text='날씨 위치를 수원시 우만1동으로 저장해줘', tools=('set_weather_location',),
    ))
    assert result.decision.reason == 'weather_location_unavailable'
    assert '저장하지 못했어요' in result.decision.message
    assert '저장했어요' not in result.decision.message
