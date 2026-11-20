"""Tests for content-free per-turn latency waterfall observability."""

import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.http_server import make_server
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.base import AgentProvider, ProviderError
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderAPITiming,
    ProviderResult,
    ProviderUsage,
    RobotState,
)
from malbut_agent_server.speech_dialogue import DialogueWorker
from malbut_agent_server.turn_metrics import (
    TurnWaterfallRecord,
    TurnWaterfallRecorder,
    format_waterfall_table,
    parse_waterfall_logs,
)


SECRET_UTTERANCE = '민감한 개인 발화 비밀번호 987654'
SECRET_REPLY = '절대 로그에 남으면 안 되는 개인 답변 123456'


class _TimedProvider(AgentProvider):
    """Deterministic provider returning explicit API timing and cache usage."""

    name = 'timed-test'

    def __init__(self, *, reply=SECRET_REPLY, fail=False, weather_tool=False):
        self.reply = reply
        self.fail = fail
        self.weather_tool = weather_tool
        self.calls = 0

    def complete(
        self,
        request,
        memories,
        conversation_turns,
        tools,
        conversation_summary=None,
        *,
        memory_context=None,
        weather_context=None,
    ):
        del request, memories, conversation_turns, tools, conversation_summary, memory_context
        self.calls += 1
        if self.fail:
            raise ProviderError('simulated failure')
        time.sleep(0.005)
        if self.weather_tool and weather_context is None:
            decision = AgentDecision(
                type='tool_call',
                message='날씨를 확인해 볼게요.',
                tool_name='get_weather',
                arguments={},
                reason='weather_request',
                confidence=0.95,
            )
            return ProviderResult(
                decision=decision,
                provider=self.name,
                model='gpt-5-test',
                latency_ms=12.5,
                usage=ProviderUsage(
                    input_tokens=1000,
                    output_tokens=20,
                    total_tokens=1020,
                    cached_input_tokens=800,
                ),
                api_timing=ProviderAPITiming(
                    preparation_ms=1.2,
                    transport_ms=12.5,
                    parse_ms=0.3,
                    headers_ms=4.5,
                    first_event_ms=5.0,
                    first_output_delta_ms=7.8,
                    completed_event_ms=12.4,
                    attempt_count=1,
                ),
                input_chars=420,
            )
        return ProviderResult(
            decision=AgentDecision(
                type='message',
                message=self.reply,
                reason='answer',
                confidence=0.98,
            ),
            provider=self.name,
            model='gpt-5-test',
            latency_ms=15.0,
            usage=ProviderUsage(
                input_tokens=2000,
                output_tokens=40,
                total_tokens=2040,
                cached_input_tokens=1500,
                reasoning_output_tokens=0,
            ),
            api_timing=ProviderAPITiming(
                preparation_ms=1.4,
                transport_ms=15.0,
                parse_ms=0.2,
                headers_ms=5.1,
                first_event_ms=5.6,
                first_output_delta_ms=9.2,
                completed_event_ms=14.9,
                attempt_count=1,
            ),
            input_chars=640,
        )


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condition did not become true before timeout')


def test_ring_buffer_eviction_percentiles_and_cache_hit_rate():
    """Recorder bounds memory, preserves newest-first order, and computes p50/p95."""
    recorder = TurnWaterfallRecorder(capacity=3)
    totals = [100.0, 200.0, 300.0, 400.0]
    for idx, total_ms in enumerate(totals):
        recorder.record(
            TurnWaterfallRecord(
                recorded_at=1700000000.0 + idx,
                channel='speech',
                outcome='ok' if idx < 3 else 'error',
                request_id=f'req-{idx}',
                stages_ms={'provider_ms': total_ms * 0.8, 'total_ms': total_ms},
                usage={
                    'input_tokens': 1000,
                    'cached_input_tokens': 500 if idx % 2 == 0 else 1000,
                    'cache_hit_ratio': 0.5 if idx % 2 == 0 else 1.0,
                },
            )
        )

    snap = recorder.snapshot(limit=10)
    assert snap['capacity'] == 3
    assert snap['recorded_total'] == 4
    assert snap['buffered'] == 3
    assert [turn['request_id'] for turn in snap['turns']] == ['req-3', 'req-2', 'req-1']
    # Window contains req-1 (200), req-2 (300), req-3 (400).
    total_stats = snap['summary']['stages_ms']['total_ms']
    assert total_stats['count'] == 3
    assert total_stats['p50'] == 300.0
    assert total_stats['p95'] == 390.0
    assert total_stats['max'] == 400.0
    assert total_stats['avg'] == 300.0
    # Cached tokens across req-1 (1000), req-2 (500), req-3 (1000) = 2500 / 3000.
    assert snap['summary']['cache_hit_rate'] == round(2500 / 3000, 4)
    assert snap['summary']['outcomes'] == {'error': 1, 'ok': 2}


