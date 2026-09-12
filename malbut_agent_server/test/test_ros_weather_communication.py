"""Exercise on-demand weather through real Manager and downstream Actions."""

import copy
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest


rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')

from action_msgs.msg import GoalStatus  # noqa: E402
from malbut_interfaces.action import GetWeather  # noqa: E402
from malbut_interfaces.msg import (  # noqa: E402
    SpeechRequest, SpeechTranscript,
)
from rclpy.action import ActionClient  # noqa: E402
from rclpy.executors import (  # noqa: E402
    MultiThreadedExecutor, SingleThreadedExecutor,
)
from rclpy.node import Node  # noqa: E402
import yaml  # noqa: E402

from malbut_agent_server import ros_communication, weather_action  # noqa: E402
from malbut_agent_server.config import Settings  # noqa: E402
from malbut_agent_server.factory import build_orchestrator  # noqa: E402
from malbut_agent_server.schemas import (  # noqa: E402
    AgentDecision, ProviderResult,
)
from malbut_agent_server.weather import (  # noqa: E402
    SOURCE, WeatherForecast, WeatherState,
)
from malbut_agent_server.weather_action import (  # noqa: E402
    create_weather_action_node,
)
from malbut_agent_server.weather_kma import (  # noqa: E402
    KmaWeatherClient, KmaWeatherError,
)
from malbut_agent_server.weather_location_store import WeatherLocationStore  # noqa: E402
from malbut_system_manager.system_manager_node import (  # noqa: E402
    SystemManagerNode,
)


MANIFEST = (
    Path(__file__).resolve().parents[2]
    / 'malbut_interfaces/capabilities/get_weather.yaml'
)
PRIVATE_ERROR = 'private-weather-backend-body'


def weather_state(temperature=23.5, *, location='시험 지역', latitude=37.0,
                  longitude=127.0, timezone='Asia/Seoul'):
    """Return synthetic observation and forecast data with local dates."""
    now = time.time()
    today = datetime.fromtimestamp(now, ZoneInfo(timezone)).date()
    return WeatherState(
        fetched_at=now, valid_at=now - 300,
        location=location, latitude=latitude, longitude=longitude,
        source=SOURCE, timezone=timezone,
        temperature_c=temperature, weather_code=0,
        daily=(
            WeatherForecast(today.isoformat(), 27.0, 18.0, 10.0, 0),
            WeatherForecast((today + timedelta(days=1)).isoformat(),
                            24.0, 17.0, 70.0, 61),
        ),
    )


class _WeatherClient:
    """Count real Action fetches and let tests hold the HTTP seam in flight."""

    def __init__(self):
        self.calls = 0
        self.state = weather_state()
        self.error = False
        self.block = False
        self.started = Event()
        self.release = Event()
        self.finished = Event()

    def fetch(self):
        self.calls += 1
        self.started.set()
        try:
            if self.block and not self.release.wait(15.0):
                raise TimeoutError(PRIVATE_ERROR)
            if isinstance(self.error, Exception):
                raise self.error
            if self.error:
                raise OSError(PRIVATE_ERROR)
            return self.state
        finally:
            self.finished.set()


