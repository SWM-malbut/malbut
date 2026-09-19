"""Exercise ROS wiring with controlled runtime boundaries and no hardware or SDK."""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from malbut_stt.node import main


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """Provide ROS message/callback boundaries without starting a ROS context."""
    model = tmp_path / 'local-model'
    model.mkdir()
    state = SimpleNamespace(
        ok=False, calls={}, logs=[], published=[], statuses=[], closed=[], failure=None,
        cleanup_failure=False, native_cleanup_failure=False,
        shutdown_before_publish=False, callbacks={},
        parameters={'wake_model_path': str(model)},
        now=0.0, clients={}, on_spin=None, service_ready={}, service_failure={},
        response_failure={}, accepted=True, decision='addressed', decisions=[],
    )

    def fail(phase):
        if state.failure == phase:
            raise RuntimeError('hidden credential or audio content')

    def init(args):
        state.calls['ros_args'] = args
        state.ok = True

    def shutdown():
        state.ok = False
        state.closed.append('ros')

    def spin_once(node, timeout_sec):
        state.calls['spin_timeout'] = timeout_sec
        if state.on_spin is not None:
            state.on_spin()
            return
        state.callbacks['/malbut/speech/playback_status'](
            SimpleNamespace(playback_id='p1', state=state.messages.SpeechPlaybackStatus.PLAYING))
        state.callbacks['/malbut/speech/playback_status'](
            SimpleNamespace(playback_id='p1', state='invalid'))
        for client in state.clients.values():
            for future in client.futures:
                future.finish()
        raise KeyboardInterrupt

    logger = SimpleNamespace(**{
        level: lambda message, level=level: state.logs.append((level, message))
        for level in ('info', 'warning', 'error')
    })

    class Future:
        def __init__(self, response, failure):
            self.response = response
            self.failure = failure
            self.callbacks = []
            self.cancelled = False

        def add_done_callback(self, callback):
            self.callbacks.append(callback)

        def finish(self):
            for callback in self.callbacks:
                callback(self)

        def result(self):
            if self.failure:
                raise RuntimeError('private service response details')
            return self.response

        def cancel(self):
            self.cancelled = True
            self.finish()

    class Client:
        def __init__(self, service_type, name):
            self.name = name
            self.service_type = service_type
            self.futures = []
            self.requests = []
            self.removed = []

        def service_is_ready(self):
            return state.service_ready.get(self.name, True)

        def call_async(self, request):
            if state.service_failure.get(self.name):
                raise RuntimeError('private service send details')
            self.requests.append(request)
            response = (self.service_type.Response(decision=state.decision)
                        if self.name.endswith('classify_addressee')
                        else self.service_type.Response(accepted=state.accepted))
            future = Future(response, state.response_failure.get(self.name))
            self.futures.append(future)
            return future

        def remove_pending_request(self, future):
            self.removed.append(future)

    class Node:
        def __init__(self, name):
            state.calls['node_name'] = name

        def declare_parameter(self, name, default):
            state.calls.setdefault('defaults', {})[name] = default
            return SimpleNamespace(value=state.parameters.get(name, default))

        def get_logger(self):
            return logger

        def create_publisher(self, message_type, topic, qos):
            state.calls.setdefault('publishers', {})[topic] = (message_type, qos)
            if topic == '/malbut/speech/status':
                return SimpleNamespace(publish=lambda msg: state.statuses.append(
                    (msg.data, state.pipeline.phase)))
            return SimpleNamespace(publish=lambda msg: state.published.append((topic, msg)))

        def create_subscription(self, message_type, topic, callback, qos):
            state.callbacks[topic] = callback
            state.calls.setdefault('subscriptions', {})[topic] = (message_type, qos)

        def create_client(self, service_type, name):
            client = Client(service_type, name)
            state.clients[name] = client
            return client

        def destroy_node(self):
            state.closed.append('node')

    def create_recorder(**kwargs):
        state.calls['recorder'] = kwargs
        return object()

    def create_wake(model_path, **options):
        state.calls['wake'] = model_path
        state.calls['wake_options'] = options
        fail('initializing_wake')
        return SimpleNamespace(model=object())

    def share_wake(transcriber):
        state.calls['wake_shared'] = transcriber
        fail('initializing_wake')
        return SimpleNamespace(model=transcriber.model)

    create_wake.from_transcriber = share_wake

    def create_transcriber(model_path, **options):
        state.calls['local_stt'] = model_path
        state.calls['local_stt_options'] = options
        fail('initializing_stt')
        return SimpleNamespace(model=object())

    def close_cpp():
        state.closed.append('cpp')
        if state.native_cleanup_failure:
            raise RuntimeError('private native cleanup details')

    def create_cpp_transcriber(model_path, library_path, **options):
        state.calls.setdefault('cpp_stt', []).append((Path(model_path), Path(library_path)))
        state.calls['cpp_stt_options'] = options
        fail('initializing_stt')
        return SimpleNamespace(model=object(), close=close_cpp)

    def create_vad(mode):
        state.calls['vad_mode'] = mode
        return SimpleNamespace(is_speech=lambda frame, _: frame[:2] != b'\x00\x00')

    class Pipeline:
        def __init__(self, **kwargs):
            state.pipeline_args = kwargs
            state.pipeline = self
            self.phase = 'idle'
            self.pending_addressee = None
            self.session = SimpleNamespace(playback_id='p1', playback_state='playing')
            self.polled = False

        def start(self):
            self.phase = 'opening_microphone'
            fail(self.phase)
            state.pipeline_args['recorder_factory']()
            self.phase = 'starting_microphone'
            fail(self.phase)
            self.phase = 'running'
            state.pipeline_args['report']('waiting_for_wake')

        def poll(self):
            fail(self.phase)
            if self.polled:
                return
            self.polled = True
            if state.shutdown_before_publish:
                state.ok = False
            state.pipeline_args['publish_transcript']('u1', '원문 그대로')
            state.pipeline_args['publish_control']('p1', 'pause')
            self.pending_addressee = ('u2', 'p1', state.now + 45.0)
            state.pipeline_args['publish_interruption']('u2', 'p1', '로봇에게 한 말')
            state.pipeline_args['report']('barge_in_requires_aec')
            state.pipeline_args['on_wake']()
            state.pipeline_args['on_endpoint']()

        def on_playback_status(self, pid, status):
            if status == 'invalid':
                raise ValueError('private invalid content')
            state.calls['playback_status'] = (pid, status)

        def on_addressee(self, uid, pid, decision):
            state.calls['addressee'] = (uid, pid, decision)
            state.decisions.append((uid, pid, decision))
            self.pending_addressee = None

        def close(self):
            state.closed.append('pipeline')
            if state.cleanup_failure:
                raise RuntimeError('private cleanup device details')

    state.messages = SimpleNamespace(
        SpeechTranscript=type('SpeechTranscript', (SimpleNamespace,), {}),
        SpeechPlaybackStatus=type('SpeechPlaybackStatus', (SimpleNamespace,), {
            key.upper(): key for key in ('playing', 'paused', 'finished', 'failed', 'stopped')
        }),
    )
    state.services = SimpleNamespace(
        ClassifySpeechAddressee=SimpleNamespace(
            Request=type('ClassifyRequest', (SimpleNamespace,), {}),
            Response=type('ClassifyResponse', (SimpleNamespace,), {
                'ADDRESSED': 'addressed', 'NOT_ADDRESSED': 'not_addressed', 'UNKNOWN': 'unknown',
            }),
        ),
        ControlSpeechPlayback=SimpleNamespace(
            Request=type('ControlRequest', (SimpleNamespace,), {
                'PAUSE': 'pause', 'RESUME': 'resume', 'STOP': 'stop',
            }),
            Response=type('ControlResponse', (SimpleNamespace,), {}),
        ),
    )
    modules = {
        'rclpy': SimpleNamespace(
            init=init, ok=lambda: state.ok, shutdown=shutdown, spin_once=spin_once),
        'rclpy.node': SimpleNamespace(Node=Node),
        'rclpy.qos': SimpleNamespace(
            QoSProfile=SimpleNamespace,
            HistoryPolicy=SimpleNamespace(KEEP_LAST='keep_last'),
            ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
            DurabilityPolicy=SimpleNamespace(
                VOLATILE='volatile', TRANSIENT_LOCAL='transient_local'),
        ),
        'std_msgs.msg': SimpleNamespace(String=SimpleNamespace),
        'malbut_interfaces.msg': state.messages,
        'malbut_interfaces.srv': state.services,
        'pvporcupine': None,
        'pvrecorder': None,
        'webrtcvad': SimpleNamespace(Vad=create_vad),
        'openai': None,
        'faster_whisper': None,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr('malbut_stt.wake.LocalWakeRecognizer', create_wake)
    monkeypatch.setattr('malbut_stt.node.SoundDeviceRecorder', create_recorder)
    monkeypatch.setattr('malbut_stt.node.play_wake_chime',
                        lambda device: state.calls.setdefault('chimes', []).append(device))
    monkeypatch.setattr(
        'malbut_stt.node.play_endpoint_chime',
        lambda device: state.calls.setdefault('endpoint_chimes', []).append(device))
    monkeypatch.setattr('malbut_stt.node.LocalWhisperTranscriber', create_transcriber)
    monkeypatch.setattr(
        'malbut_stt.cpp_transcription.CppWhisperTranscriber', create_cpp_transcriber)
    monkeypatch.setattr('malbut_stt.node.DialoguePipeline', Pipeline)
    monkeypatch.setattr('malbut_stt.node.monotonic', lambda: state.now)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('PICOVOICE_ACCESS_KEY', raising=False)
    return state


@pytest.fixture
def cpp_runtime(runtime, tmp_path):
    """Provide existing native asset files while keeping native loading mocked."""
    runtime.cpp_model = tmp_path / 'ggml-small.bin'
    runtime.cpp_library = tmp_path / 'libmalbut_whisper.so'
    runtime.cpp_model.touch()
    runtime.cpp_library.touch()
    runtime.parameters.update(
        backend='whisper_cpp', stt_model_path=str(runtime.cpp_model),
        stt_library_path=str(runtime.cpp_library), wake_model_path='',
    )
    return runtime


def test_local_entrypoint_wires_continuous_pipeline_and_ros_callbacks(runtime):
    assert main(['--ros-args']) == 0
    assert runtime.calls['ros_args'] == ['--ros-args']
    assert runtime.calls['node_name'] == 'malbut_stt'
    defaults = runtime.calls['defaults']
    assert defaults['silence_timeout_s'] == 2.0
    assert defaults['start_timeout_s'] == 5.0
    assert defaults['max_utterance_s'] == 0.0
    assert defaults['max_buffer_s'] == 60.0
    assert defaults['stt_decode_timeout_s'] == 30.0
    assert defaults['wake_chime_device_index'] == -1
    assert defaults['pre_roll_s'] == 0.3
    assert defaults['wake_model_path'] == defaults['stt_model_path'] == ''
    assert defaults['compute_type'] == 'int8'
    assert defaults['backend'] == 'faster_whisper'
    assert defaults['stt_library_path'] == ''
    assert defaults['cpp_use_gpu'] is True
    assert defaults['cpp_threads'] == 6
    assert defaults['endpoint_predecode_s'] == 0.8
    assert defaults['input_has_aec'] is False
    assert defaults['playback_control_timeout_s'] == 5.0
    assert 'api_timeout_s' not in defaults
    assert 'keyword_path' not in defaults and 'language_model_path' not in defaults
    assert runtime.calls['vad_mode'] == 2
    assert defaults['device_index'] == 0
    assert runtime.calls['recorder'] == {'frame_length': 512, 'device_index': 0}
    assert runtime.calls['local_stt'] == Path(runtime.parameters['wake_model_path'])
    assert runtime.calls['local_stt_options'] == {'compute_type': 'int8'}
    assert 'wake' not in runtime.calls
    assert runtime.calls['wake_shared'] is runtime.pipeline_args['transcriber']
    assert runtime.pipeline_args['wake'].model is runtime.pipeline_args['transcriber'].model
    assert runtime.pipeline_args['input_has_aec'] is False
    assert runtime.pipeline_args['settings'].silence_timeout_s == 2.0
    assert runtime.pipeline_args['settings'].max_utterance_s is None
    assert runtime.pipeline_args['settings'].max_buffer_s == 60.0
    assert runtime.pipeline_args['endpoint_predecode_s'] == 0.8
    assert runtime.calls['spin_timeout'] == 0.02
    assert runtime.calls['playback_status'] == ('p1', 'playing')
    assert runtime.calls['addressee'] == ('u2', 'p1', 'addressed')
    assert ('warning', 'invalid_playback_status') in runtime.logs
    assert ('warning', 'barge_in_requires_aec') in runtime.logs
    assert runtime.calls['chimes'] == [-1]
    assert runtime.calls['endpoint_chimes'] == [-1]
    assert runtime.closed == ['pipeline', 'node', 'ros']
    assert [topic for topic, _ in runtime.published] == ['/malbut/speech/transcript']
    assert [vars(msg) for _, msg in runtime.published] == [
        {'utterance_id': 'u1', 'text': '원문 그대로'},
    ]
    assert set(runtime.calls['subscriptions']) == {'/malbut/speech/playback_status'}
    assert set(runtime.clients) == {
        '/malbut/speech/playback_control', '/malbut/speech/classify_addressee',
    }
    assert vars(runtime.clients['/malbut/speech/playback_control'].requests[0]) == {
        'playback_id': 'p1', 'command': 'pause',
    }
    assert vars(runtime.clients['/malbut/speech/classify_addressee'].requests[0]) == {
        'utterance_id': 'u2', 'playback_id': 'p1', 'text': '로봇에게 한 말',
    }
    assert ('info', 'playback_control_accepted:pause') in runtime.logs
    for _, qos in [runtime.calls['publishers']['/malbut/speech/transcript'],
                   *runtime.calls['subscriptions'].values()]:
        assert vars(qos) == {
            'history': 'keep_last', 'depth': 10,
            'reliability': 'reliable', 'durability': 'volatile',
        }


def test_readiness_is_latched_only_after_microphone_start(runtime, capsys):
    """Web readiness must represent successful capture startup, not just a publisher."""
    assert main() == 0
    assert runtime.statuses == [('ready', 'running')]
    assert capsys.readouterr().out.splitlines().count('malbut_speech_capture_ready') == 1
    _, qos = runtime.calls['publishers']['/malbut/speech/status']
    assert vars(qos) == {
        'history': 'keep_last', 'depth': 1,
        'reliability': 'reliable', 'durability': 'transient_local',
    }


@pytest.mark.parametrize('phase', [
    'initializing_stt', 'opening_microphone', 'starting_microphone',
])
def test_failed_microphone_or_model_never_reports_ready(runtime, phase, capsys):
    """A loaded node name or DDS publisher alone cannot satisfy Bringup readiness."""
    runtime.failure = phase
    assert main() == 1
    assert runtime.statuses == []
    assert 'malbut_speech_capture_ready' not in capsys.readouterr().out


def test_explicit_command_model_and_processed_microphone(runtime, tmp_path):
    model = tmp_path / 'command-model'
    model.mkdir()
    runtime.parameters.update(stt_model_path=str(model), input_has_aec=True, device_index=2)
    assert main() == 0
    assert runtime.calls['local_stt'] == model
    assert runtime.calls['wake'] == Path(runtime.parameters['wake_model_path'])
    assert 'wake_shared' not in runtime.calls
    assert runtime.pipeline_args['wake'].model is not runtime.pipeline_args['transcriber'].model
    assert runtime.pipeline_args['input_has_aec'] is True
    assert runtime.calls['recorder']['device_index'] == 2


@pytest.mark.parametrize('input_has_aec', [False, True])
def test_robot_deployment_disables_barge_in(runtime, input_has_aec):
    import malbut_stt.node as source_node

    path = Path(__file__).parents[2] / 'malbut_test/malbut_stt/malbut_stt/node.py'
    # Load the deployment copy without leaving forbidden __pycache__ files in it.
    robot_node = {'__name__': 'robot_stt_node', '__file__': str(path)}
    exec(compile(path.read_text(), str(path), 'exec'), robot_node)
    for name in ('DialoguePipeline', 'LocalWhisperTranscriber', 'SoundDeviceRecorder',
                 'play_wake_chime', 'play_endpoint_chime', 'monotonic'):
        robot_node[name] = getattr(source_node, name)
    runtime.parameters['input_has_aec'] = input_has_aec
    assert robot_node['main']() == 0
    assert runtime.pipeline_args['input_has_aec'] is False
    assert runtime.calls['endpoint_chimes'] == [-1]


def test_symlink_to_same_model_reuses_one_local_model(runtime, tmp_path):
    alias = tmp_path / 'same-model-alias'
    alias.symlink_to(runtime.parameters['wake_model_path'], target_is_directory=True)
    runtime.parameters['stt_model_path'] = str(alias)
    assert main() == 0
    assert runtime.calls['local_stt'] == alias
    assert 'wake' not in runtime.calls
    assert runtime.pipeline_args['wake'].model is runtime.pipeline_args['transcriber'].model


@pytest.mark.parametrize('shared', [True, False])
def test_float32_parameter_reaches_both_local_engines(runtime, tmp_path, shared):
    runtime.parameters['compute_type'] = 'float32'
    if not shared:
        second = tmp_path / 'separate-model'
        second.mkdir()
        runtime.parameters['stt_model_path'] = str(second)
    assert main() == 0
    assert runtime.calls['local_stt_options'] == {'compute_type': 'float32'}
    if shared:
        assert runtime.pipeline_args['wake'].model is runtime.pipeline_args['transcriber'].model
    else:
        assert runtime.calls['wake_options'] == {'compute_type': 'float32'}


@pytest.mark.parametrize('wake_path', ['blank', 'same', 'symlink'])
def test_cpp_backend_shares_one_native_model_and_closes_after_pipeline(cpp_runtime, wake_path):
    runtime = cpp_runtime
    if wake_path == 'same':
        runtime.parameters['wake_model_path'] = str(runtime.cpp_model)
    elif wake_path == 'symlink':
        alias = runtime.cpp_model.with_name('wake-alias.bin')
        alias.symlink_to(runtime.cpp_model)
        runtime.parameters['wake_model_path'] = str(alias)
    assert main() == 0
    assert runtime.calls['cpp_stt'] == [(runtime.cpp_model, runtime.cpp_library)]
    assert runtime.calls['cpp_stt_options'] == {
        'use_gpu': True, 'n_threads': 6, 'decode_timeout_s': 30.0}
    assert not {'local_stt', 'wake'} & runtime.calls.keys()
    assert runtime.calls['wake_shared'] is runtime.pipeline_args['transcriber']
    assert runtime.pipeline_args['wake'].model is runtime.pipeline_args['transcriber'].model
    assert runtime.pipeline_args['settings'].max_utterance_s is None
    assert runtime.pipeline_args['settings'].max_buffer_s == 60.0
    assert runtime.closed == ['pipeline', 'cpp', 'node', 'ros']


def test_cpp_backend_forwards_explicit_options_without_cpu_compute_type(cpp_runtime):
    cpp_runtime.parameters.update(
        cpp_use_gpu=False, cpp_threads=2, compute_type='float16', stt_decode_timeout_s=12.5)
    assert main() == 0
    assert cpp_runtime.calls['cpp_stt_options'] == {
        'use_gpu': False, 'n_threads': 2, 'decode_timeout_s': 12.5}
    assert cpp_runtime.closed == ['pipeline', 'cpp', 'node', 'ros']


@pytest.mark.parametrize('parameter, asset_kind', [
    ('stt_model_path', 'blank'), ('stt_model_path', 'missing'),
    ('stt_model_path', 'directory'), ('stt_library_path', 'blank'),
    ('stt_library_path', 'missing'), ('stt_library_path', 'directory'),
    ('wake_model_path', 'missing'), ('wake_model_path', 'directory'),
    ('wake_model_path', 'different_file'),
])
def test_cpp_invalid_asset_paths_fail_before_native_load(
    cpp_runtime, tmp_path, parameter, asset_kind,
):
    target = tmp_path / ('invalid-' + parameter)
    if asset_kind == 'directory':
        target.mkdir()
    elif asset_kind == 'different_file':
        target.touch()
    cpp_runtime.parameters[parameter] = '' if asset_kind == 'blank' else str(target)
    assert main() == 1
    assert not {'cpp_stt', 'local_stt', 'wake', 'recorder'} & cpp_runtime.calls.keys()
    assert cpp_runtime.closed == ['node', 'ros']


@pytest.mark.parametrize('parameter, value', [
    ('cpp_use_gpu', 'true'), ('cpp_use_gpu', 1), ('cpp_use_gpu', None),
    ('cpp_threads', 0), ('cpp_threads', -1), ('cpp_threads', True),
    ('cpp_threads', 1.5), ('cpp_threads', '6'),
])
def test_cpp_invalid_options_fail_before_native_load(cpp_runtime, parameter, value):
    cpp_runtime.parameters[parameter] = value
    assert main() == 1
    assert not {'cpp_stt', 'local_stt', 'wake', 'recorder'} & cpp_runtime.calls.keys()
    assert cpp_runtime.closed == ['node', 'ros']


@pytest.mark.parametrize('failure', [
    'initializing_stt', 'wake', 'opening_microphone', 'running',
    'pipeline_close', 'native_close',
])
def test_cpp_cleanup_releases_native_model_and_ros_on_failures(cpp_runtime, failure):
    if failure == 'wake':
        cpp_runtime.failure = 'initializing_wake'
    elif failure == 'pipeline_close':
        cpp_runtime.cleanup_failure = True
    elif failure == 'native_close':
        cpp_runtime.native_cleanup_failure = True
    else:
        cpp_runtime.failure = failure
    assert main() == 1
    if failure == 'initializing_stt':
        expected = ['node', 'ros']
    elif failure == 'wake':
        expected = ['cpp', 'node', 'ros']
    else:
        expected = ['pipeline', 'cpp', 'node', 'ros']
    assert cpp_runtime.closed == expected
    assert all('private' not in message and 'hidden' not in message
               for _, message in cpp_runtime.logs)


@pytest.mark.parametrize('configured, expected', [(0.0, None), (30, 30), (31.5, 31.5)])
def test_max_utterance_parameter_preserves_explicit_limit(
    runtime, configured, expected,
):
    runtime.parameters['max_utterance_s'] = configured
    assert main() == 0
    assert runtime.pipeline_args['settings'].max_utterance_s == expected


def test_acknowledgement_chimes_use_selected_output_device(runtime):
    runtime.parameters['wake_chime_device_index'] = 4
    assert main() == 0
    assert runtime.calls['chimes'] == [4]
    assert runtime.calls['endpoint_chimes'] == [4]


def test_endpoint_chime_failure_is_reported_as_warning(runtime):
    def report_failure():
        runtime.pipeline_args['report']('endpoint_chime_failed:RuntimeError')
        raise KeyboardInterrupt

    runtime.on_spin = report_failure
    assert main() == 0
    assert ('warning', 'endpoint_chime_failed:RuntimeError') in runtime.logs


@pytest.mark.parametrize('predecode', [0.2, 1.0])
def test_explicit_predecode_parameter_reaches_pipeline(runtime, predecode):
    runtime.parameters['endpoint_predecode_s'] = predecode
    assert main() == 0
    assert runtime.pipeline_args['endpoint_predecode_s'] == predecode


def test_short_custom_silence_fallback_disables_early_predecode(runtime):
    runtime.parameters['silence_timeout_s'] = 0.5
    assert main() == 0
    assert runtime.pipeline_args['settings'].silence_timeout_s == 0.5
    assert runtime.pipeline_args['endpoint_predecode_s'] is None


@pytest.mark.parametrize('phase', [
    'initializing_wake', 'initializing_stt', 'opening_microphone', 'running',
])
def test_runtime_failure_is_sanitized_and_releases_ros(runtime, phase):
    runtime.failure = phase
    assert main() == 1
    assert ('error', 'STT stopped during ' + phase + ': RuntimeError') in runtime.logs
    assert all('hidden' not in message for _, message in runtime.logs)
    assert runtime.closed[-2:] == ['node', 'ros']
    if phase in ('opening_microphone', 'running'):
        assert runtime.closed[0] == 'pipeline'
    assert runtime.published == []


def test_cleanup_failure_still_destroys_node_and_shuts_down_ros(runtime):
    runtime.cleanup_failure = True
    assert main() == 1
    assert runtime.closed == ['pipeline', 'node', 'ros']
    assert ('error', 'STT cleanup failed: RuntimeError') in runtime.logs
    assert all('private' not in message for _, message in runtime.logs)


def test_publication_and_callbacks_are_guarded_after_ros_shutdown(runtime):
    runtime.shutdown_before_publish = True
    assert main() == 0
    assert runtime.published == []
    runtime.callbacks['/malbut/speech/playback_status'](
        SimpleNamespace(playback_id='late', state='playing'))
    assert 'playback_status' not in runtime.calls and 'addressee' not in runtime.calls
    assert all(not client.requests for client in runtime.clients.values())
    assert runtime.closed == ['pipeline', 'node']


@pytest.mark.parametrize('parameter, value', [
    ('wake_model_path', ''), ('wake_model_path', '/missing/local-model'),
    ('stt_model_path', '/missing/command-model'), ('vad_mode', 4),
    ('input_has_aec', 'true'), ('silence_timeout_s', -1.0),
    ('playback_control_timeout_s', 0.0), ('playback_control_timeout_s', float('nan')),
    ('playback_control_timeout_s', float('inf')), ('playback_control_timeout_s', True),
    ('compute_type', 'auto'), ('compute_type', 32),
    ('backend', 'mlx'), ('backend', ''), ('backend', 1),
    ('max_utterance_s', -1.0), ('max_utterance_s', True), ('max_utterance_s', False),
    ('max_utterance_s', float('nan')), ('max_utterance_s', float('inf')),
    ('max_utterance_s', None), ('max_utterance_s', 2.0),
    ('max_buffer_s', 0), ('max_buffer_s', -1), ('max_buffer_s', 2.0),
    ('max_buffer_s', True), ('max_buffer_s', None),
    ('max_buffer_s', float('inf')), ('max_buffer_s', float('nan')),
    ('wake_chime_device_index', -2), ('wake_chime_device_index', True),
    ('wake_chime_device_index', 1.5),
    ('stt_decode_timeout_s', 0), ('stt_decode_timeout_s', -1),
    ('stt_decode_timeout_s', None), ('stt_decode_timeout_s', True),
    ('stt_decode_timeout_s', float('nan')), ('stt_decode_timeout_s', float('inf')),
    ('endpoint_predecode_s', 0.0), ('endpoint_predecode_s', -0.1),
    ('endpoint_predecode_s', 1.01), ('endpoint_predecode_s', True),
    ('endpoint_predecode_s', float('nan')), ('endpoint_predecode_s', float('inf')),
    ('endpoint_predecode_s', None),
])
def test_invalid_configuration_fails_before_local_models_or_microphone(runtime, parameter, value):
    runtime.parameters[parameter] = value
    assert main() == 1
    assert not {'wake', 'local_stt', 'cpp_stt', 'recorder'} & runtime.calls.keys()
    assert runtime.closed == ['node', 'ros']
    assert runtime.published == []


def test_generated_constants_are_used_at_ros_boundaries(runtime):
    runtime.services.ControlSpeechPlayback.Request.PAUSE = 'wire-pause'
    runtime.services.ClassifySpeechAddressee.Response.ADDRESSED = 'wire-addressed'
    runtime.messages.SpeechPlaybackStatus.PLAYING = 'wire-playing'
    runtime.decision = 'wire-addressed'
    assert main() == 0
    assert runtime.clients['/malbut/speech/playback_control'].requests[0].command == 'wire-pause'
    assert runtime.calls['playback_status'] == ('p1', 'playing')
    assert runtime.decisions == [('u2', 'p1', 'addressed')]


@pytest.mark.parametrize('service', ['playback_control', 'classify_addressee'])
def test_unavailable_services_do_not_block_or_claim_playback_control(runtime, service):
    name = '/malbut/speech/' + service
    runtime.service_ready[name] = False
    assert main() == 0
    assert runtime.clients[name].requests == []
    assert runtime.calls['spin_timeout'] == 0.02
    if service == 'classify_addressee':
        assert runtime.decisions == [('u2', 'p1', 'unknown')]
        assert ('warning', 'addressee_service_unavailable') in runtime.logs
    else:
        assert ('warning', 'playback_control_service_unavailable') in runtime.logs
    assert runtime.pipeline.session.playback_state == 'playing'


@pytest.mark.parametrize('boundary', ['service_failure', 'response_failure'])
@pytest.mark.parametrize('service', ['playback_control', 'classify_addressee'])
def test_service_failures_are_sanitized_and_classification_fails_unknown(
    runtime, boundary, service,
):
    name = '/malbut/speech/' + service
    getattr(runtime, boundary)[name] = True
    assert main() == 0
    expected = ('addressee_service_failed' if service == 'classify_addressee'
                else 'playback_control_failed')
    assert ('warning', expected + ':RuntimeError') in runtime.logs
    assert all('private' not in text for _, text in runtime.logs)
    if service == 'classify_addressee':
        assert runtime.decisions == [('u2', 'p1', 'unknown')]
    assert runtime.pipeline.session.playback_state == 'playing'


def test_rejected_control_is_observed_without_fabricating_a_paused_state(runtime):
    runtime.accepted = False
    assert main() == 0
    assert ('warning', 'playback_control_rejected:pause') in runtime.logs
    assert runtime.pipeline.session.playback_state == 'playing'


def test_invalid_classifier_decision_falls_back_to_unknown(runtime):
    runtime.decision = 'not-a-contract-decision'
    assert main() == 0
    assert runtime.decisions == [('u2', 'p1', 'unknown')]
    assert ('warning', 'addressee_service_failed:KeyError') in runtime.logs


def test_classification_timeout_removes_future_and_ignores_late_response(runtime):
    spins = []

    def spin():
        spins.append(True)
        if len(spins) == 1:
            runtime.now = 45.0
            return
        for client in runtime.clients.values():
            for future in client.futures:
                future.finish()  # A remote result may arrive even after local cancellation.
        raise KeyboardInterrupt

    runtime.on_spin = spin
    assert main() == 0
    assert runtime.decisions == [('u2', 'p1', 'unknown')]
    assert ('warning', 'addressee_service_response_timeout') in runtime.logs
    future = runtime.clients['/malbut/speech/classify_addressee'].futures[0]
    assert future.cancelled
    assert future in runtime.clients['/malbut/speech/classify_addressee'].removed


def test_response_arriving_at_deadline_is_unknown_before_next_poll(runtime):
    def spin():
        runtime.now = 45.0
        runtime.clients['/malbut/speech/classify_addressee'].futures[0].finish()
        raise KeyboardInterrupt

    runtime.on_spin = spin
    assert main() == 0
    assert runtime.decisions == [('u2', 'p1', 'unknown')]


def test_changed_session_discards_classifier_future_without_reactivating_it(runtime):
    def spin():
        runtime.pipeline.pending_addressee = None
        runtime.clients['/malbut/speech/classify_addressee'].futures[0].finish()
        raise KeyboardInterrupt

    runtime.on_spin = spin
    assert main() == 0
    assert runtime.decisions == []
    client = runtime.clients['/malbut/speech/classify_addressee']
    assert client.removed == client.futures


def test_replacement_classification_removes_previous_future_and_correlates_new_result(runtime):
    def spin():
        client = runtime.clients['/malbut/speech/classify_addressee']
        previous = client.futures[0]
        runtime.pipeline.pending_addressee = ('new-u', 'new-p', 45.0)
        runtime.pipeline_args['publish_interruption']('new-u', 'new-p', '새 발화')
        previous.finish()
        assert runtime.decisions == []
        assert previous.cancelled and previous in client.removed
        client.futures[-1].finish()
        raise KeyboardInterrupt

    runtime.on_spin = spin
    assert main() == 0
    assert runtime.decisions == [('new-u', 'new-p', 'addressed')]


def test_control_timeout_parameter_bounds_wait_without_changing_playback_state(runtime):
    runtime.parameters['playback_control_timeout_s'] = 1.0
    spins = []

    def spin():
        spins.append(True)
        if len(spins) == 1:
            runtime.now = 1.0
            return
        client = runtime.clients['/malbut/speech/playback_control']
        assert client.futures[0].cancelled
        client.futures[0].finish()
        raise KeyboardInterrupt

    runtime.on_spin = spin
    assert main() == 0
    assert ('warning', 'playback_control_response_timeout') in runtime.logs
    assert not any(text.startswith('playback_control_accepted') for _, text in runtime.logs)
    assert runtime.pipeline.session.playback_state == 'playing'


def test_old_playback_control_response_is_reported_as_stale(runtime):
    def spin():
        runtime.pipeline.session.playback_id = 'new-playback'
        runtime.clients['/malbut/speech/playback_control'].futures[0].finish()
        raise KeyboardInterrupt

    runtime.on_spin = spin
    assert main() == 0
    assert ('warning', 'playback_control_response_stale') in runtime.logs
    assert not any(text.startswith('playback_control_accepted') for _, text in runtime.logs)


def test_control_requests_are_bounded_and_shutdown_removes_all_pending_waits(runtime):
    def spin():
        for _ in range(12):
            runtime.pipeline_args['publish_control']('p1', 'pause')
        assert len(runtime.clients['/malbut/speech/playback_control'].requests) == 8
        raise KeyboardInterrupt

    runtime.on_spin = spin
    assert main() == 0
    assert ('warning', 'playback_control_requests_full') in runtime.logs
    for client in runtime.clients.values():
        assert len(client.removed) == len(client.futures)
        assert all(future.cancelled for future in client.futures)
        for future in client.futures:
            future.finish()
    assert runtime.decisions == []
