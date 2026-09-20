"""Offline checks for the ROS adapter's worker and executor boundary."""

import sys
from threading import Thread, get_ident
from types import ModuleType, SimpleNamespace

import pytest

from malbut_tts import node as tts_node


class FakeRuntime:
    """Accept commands separately from actual playback state changes."""

    def __init__(self, on_status):
        self.on_status = on_status
        self.submitted = []
        self.controls = []
        self.closed = False

    def submit(self, text, request_type=0):
        self.submitted.append((text, request_type))
        return 'playback-1'

    def control(self, playback_id, command):
        self.controls.append((playback_id, command))
        return playback_id == 'playback-1' and command == 'pause'

    def close(self):
        self.closed = True


@pytest.fixture
def fake_ros(monkeypatch):
    """Provide ROS entity bookkeeping without importing optional backends."""
    entities = SimpleNamespace(
        subscriptions=[], services=[], timers=[], publishers=[],
        published=[], destruction=[], parameters={}, logs=[],
    )

    class FakeNode:
        def __init__(self, name):
            self.name = name
            self.parameters = dict(entities.parameters)

        def declare_parameter(self, name, default):
            self.parameters.setdefault(name, default)

        def get_parameter(self, name):
            return SimpleNamespace(value=self.parameters[name])

        def get_logger(self):
            return SimpleNamespace(
                info=lambda message: entities.logs.append(message),
            )

        def create_subscription(self, message_type, topic, callback, qos):
            entities.subscriptions.append((message_type, topic, callback, qos))

        def create_publisher(self, message_type, topic, qos):
            entities.publishers.append((message_type, topic, qos))
            return SimpleNamespace(publish=lambda message:
                                   entities.published.append(
                                       (get_ident(), message)))

        def create_service(self, service_type, name, callback):
            entities.services.append((service_type, name, callback))

        def create_timer(self, interval, callback):
            entities.timers.append((interval, callback))

        def destroy_node(self):
            entities.destruction.append('node')
            return True

    monkeypatch.setitem(sys.modules, 'rclpy', ModuleType('rclpy'))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(
        Node=FakeNode,
    ))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        DurabilityPolicy=SimpleNamespace(VOLATILE='volatile'),
        HistoryPolicy=SimpleNamespace(KEEP_LAST='keep_last'),
        ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
        QoSProfile=lambda **kwargs: kwargs,
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces',
                        ModuleType('malbut_interfaces'))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechRequest=type('SpeechRequest', (), {}),
        SpeechPlaybackStatus=lambda **kwargs: SimpleNamespace(**kwargs),
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', SimpleNamespace(
        ControlSpeechPlayback=type('ControlSpeechPlayback', (), {}),
    ))
    return entities


def test_request_kind_and_verbatim_text_reach_runtime(fake_ros):
    """Dialogue and notification input use the agreed reliable topic."""
    node = tts_node.create_tts_node(FakeRuntime)
    _, topic, callback, qos = fake_ros.subscriptions[0]
    assert topic == tts_node.RESPONSE_TOPIC
    assert qos == {
        'history': 'keep_last', 'depth': 10,
        'reliability': 'reliable', 'durability': 'volatile',
    }
    assert fake_ros.publishers[0][1:] == (tts_node.STATUS_TOPIC, qos)
    callback(SimpleNamespace(text=' 원문\n', request_type=0))
    callback(SimpleNamespace(text='순찰 완료', request_type=1))
    assert node._runtime.submitted == [(' 원문\n', 0), ('순찰 완료', 1)]
    node.destroy_node()


def test_worker_states_are_published_in_order_only_by_executor(fake_ros):
    """Audio threads enqueue actual states without publishing ROS messages."""
    node = tts_node.create_tts_node(FakeRuntime)
    worker = Thread(target=lambda: [
        node._runtime.on_status('playback-1', state)
        for state in ('playing', 'paused', 'playing', 'finished')
    ])
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert fake_ros.published == []
    fake_ros.timers[0][1]()
    assert [(message.playback_id, message.state)
            for _, message in fake_ros.published] == [
        ('playback-1', state)
        for state in ('playing', 'paused', 'playing', 'finished')
    ]
    assert {thread_id for thread_id, _ in fake_ros.published} == {get_ident()}
    fake_ros.timers[0][1]()
    assert len(fake_ros.published) == 4
    node.destroy_node()