class _WeatherProvider:
    """Choose a Tool only for weather questions, then use its result."""

    def __init__(self):
        self.calls = []

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, weather_context=None):
        self.calls.append({
            'request': copy.deepcopy(request),
            'weather': copy.deepcopy(weather_context),
            'tools': [tool.name for tool in tools],
        })
        if weather_context is None:
            assert {tool.name for tool in tools} == {'get_weather', 'set_weather_location'}
            if request.utterance.startswith('여기는 '):
                decision = AgentDecision(
                    type='tool_call', message='', tool_name='set_weather_location',
                    arguments={'location': request.utterance.removeprefix('여기는 ')},
                )
            elif '날씨' in request.utterance:
                decision = AgentDecision(
                    type='tool_call', message='', tool_name='get_weather',
                    arguments={},
                )
            else:
                decision = AgentDecision(type='message', message='안녕하세요.')
        else:
            assert not tools, 'The follow-up must not trigger another Tool'
            if weather_context['status'] == 'fresh':
                temperature = weather_context['current']['temperature_c']
                location = weather_context['location']
                text = f'{location} 현재 기온은 {temperature}도예요.'
            elif weather_context['status'] == 'location_required':
                text = '어느 지역 날씨를 알려드릴까요?'
            elif weather_context['status'] == 'location_set':
                text = f"날씨 조회 지역을 {weather_context['location']}(으)로 저장했어요."
            else:
                assert 'current' not in weather_context
                assert 'daily' not in weather_context
                text = '지금 사용할 수 있는 최신 날씨 정보가 없어요.'
            decision = AgentDecision(type='message', message=text)
        return ProviderResult(
            decision=decision, provider='ros-weather-fixture',
            model='fixed', latency_ms=0.0,
        )