def test_orchestrator_records_stages_cache_replay_followup_and_errors():
    """Orchestrator captures per-stage waterfall for normal, cached, weather, and error turns."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    provider = _TimedProvider(weather_tool=True)
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        weather_executor=lambda _req_id: {
            'status': 'ok',
            'location': '서울',
            'temperature_c': 18,
            'condition': '맑음',
        },
    )
    try:
        session = conversation_store.create('user-1')
        request = AgentRequest(
            request_id='req-weather-1',
            user_id='user-1',
            conversation_id=session.conversation_id,
            turn_id='turn-1',
            utterance=SECRET_UTTERANCE,
            robot_state=RobotState(),
            available_tools=('get_weather',),
        )
        orchestrator.handle(request)
        # Replaying the same request_id exercises the cached response path.
        orchestrator.handle(request)

        # Trigger a provider failure on a new turn.
        provider.fail = True
        with pytest.raises(ProviderError):
            orchestrator.handle(
                AgentRequest(
                    request_id='req-fail-2',
                    user_id='user-1',
                    conversation_id=session.conversation_id,
                    turn_id='turn-2',
                    utterance='또 다른 민감 발화',
                    robot_state=RobotState(),
                    available_tools=(),
                )
            )

        snap = orchestrator.turn_metrics.snapshot(limit=10)
        assert snap['recorded_total'] == 3
        serialized = json.dumps(snap, ensure_ascii=False)
        assert SECRET_UTTERANCE not in serialized
        assert SECRET_REPLY not in serialized
        assert 'user-1' not in serialized

        err_turn, cached_turn, weather_turn = snap['turns']
        assert err_turn['outcome'] == 'error'
        assert 'begin_turn_ms' in err_turn['stages_ms']
        assert 'total_ms' in err_turn['stages_ms']

        assert cached_turn['outcome'] == 'cached'
        assert cached_turn['flags'].get('cached_response') is True

        assert weather_turn['outcome'] == 'ok'
        assert weather_turn['tool_name'] == 'get_weather'
        for required_stage in (
            'begin_turn_ms',
            'memory_snapshot_ms',
            'provider_prep_ms',
            'headers_ms',
            'ttft_ms',
            'provider_ms',
            'safety_ms',
            'tool_followup_ms',
            'commit_ms',
            'total_ms',
        ):
            assert required_stage in weather_turn['stages_ms'], required_stage
            assert weather_turn['stages_ms'][required_stage] >= 0.0
        assert weather_turn['stages_ms']['ttft_ms'] == 7.8
        assert weather_turn['usage']['cache_hit_ratio'] == 0.8
    finally:
        conversation_store.close()
        memory_store.close()


@contextmanager
def _running_http_server(auth_token='secret-token', provider=None):
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=provider or _TimedProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
    )
    server = make_server(
        '127.0.0.1',
        0,
        orchestrator,
        auth_token=auth_token,
        allowed_user_id='http-user',
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f'http://{host}:{port}', orchestrator
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        conversation_store.close()
        memory_store.close()


def _http_request(method, url, *, payload=None, token=''):
    headers = {}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), json.loads(err.read())


def test_http_observability_endpoint_auth_validation_and_content_free():
    """GET /v1/observability/turns enforces auth, validates query, and hides content."""
    with _running_http_server(auth_token='obs-token') as (base_url, _orch):
        status, _, err = _http_request('GET', f'{base_url}/v1/observability/turns')
        assert status == 401
        assert err['error']['code'] == 'unauthorized'

        for bad_query in ('?limit=0', '?limit=abc', '?limit=5000', '?unknown=1'):
            status, _, err = _http_request(
                'GET',
                f'{base_url}/v1/observability/turns{bad_query}',
                token='obs-token',
            )
            assert status == 400
            assert err['error']['code'] == 'validation_error'

        status, _, created = _http_request(
            'POST',
            f'{base_url}/v1/conversations',
            payload={'user_id': 'http-user'},
            token='obs-token',
        )
        assert status == 201
        cid = created['conversation']['conversation_id']

        status, _, _ = _http_request(
            'POST',
            f'{base_url}/v1/agent/respond',
            payload={
                'request_id': 'http-req-1',
                'user_id': 'http-user',
                'conversation_id': cid,
                'turn_id': 'http-turn-1',
                'utterance': SECRET_UTTERANCE,
            },
            token='obs-token',
        )
        assert status == 200

        status, headers, snap = _http_request(
            'GET',
            f'{base_url}/v1/observability/turns?limit=5',
            token='obs-token',
        )
        assert status == 200
        assert headers.get('Cache-Control') == 'no-store'
        assert snap['recorded_total'] == 1
        turn = snap['turns'][0]
        assert turn['channel'] == 'http'
        assert turn['outcome'] == 'ok'
        assert turn['request_id'] == 'http-req-1'
        assert turn['stages_ms']['ttft_ms'] == 9.2
        raw = json.dumps(snap, ensure_ascii=False)
        assert SECRET_UTTERANCE not in raw
        assert SECRET_REPLY not in raw
        assert 'http-user' not in raw


@pytest.mark.parametrize('publish_before_save', [False, True])
def test_speech_dialogue_worker_records_end_to_end_waterfall(tmp_path, publish_before_save):
    """DialogueWorker captures queue, scene, provider, commit, and TTS handoff times."""
    db_path = str(tmp_path / 'speech.sqlite3')

    def runtime_factory():
        return AgentOrchestrator(
            provider=_TimedProvider(),
            memory_store=SQLiteMemoryStore(db_path),
            conversation_store=SQLiteConversationStore(db_path),
            safety_policy=SafetyPolicy(),
        )

    class _SceneSource:
        def prepare_text_turn(self):
            time.sleep(0.004)
            return {'image': {'available': False}, 'map': None, 'pose': None}, None

    worker = DialogueWorker(
        runtime_factory,
        'speech-user',
        scene_source=_SceneSource(),
        publish_before_save=publish_before_save,
    )
    try:
        _wait_until(lambda: worker.ready)
        assert worker.submit('utt-1', SECRET_UTTERANCE)

        published = []
        waterfalls = []

        def drain_and_publish():
            for reply in worker.drain(validate=False):
                out = worker.publish_reply(reply, lambda text: (published.append(text), True)[1])
                if out is not None:
                    waterfalls.append(getattr(reply, '_waterfall', None))

        _wait_until(lambda: (drain_and_publish(), len(published) == 1)[1])
        # Wait for the worker to finish commit_turn() after publish_before_save.
        _wait_until(lambda: not worker.has_pending and worker.metrics_snapshot()['recorded_total'] == 1)

        snap = worker.metrics_snapshot(limit=5)
        assert snap['recorded_total'] == 1
        turn = snap['turns'][0]
        assert turn['channel'] == 'speech'
        assert turn['outcome'] == 'ok'
        assert turn['utterance_id'] == 'utt-1'
        assert turn['flags']['delivery_before_save'] is publish_before_save if publish_before_save else True
        assert turn['flags']['image_captured'] is False
        for stage in (
            'queue_wait_ms',
            'scene_prepare_ms',
            'begin_turn_ms',
            'memory_snapshot_ms',
            'provider_ms',
            'ttft_ms',
            'safety_ms',
            'commit_ms',
            'tts_handoff_ms',
            'time_to_tts_ms',
            'total_ms',
        ):
            assert stage in turn['stages_ms'], f'missing {stage} (publish_before_save={publish_before_save})'
        assert waterfalls[0] is not None
        assert 'stages_ms' in waterfalls[0]

        serialized = json.dumps(snap, ensure_ascii=False)
        assert SECRET_UTTERANCE not in serialized
        assert SECRET_REPLY not in serialized
    finally:
        worker.close()


def test_speech_dialogue_records_cancelled_turn_on_suspend(tmp_path):
    """Suspended queued utterances finalize cleanly as cancelled without leaking."""
    db_path = str(tmp_path / 'cancel.sqlite3')

    def runtime_factory():
        return AgentOrchestrator(
            provider=MockProvider(),
            memory_store=SQLiteMemoryStore(db_path),
            conversation_store=SQLiteConversationStore(db_path),
            safety_policy=SafetyPolicy(),
        )

    worker = DialogueWorker(runtime_factory, 'speech-user')
    try:
        _wait_until(lambda: worker.ready)
        assert worker.submit('utt-ok', '안녕')
        _wait_until(lambda: bool(worker._results))
        worker.suspend()
        snap = worker.metrics_snapshot()
        assert snap['recorded_total'] == 1
        assert snap['turns'][0]['outcome'] == 'cancelled'
    finally:
        worker.close()


def test_snapshot_file_formatter_and_log_parser(tmp_path):
    """Atomic snapshot write uses 0600 permissions and CLI helpers parse logs/tables."""
    recorder = TurnWaterfallRecorder(capacity=10)
    rec = recorder.record(
        TurnWaterfallRecord(
            recorded_at=1700000100.0,
            channel='speech',
            outcome='ok',
            utterance_id='utt-log-1',
            model='gpt-5-mini',
            stages_ms={
                'queue_wait_ms': 1.5,
                'scene_prepare_ms': 3.2,
                'ttft_ms': 210.4,
                'provider_ms': 620.0,
                'commit_ms': 2.1,
                'time_to_tts_ms': 626.8,
                'total_ms': 628.0,
            },
            usage={
                'input_tokens': 1600,
                'cached_input_tokens': 1200,
                'cache_hit_ratio': 0.75,
            },
        )
    )

    out_path = tmp_path / 'nested' / 'turn_metrics.json'
    recorder.write_snapshot(str(out_path))
    assert out_path.is_file()
    assert stat.S_IMODE(os.stat(out_path).st_mode) == 0o600
    loaded = json.loads(out_path.read_text(encoding='utf-8'))
    assert loaded['recorded_total'] == 1

    table = format_waterfall_table(loaded)
    assert 'Turn Waterfall' in table
    assert 'prompt_cache_hit=75.0%' in table
    assert 'gpt-5-mini' in table

    log_line = '[INFO] [malbut_agent_communication]: ' + json.dumps({
        'event': 'dialogue_response_published',
        'utterance_id': 'utt-log-1',
        'kind': 'answer',
        'waterfall': rec.to_compact_dict(),
    }, ensure_ascii=False)
    parsed = parse_waterfall_logs(['unrelated line', log_line])
    assert parsed['recorded_total'] == 1
    assert parsed['summary']['cache_hit_rate'] == 0.75
    assert parsed['turns'][0]['stages_ms']['ttft_ms'] == 210.4


def test_settings_validates_turn_metrics_configuration():
    """Settings parses and validates turn metrics capacity and snapshot path."""
    settings = Settings.from_env({
        'MALBUT_AGENT_TURN_METRICS_CAPACITY': '250',
        'MALBUT_AGENT_TURN_METRICS_PATH': '/tmp/malbut-turns.json',
    })
    assert settings.turn_metrics_capacity == 250
    assert settings.turn_metrics_snapshot_path == '/tmp/malbut-turns.json'
    settings.validate_for_dialogue()

    bad = Settings(turn_metrics_capacity=0)
    with pytest.raises(ValueError, match='MALBUT_AGENT_TURN_METRICS_CAPACITY'):
        bad.validate_for_dialogue()


def test_ros_communication_emits_waterfall_and_optional_snapshot(tmp_path, monkeypatch):
    """CommunicationNode attaches content-free waterfall to published log and snapshot."""
    import sys
    from malbut_agent_server import ros_communication

    spoken, logs = [], []

    class _SpeechRequest(SimpleNamespace):
        DIALOGUE = 0
        NOTIFICATION = 1

    class _FakeNode:
        def __init__(self, _name):
            self.context = SimpleNamespace(ok=lambda: True)
            self.executor = None

        def declare_parameter(self, name, default, descriptor=None):
            del descriptor
            return SimpleNamespace(value=False if name == 'patrol_conversation_enabled' else default)

        def create_publisher(self, *_args):
            return SimpleNamespace(publish=lambda msg: spoken.append(msg.text))

        def create_subscription(self, *_args):
            return None

        def create_service(self, *_args, **_kwargs):
            return None

        def create_guard_condition(self, _cb):
            return SimpleNamespace(trigger=lambda: None)

        def create_timer(self, *_args):
            return None

        def get_logger(self):
            return SimpleNamespace(info=logs.append, error=logs.append, warning=logs.append)

        def destroy_node(self):
            return True

    monkeypatch.setitem(sys.modules, 'std_msgs.msg', SimpleNamespace(String=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=_FakeNode))
    monkeypatch.setitem(sys.modules, 'rclpy.callback_groups', SimpleNamespace(ReentrantCallbackGroup=object))
    monkeypatch.setitem(sys.modules, 'rcl_interfaces.msg', SimpleNamespace(ParameterDescriptor=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'rclpy.task', SimpleNamespace(Future=object))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        DurabilityPolicy=SimpleNamespace(VOLATILE='volatile', TRANSIENT_LOCAL='transient_local'),
        HistoryPolicy=SimpleNamespace(KEEP_LAST='keep-last'),
        ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
        QoSProfile=lambda **kwargs: kwargs,
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechRequest=_SpeechRequest,
        SpeechTranscript=SimpleNamespace,
        SpeechInputStatus=SimpleNamespace(
            STARTED='started', FAILED='failed', FILTERED='filtered', ENDED='ended',
        ),
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', SimpleNamespace(
        ClassifySpeechAddressee=SimpleNamespace(Response=SimpleNamespace(UNKNOWN='unknown')),
    ))
    monkeypatch.setattr(
        'malbut_agent_server.manager_client.ManagerClient',
        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        ros_communication, 'SituationActionServer',
        lambda *_args, **_kwargs: SimpleNamespace(active=False, close=lambda: None, destroy=lambda: None),
    )

    db_path = str(tmp_path / 'ros_dialogue.sqlite3')
    snap_path = tmp_path / 'turn_metrics.json'

    def factory():
        return AgentOrchestrator(
            provider=_TimedProvider(),
            memory_store=SQLiteMemoryStore(db_path),
            conversation_store=SQLiteConversationStore(db_path),
            safety_policy=SafetyPolicy(),
        )

    node = ros_communication.create_communication_node(
        speech_db_path=str(tmp_path / 'receipts.sqlite3'),
        dialogue_settings=Settings(
            user_id='ros-speaker',
            database_path=db_path,
            speech_publish_before_save=False,
            turn_metrics_snapshot_path=str(snap_path),
        ),
        dialogue_factory=factory,
    )
    try:
        _wait_until(lambda: node.dialogue.ready)
        assert node.dialogue.submit('ros-utt-1', SECRET_UTTERANCE)
        _wait_until(lambda: (node._drain_dialogue(), len(spoken) == 1)[1])

        published_events = [
            json.loads(line)
            for line in logs
            if line.startswith('{') and json.loads(line).get('event') == 'dialogue_response_published'
        ]
        assert len(published_events) == 1
        waterfall = published_events[0]['waterfall']
        assert waterfall['outcome'] == 'ok'
        assert waterfall['stages_ms']['ttft_ms'] == 9.2
        assert 'tts_handoff_ms' in waterfall['stages_ms']
        assert snap_path.is_file()
        snap_text = snap_path.read_text(encoding='utf-8')
        assert SECRET_UTTERANCE not in snap_text
        assert SECRET_REPLY not in snap_text
        assert node.turn_metrics.snapshot()['recorded_total'] == 1
    finally:
        node.destroy_node()