def test_control_acceptance_does_not_fabricate_a_state(fake_ros):
    """Accepted service responses await a separate actual runtime event."""
    node = tts_node.create_tts_node(FakeRuntime)
    _, name, control = fake_ros.services[0]
    assert name == tts_node.CONTROL_SERVICE
    assert control(
        SimpleNamespace(playback_id='playback-1', command='pause'),
        SimpleNamespace(accepted=False),
    ).accepted
    assert not control(
        SimpleNamespace(playback_id='missing', command='pause'),
        SimpleNamespace(accepted=False),
    ).accepted
    fake_ros.timers[0][1]()
    assert fake_ros.published == []
    node.destroy_node()


def test_destruction_closes_audio_before_ros_and_ignores_late_events(fake_ros):
    """Shutdown closes audio before ROS and rejects late worker events."""
    node = tts_node.create_tts_node(FakeRuntime)
    runtime = node._runtime

    def close():
        fake_ros.destruction.append('runtime')
        runtime.on_status('playback-1', 'stopped')

    runtime.close = close
    assert node.destroy_node()
    assert not node.destroy_node()
    assert fake_ros.destruction == ['runtime', 'node']
    fake_ros.subscriptions[0][2](SimpleNamespace(text='늦은 말', request_type=0))
    fake_ros.timers[0][1]()
    assert runtime.submitted == []
    assert fake_ros.published == []


def test_explicit_local_startup_failure_releases_node(fake_ros):
    """An absent local model path yields guidance and cleans up ROS."""
    fake_ros.parameters.update(backend='qwen-cuda')
    with pytest.raises(ValueError, match='model_path:=/absolute/model/path'):
        tts_node.create_tts_node()
    assert fake_ros.destruction == ['node']


def test_unknown_backend_is_rejected_before_loading(fake_ros):
    fake_ros.parameters.update(backend='legacy', model_path='/legacy/model')
    with pytest.raises(ValueError, match='openai or qwen-cuda'):
        tts_node.create_tts_node()
    assert fake_ros.destruction == ['node']


def test_cuda_node_uses_explicit_backend_without_changing_ros_contract(
    fake_ros, monkeypatch,
):
    """Backend selection does not replace the queue, topics, or player."""
    fake_ros.parameters.update(
        model_path='/local/cuda-model', backend='qwen-cuda',
        cuda_dtype='float16', output_device=3,
        max_pending_requests=8, pending_timeout_s=12.0,
    )
    received = {}

    def synthesizer(path, **kwargs):
        received.update(path=path, **kwargs)
        return object()

    def runtime(synth, player_factory, on_status, logger=None, **kwargs):
        received['device'] = player_factory(None, None)
        received.update(kwargs)
        return FakeRuntime(on_status)

    monkeypatch.setattr('malbut_tts.backends.create_synthesizer', synthesizer)
    monkeypatch.setitem(sys.modules, 'malbut_tts.audio', SimpleNamespace(
        StreamingPlayer=lambda *args, **kwargs: kwargs['device'],
    ))
    monkeypatch.setitem(sys.modules, 'malbut_tts.runtime', SimpleNamespace(
        SpeechRuntime=runtime,
    ))
    node = tts_node.create_tts_node()
    assert received == dict(
        path='/local/cuda-model', backend='qwen-cuda', cuda_dtype='float16',
        cuda_sentence_mode=True, sentence_max_chars=80,
        api_model='gpt-4o-mini-tts', api_voice='marin', api_timeout_seconds=8.0,
        speaker='Sohee', language='Korean', device=3,
        max_pending_requests=8, pending_timeout_s=12.0,
    )
    assert fake_ros.subscriptions[0][1] == tts_node.RESPONSE_TOPIC
    assert fake_ros.services[0][1] == tts_node.CONTROL_SERVICE
    node.destroy_node()