@pytest.fixture
def ros_weather(tmp_path, monkeypatch):
    """Keep Agent SQLite on one thread and Action callbacks on another."""
    monkeypatch.delenv('KMA_SERVICE_KEY', raising=False)
    monkeypatch.setenv('ROS_DOMAIN_ID', '197')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    location_db_path = str(tmp_path / 'weather-location.sqlite3')
    rclpy.init(args=[
        '--ros-args', '-p',
        f'malbut_weather:weather_location_path:={location_db_path}',
    ])
    executor = SingleThreadedExecutor()
    background = MultiThreadedExecutor(num_threads=6)
    thread = Thread(target=background.spin, daemon=True)
    provider, client = _WeatherProvider(), _WeatherClient()
    location_store = WeatherLocationStore(location_db_path)
    nodes, replies, receipts, events, goals = [], [], [], [], []
    settings = Settings(
        user_id='weather-test-user',
        database_path=str(tmp_path / 'dialogue.db'),
    )
    original_receive = ros_communication.receive_transcript
    original_goal = SystemManagerNode._goal

    def receive(receipt_store, utterance_id, text, logger):
        outcome = original_receive(receipt_store, utterance_id, text, logger)
        receipts.append((utterance_id, text, outcome))
        return outcome

    def goal(manager, request):
        goals.append((request.capability_id,
                      yaml.safe_load(request.arguments_yaml)))
        return original_goal(manager, request)

    def forbid_http(_client):
        raise AssertionError('Tests must use only the injected API client')

    monkeypatch.setattr(ros_communication, 'receive_transcript', receive)
    monkeypatch.setattr(SystemManagerNode, '_goal', goal)
    monkeypatch.setattr(KmaWeatherClient, 'fetch', forbid_http)

    def spin_until(predicate):
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if predicate():
                return
            executor.spin_once(timeout_sec=0.02)
        raise AssertionError('Weather mission or speech evidence is missing')

    def runtime_factory():
        runtime = build_orchestrator(settings, http_server=False)
        runtime.provider = provider
        return runtime

    run = SimpleNamespace(
        client=client, provider=provider, replies=replies, receipts=receipts,
        events=events, goals=goals, spin_until=spin_until,
        agent=None, manager=None, weather=None,
        location_db_path=location_db_path, location_store=location_store,
        resolve_location=None,
    )
    sender = Node('weather_dialogue_test_publisher')
    executor.add_node(sender)
    speech_publisher = sender.create_publisher(
        SpeechTranscript, '/malbut/speech/transcript', 10,
    )
    sender.create_subscription(
        SpeechRequest, '/malbut/speech/response',
        lambda message: replies.append(message.text), 10,
    )
    run.publish_speech = speech_publisher.publish

    def start(*, with_manager=True, with_weather=True, timeout_s=5.0,
              auto_location=False):
        if with_weather:
            run.weather = create_weather_action_node(
                client=None if auto_location else client, timeout_s=timeout_s,
                location_store=location_store, location_resolver=run.resolve_location,
            )
            nodes.append(run.weather)
        if with_manager:
            directory = tmp_path / 'manifests'
            directory.mkdir()
            (directory / MANIFEST.name).write_bytes(MANIFEST.read_bytes())
            setter_manifest = MANIFEST.with_name('set_weather_location.yaml')
            (directory / setter_manifest.name).write_bytes(setter_manifest.read_bytes())
            run.manager = SystemManagerNode(manifest_directory=str(directory))
            manifest = run.manager._registry.get('get_weather')
            assert manifest.command_kind.value == 'ACTION'
            assert manifest.command_name == '/malbut/weather/get'
            assert manifest.command_type == (
                'malbut_interfaces/action/GetWeather'
            )
            nodes.append(run.manager)
        for node in nodes:
            background.add_node(node)
        thread.start()
        run.agent = ros_communication.create_communication_node(
            speech_db_path=str(tmp_path / 'receipts.db'),
            dialogue_settings=settings, dialogue_factory=runtime_factory,
            on_event=events.append, goal_response_timeout_s=1.0,
        )
        executor.add_node(run.agent)
        spin_until(lambda: (
            speech_publisher.get_subscription_count() == 1
            and run.agent._speech.get_subscription_count() == 1
        ))
        assert not any(s.topic_name == '/malbut/weather'
                       for s in run.agent.subscriptions)
        assert not any(c.srv_name.startswith('/malbut/weather/get')
                       for c in run.agent.clients)
        if with_manager:
            spin_until(run.agent.missions._client.server_is_ready)
        if with_weather:
            probe = ActionClient(sender, GetWeather, '/malbut/weather/get')
            try:
                spin_until(probe.server_is_ready)
            finally:
                probe.destroy()
        assert client.calls == 0, 'Node startup must not fetch weather'

    def send(text, utterance_id=None):
        utterance_id = utterance_id or str(uuid4())
        assert str(UUID(utterance_id)) == utterance_id
        speech_publisher.publish(SpeechTranscript(
            utterance_id=utterance_id, text=text,
        ))
        return utterance_id

    def say(text, utterance_id=None):
        previous = len(replies)
        utterance_id = send(text, utterance_id)
        spin_until(lambda: len(replies) > previous)
        return utterance_id, replies[-1]

    run.start, run.send, run.say = start, send, say

    def mission(capability_id, arguments):
        request_id = 'weather-query:test:' + str(uuid4())
        run.agent.missions.submit(capability_id, arguments, request_id=request_id)
        spin_until(lambda: run.agent.missions.snapshot(request_id)['terminal'])
        return run.agent.missions.snapshot(request_id)

    run.mission = mission
    try:
        yield run
    finally:
        if run.weather is not None:
            run.weather.close()
        client.release.set()
        if run.agent is not None:
            executor.remove_node(run.agent)
            run.agent.destroy_node()
        if run.manager is not None:
            run.manager.begin_shutdown()
            run.manager.force_shutdown()
        background.shutdown(timeout_sec=3.0)
        if thread.ident is not None:
            thread.join(timeout=3.0)
        for node in reversed(nodes):
            node.destroy_node()
        executor.remove_node(sender)
        sender.destroy_node()
        executor.shutdown(timeout_sec=2.0)
        if rclpy.ok():
            rclpy.shutdown()
        location_store.close()


@pytest.fixture
def stored_weather(ros_weather, monkeypatch):
    """Exercise persisted coordinates while isolating external HTTP."""
    run = ros_weather
    run.location = {
        'location': '수원시 우만동', 'latitude': 37.28535, 'longitude': 127.02941,
        'timezone': 'Asia/Seoul',
    }
    run.client.state = weather_state(**run.location)
    run.weather_options = []
    run.location_queries = []
    run.candidates = [dict(run.location)]

    def resolve(query):
        run.location_queries.append(query)
        return run.candidates

    def make_client(**values):
        run.weather_options.append(values)
        run.client.state = weather_state(**values)
        return run.client

    run.resolve_location = resolve
    monkeypatch.setattr(weather_action, 'KmaWeatherClient', make_client)
    return run


def terminal_event(run):
    events = [event for event in run.events if event.get('terminal')]
    assert len(events) == 1
    return events[0]


def test_startup_and_general_dialogue_never_fetch_weather(ros_weather):
    """Idle nodes and a general reply must not invoke the weather API seam."""
    run = ros_weather
    run.start()
    _, reply = run.say('안녕')
    assert reply == '안녕하세요.'
    assert len(run.provider.calls) == 1
    assert run.provider.calls[0]['weather'] is None
    assert run.client.calls == 0
    assert run.goals == []


def test_missing_location_reaches_agent_through_manager_without_weather_http(stored_weather):
    """An empty database yields a location question, with no IP or weather lookup."""
    run = stored_weather
    run.start(auto_location=True)
    assert run.location_queries == run.weather_options == []
    _, greeting = run.say('안녕')
    assert greeting == '안녕하세요.'
    _, reply = run.say('오늘 날씨가 어때?')
    assert run.location_queries == run.weather_options == []
    assert run.client.calls == 0
    assert run.goals == [('get_weather', {})]
    event = terminal_event(run)
    assert event['kind'] == 'failed'
    assert event['ros_status'] == GoalStatus.STATUS_ABORTED
    assert yaml.safe_load(event['result_yaml'])['error_code'] == 'LOCATION_REQUIRED'
    assert run.provider.calls[-1]['weather'] == {'status': 'location_required'}
    assert reply == '어느 지역 날씨를 알려드릴까요?'


def test_saved_location_reaches_agent_through_manager_and_typed_weather_action(stored_weather):
    """Stored coordinates survive the empty weather Goal and typed Action result."""
    run = stored_weather
    run.location_store.set(run.location)
    run.start(auto_location=True)
    assert run.weather.get_parameter('weather_location_path').value == run.location_db_path
    _, reply = run.say('오늘 날씨가 어때?')
    assert run.location_queries == []
    assert run.weather_options == [run.location]
    assert run.client.calls == 1
    assert run.goals == [('get_weather', {})]
    event = terminal_event(run)
    assert event['kind'] == 'succeeded'
    assert event['ros_status'] == GoalStatus.STATUS_SUCCEEDED
    result = yaml.safe_load(event['result_yaml'])
    assert result['error_code'] == ''
    context = run.provider.calls[-1]['weather']
    assert context['status'] == 'fresh'
    for name, value in run.location.items():
        assert result['weather'][name] == context[name] == value
    assert reply == '수원시 우만동 현재 기온은 23.5도예요.'


def test_manager_location_setting_and_correction_change_next_weather_goal(stored_weather):
    """Both persisted writes and weather reads traverse the real Manager."""
    run = stored_weather
    run.start(auto_location=True)
    corrected = {**run.location, 'location': '용인시 동백동',
                 'latitude': 37.277, 'longitude': 127.151}
    for expected in (run.location, corrected):
        run.candidates = [dict(expected)]
        event = run.mission('set_weather_location', {
            'arguments_yaml': yaml.safe_dump({'location': expected['location']},
                                             allow_unicode=True),
        })
        assert event['kind'] == 'succeeded'
        downstream = yaml.safe_load(event['result_yaml'])
        assert yaml.safe_load(downstream['result_yaml']) == {
            'status': 'location_set', 'location': expected['location'],
        }
        assert run.location_store.get() == expected
        _, reply = run.say('오늘 날씨가 어때?')
        assert run.weather_options[-1] == expected
        assert expected['location'] in reply
    assert run.location_queries == ['수원시 우만동', '용인시 동백동']
    assert [name for name, _ in run.goals] == [
        'set_weather_location', 'get_weather', 'set_weather_location', 'get_weather',
    ]
    assert run.client.calls == 2


def test_agent_location_tool_stores_through_real_manager(stored_weather):
    """A model location Tool choice reaches the dedicated downstream Action."""
    run = stored_weather
    run.start(auto_location=True)
    _, reply = run.say('여기는 수원시 우만동')
    assert run.goals[0][0] == 'set_weather_location'
    assert run.location_queries == ['수원시 우만동']
    assert run.location_store.get() == run.location
    assert '수원시 우만동' in reply
    assert run.client.calls == 0