@pytest.mark.parametrize('options,model,voice,timeout', [
    ({}, 'gpt-4o-mini-tts', 'marin', 8.0),
    ({'api_model': 'tts-1', 'api_voice': 'alloy', 'api_timeout_seconds': 4.5},
     'tts-1', 'alloy', 4.5),
])
def test_default_openai_node_needs_no_model_and_reuses_ros_contract(
    fake_ros, monkeypatch, options, model, voice, timeout,
):
    fake_ros.parameters.update(**options)
    received = {}

    def synthesizer(**kwargs):
        received['options'] = kwargs
        return 'api-synthesizer'

    def runtime(synth, player_factory, on_status, logger=None, **kwargs):
        received['synth'] = synth
        received['player'] = player_factory(None, None)
        received.update(kwargs)
        return FakeRuntime(on_status)

    monkeypatch.setitem(sys.modules, 'malbut_tts.api_synthesis', SimpleNamespace(
        OpenAISynthesizer=synthesizer,
    ))
    monkeypatch.setitem(sys.modules, 'malbut_tts.cuda_synthesis', None)
    monkeypatch.setitem(sys.modules, 'malbut_tts.sentence_synthesis', None)
    monkeypatch.setitem(sys.modules, 'malbut_tts.audio', SimpleNamespace(
        StreamingPlayer=lambda *args, **kwargs: kwargs['device'],
    ))
    monkeypatch.setitem(sys.modules, 'malbut_tts.runtime', SimpleNamespace(
        SpeechRuntime=runtime,
    ))
    node = tts_node.create_tts_node()
    assert received == {
        'options': {'model': model, 'voice': voice, 'timeout_seconds': timeout},
        'synth': 'api-synthesizer', 'player': None,
        'max_pending_requests': 32, 'pending_timeout_s': 0.0,
    }
    assert node.parameters['model_path'] == ''
    assert node.parameters['backend'] == 'openai'
    assert 'api_key' not in node.parameters
    assert any('paid external API' in message for message in fake_ros.logs)
    assert any('AI-generated' in message for message in fake_ros.logs)
    assert fake_ros.subscriptions[0][1] == tts_node.RESPONSE_TOPIC
    assert fake_ros.services[0][1] == tts_node.CONTROL_SERVICE
    node.destroy_node()


def test_help_and_missing_ros_need_no_model(monkeypatch, capsys):
    """Help and missing dependency diagnostics work before backend loading."""
    monkeypatch.setitem(sys.modules, 'rclpy', None)
    with pytest.raises(SystemExit) as exit_info:
        tts_node.main(['--help'])
    assert exit_info.value.code == 0
    assert tts_node.main([]) == 2
    output = capsys.readouterr()
    assert 'model_path' in output.out
    assert 'ROS 2 rclpy is required' in output.err


@pytest.mark.parametrize('failure', [None, ImportError, ValueError])
def test_main_closes_ros_after_interruption_or_backend_failure(
    monkeypatch, capsys, failure,
):
    """Model configuration failures and normal interruption clean up ROS."""
    calls = []
    ros = ModuleType('rclpy')
    ros.init = lambda args: calls.append(('init', args))
    ros.ok = lambda: True
    ros.shutdown = lambda: calls.append('shutdown')

    def spin(_node):
        calls.append('spin')
        raise KeyboardInterrupt

    def create():
        if failure:
            raise failure('CUDA backend unavailable')
        return SimpleNamespace(
            get_logger=lambda: SimpleNamespace(info=lambda _: None),
            destroy_node=lambda: calls.append('destroy'),
        )

    ros.spin = spin
    monkeypatch.setitem(sys.modules, 'rclpy', ros)
    monkeypatch.setitem(sys.modules, 'rclpy.executors', SimpleNamespace(
        ExternalShutdownException=type('Shutdown', (Exception,), {}),
    ))
    monkeypatch.setattr(tts_node, 'create_tts_node', create)
    code = tts_node.main([
        '--ros-args', '-p', 'backend:=qwen-cuda', '-p', 'model_path:=/model',
    ])
    assert calls[0] == (
        'init',
        ['--ros-args', '-p', 'backend:=qwen-cuda', '-p', 'model_path:=/model'],
    )
    assert calls[-1] == 'shutdown'
    if failure:
        assert code == 2
        assert 'CUDA backend unavailable' in capsys.readouterr().err
    else:
        assert code == 0
        assert calls[1:3] == ['spin', 'destroy']