def test_manager_ambiguous_location_preserves_prior_configuration(stored_weather):
    """Ambiguous search reports candidates without changing the saved row."""
    run = stored_weather
    run.location_store.set(run.location)
    run.candidates = [{**run.location, 'location': '서울특별시 중구'},
                      {**run.location, 'location': '부산광역시 중구'}]
    run.start(auto_location=True)
    event = run.mission('set_weather_location', {'arguments_yaml': 'location: 중구'})
    assert event['kind'] == 'succeeded'
    nested = yaml.safe_load(yaml.safe_load(event['result_yaml'])['result_yaml'])
    assert nested == {'status': 'location_ambiguous',
                      'candidates': ['서울특별시 중구', '부산광역시 중구']}
    assert run.location_store.get() == run.location
    assert run.client.calls == 0


@pytest.mark.parametrize('interruption', ['cancel', 'timeout'])
def test_interrupted_location_setting_cannot_save_late_result(stored_weather, interruption):
    """The Manager's terminal failure cannot be followed by a late database write."""
    run = stored_weather
    run.location_store.set(run.location)
    started = Event()

    def resolve(query):
        started.set()
        assert run.client.release.wait(10.0)
        return [{**run.location, 'location': '용인시 동백동', 'latitude': 37.277}]

    run.resolve_location = resolve
    run.start(auto_location=True, timeout_s=0.2 if interruption == 'timeout' else 5.0)
    run.send('여기는 용인시 동백동')
    run.spin_until(started.is_set)
    if interruption == 'cancel':
        run.agent.missions.cancel(next(iter(run.agent.missions._requests)))
    run.spin_until(lambda: len(run.replies) == 1)
    event = terminal_event(run)
    assert event['kind'] == ('canceled' if interruption == 'cancel' else 'failed')
    assert run.location_store.get() == run.location
    run.client.release.set()
    run.weather._worker.join(timeout=1.0)
    assert not run.weather._worker.is_alive()
    assert run.location_store.get() == run.location
    assert run.client.calls == 0


def test_weather_fetch_runs_through_manager_and_typed_action_result(
    ros_weather,
):
    """One model Tool choice yields one Manager Goal and one API fetch."""
    run = ros_weather
    run.start()
    original = '  오늘 날씨가 어때?\n'
    utterance_id, reply = run.say(original)
    assert run.goals == [('get_weather', {})]
    assert run.client.calls == 1
    assert len(run.provider.calls) == 2
    assert run.provider.calls[0]['weather'] is None
    context = run.provider.calls[1]['weather']
    assert context['current']['temperature_c'] == 23.5
    assert context['source'] == '기상청 초단기실황·단기예보'
    assert [day['date'] for day in context['daily']] == [
        day.date for day in run.client.state.daily
    ]
    assert context['daily'][0]['temperature_max_c'] == 27.0
    assert context['daily'][1]['precipitation_probability_max_pct'] == 70.0
    event = terminal_event(run)
    assert event['kind'] == 'succeeded'
    assert event['ros_status'] == GoalStatus.STATUS_SUCCEEDED
    result = yaml.safe_load(event['result_yaml'])
    assert result['error_code'] == ''
    assert result['weather']['temperature_c'] == 23.5
    assert result['weather']['daily'][1]['date'] == context['daily'][1]['date']
    assert 'context_json' not in result
    assert reply == '시험 지역 현재 기온은 23.5도예요.'
    assert run.receipts == [(utterance_id, original, 'received')]
    assert run.provider.calls[0]['request'].utterance == original.strip()


def test_new_speech_fetches_again_but_duplicate_id_does_not(ros_weather):
    """A duplicate speech ID cannot repeat the Tool, Manager Goal or fetch."""
    run = ros_weather
    run.start()
    original = '지금 날씨 알려 줘.'
    utterance_id, _ = run.say(original)
    run.client.state = weather_state(19.0)
    run.send(original, utterance_id)
    duplicate = (utterance_id, original, 'duplicate')
    run.spin_until(lambda: duplicate in run.receipts)
    assert len(run.goals) == len(run.replies) == run.client.calls == 1
    assert len(run.provider.calls) == 2
    next_id, reply = run.say(original)
    assert next_id != utterance_id
    assert len(run.goals) == len(run.replies) == run.client.calls == 2
    assert len(run.provider.calls) == 4
    assert reply == '시험 지역 현재 기온은 19.0도예요.'


@pytest.mark.parametrize('failure,expected', [
    ('fetch_error', 'FETCH_FAILED'), ('timeout', 'TIMEOUT'),
    ('KMA_KEY_REQUIRED', 'KMA_KEY_REQUIRED'), ('KMA_AUTH_FAILED', 'KMA_AUTH_FAILED'),
])
def test_fetch_failure_and_timeout_reach_manager_as_aborted(
    ros_weather, failure, expected,
):
    """Failed Actions carry errors; Agent cannot use weather numbers."""
    run = ros_weather
    run.client.error = (KmaWeatherError(failure) if failure.startswith('KMA_')
                        else failure == 'fetch_error')
    run.client.block = failure == 'timeout'
    run.start(timeout_s=0.2 if failure == 'timeout' else 5.0)
    _, reply = run.say('오늘 날씨가 어때?')
    assert run.client.calls == 1
    assert run.goals == [('get_weather', {})]
    event = terminal_event(run)
    assert event['kind'] == 'failed'
    assert event['ros_status'] == GoalStatus.STATUS_ABORTED
    result = yaml.safe_load(event['result_yaml'])
    assert result['error_code'] == expected
    if failure == 'timeout':
        assert not run.client.finished.is_set()
    assert len(run.provider.calls) == 2
    context = run.provider.calls[-1]['weather']
    assert context['status'] == 'unavailable' and 'current' not in context
    assert '23.5' not in reply
    assert PRIVATE_ERROR not in reply + yaml.safe_dump(run.events)


def test_cancel_propagates_through_manager_and_discards_late_fetch(
    ros_weather,
):
    """Cancel the same public mission while downstream weather is fetching."""
    run = ros_weather
    run.client.block = True
    run.start()
    run.send('오늘 날씨가 어때?')
    run.spin_until(run.client.started.is_set)
    request_id = next(iter(run.agent.missions._requests))
    run.agent.missions.cancel(request_id)
    run.spin_until(lambda: len(run.replies) == 1)
    event = terminal_event(run)
    assert event['kind'] == 'canceled'
    assert event['ros_status'] == GoalStatus.STATUS_CANCELED
    assert yaml.safe_load(event['result_yaml'])['error_code'] == 'CANCELED'
    assert run.client.calls == 1
    assert run.provider.calls[-1]['weather']['status'] == 'unavailable'
    run.client.release.set()
    run.spin_until(run.client.finished.is_set)
    run.weather._worker.join(timeout=1.0)
    assert not run.weather._worker.is_alive()
    assert len(run.provider.calls) == 2 and len(run.replies) == 1
    assert '23.5' not in run.replies[0]


@pytest.mark.parametrize('missing', ['manager', 'weather_server'])
def test_missing_manager_or_weather_action_never_falls_back(
    ros_weather, missing,
):
    """Neither missing Action endpoint permits an Agent-side API fallback."""
    run = ros_weather
    run.start(with_manager=missing != 'manager',
              with_weather=missing != 'weather_server')
    _, reply = run.say('오늘 날씨가 어때?')
    assert len(run.goals) == (0 if missing == 'manager' else 1)
    assert run.client.calls == 0
    assert len(run.provider.calls) == 2
    assert run.provider.calls[0]['weather'] is None
    context = run.provider.calls[1]['weather']
    assert context['status'] == 'unavailable' and 'current' not in context
    assert reply and '23.5' not in reply
