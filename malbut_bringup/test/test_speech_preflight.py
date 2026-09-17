"""Exercise speech admission without ROS, microphone hardware, or API requests."""

import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_bringup import speech_preflight as preflight


@pytest.fixture
def checks(monkeypatch):
    state = SimpleNamespace(calls=[], failure=None)

    def check(name, *args, **kwargs):
        state.calls.append((name, args, kwargs))
        if state.failure == name:
            raise RuntimeError('secret credential and private microphone content')
        return {'bridge_abi': 2, 'cuda_execution_verified': False}

    for name in ('check_interfaces', 'check_agent', 'check_tts', 'wait_for_peers'):
        monkeypatch.setattr(preflight, name, lambda *args, _name=name, **kwargs:
                            check(_name, *args, **kwargs))
    monkeypatch.setattr('malbut_stt.preflight.check_stt',
                        lambda *args, **kwargs: check('check_stt', *args, **kwargs))
    return state


def test_success_checks_consumers_before_microphone_and_reports_proof_limits(checks, capsys):
    assert preflight.main([
        '--stt-model-path', '/model.bin', '--stt-library-path', '/bridge.so',
        '--input-device', '2', '--output-device', '3', '--cpp-threads', '4',
        '--agent-provider', 'mock',
    ]) == 0
    assert checks.calls == [
        ('check_interfaces', (), {}), ('check_agent', ('mock',), {}),
        ('check_tts', (3,), {}),
        ('check_stt', ('/model.bin', '/bridge.so'), {'device_index': 2, 'cpp_threads': 4}),
    ]
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out) == {
        'event': 'speech_preflight_passed', 'bridge_abi': 2,
        'cuda_execution_verified': False, 'api_request_verified': False,
        'transcription_verified': False,
    }


@pytest.mark.parametrize('failure,phase', [
    ('check_interfaces', 'ros_interfaces'), ('check_agent', 'agent_configuration'),
    ('check_tts', 'tts_output'), ('check_stt', 'stt_model_and_microphone'),
])
def test_failure_stops_later_checks_and_does_not_print_sensitive_errors(
        checks, capsys, failure, phase):
    checks.failure = failure
    assert preflight.main([]) == 2
    assert checks.calls[-1][0] == failure
    output = capsys.readouterr()
    assert output.out == ''
    assert json.loads(output.err) == {
        'event': 'speech_preflight_failed', 'phase': phase, 'error_type': 'RuntimeError',
    }


@pytest.mark.parametrize('argument', [
    '--input-device=-2', '--output-device=-2', '--cpp-threads=0',
    '--timeout-s=0', '--timeout-s=-1', '--timeout-s=nan', '--timeout-s=inf',
])
def test_invalid_configuration_rejects_before_runtime_checks(checks, capsys, argument):
    assert preflight.main([argument]) == 2
    assert checks.calls == []
    assert json.loads(capsys.readouterr().err) == {
        'event': 'speech_preflight_failed', 'phase': 'configuration', 'error_type': 'ValueError',
    }


def test_peer_mode_never_opens_microphone_or_audio_output(checks, capsys):
    assert preflight.main(['--wait-for-peers', '--timeout-s', '2']) == 0
    assert checks.calls == [('check_interfaces', (), {}), ('wait_for_peers', (2.0,), {})]
    assert json.loads(capsys.readouterr().out) == {'event': 'speech_peers_ready'}


def test_peer_mode_failure_is_sanitized(checks, capsys):
    checks.failure = 'wait_for_peers'
    assert preflight.main(['--wait-for-peers']) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert json.loads(output.err) == {
        'event': 'speech_preflight_failed', 'phase': 'speech_peers', 'error_type': 'RuntimeError',
    }


def test_interrupt_returns_shell_interrupt_status(checks, monkeypatch, capsys):
    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(preflight, 'check_interfaces', interrupt)
    assert preflight.main([]) == 130
    assert capsys.readouterr() == ('', '')


@pytest.fixture
def peers(monkeypatch):
    service_names = ('/malbut/speech/classify_addressee', '/malbut/speech/playback_control')
    state = SimpleNamespace(
        now=0.0, ok=False, events=[], clients=[], failure=None, on_spin=None,
        services={name: True for name in service_names},
        endpoints={
            ('subscription', '/malbut/speech/transcript'):
                'malbut_interfaces/msg/SpeechTranscript',
            ('subscription', '/malbut/speech/response'): 'malbut_interfaces/msg/SpeechRequest',
            ('publisher', '/malbut/speech/response'): 'malbut_interfaces/msg/SpeechRequest',
            ('publisher', '/malbut/speech/playback_status'):
                'malbut_interfaces/msg/SpeechPlaybackStatus',
        },
    )
    state.types = SimpleNamespace(ClassifySpeechAddressee=type('ClassifySpeechAddressee', (), {}),
                                  ControlSpeechPlayback=type('ControlSpeechPlayback', (), {}))

    def forbidden(*args, **kwargs):
        raise AssertionError('readiness must not send service requests')

    def event(name):
        state.events.append(name)
        if state.failure == name:
            raise RuntimeError(name)

    class Node:
        def __init__(self, name):
            event('node')
            assert name == 'speech_peer_check'

        def create_client(self, service_type, name):
            state.clients.append((service_type, name))
            return SimpleNamespace(service_is_ready=lambda: state.services[name],
                                   call=forbidden, call_async=forbidden)

        def get_subscriptions_info_by_topic(self, topic):
            return self._query('subscription', topic)

        def get_publishers_info_by_topic(self, topic):
            return self._query('publisher', topic)

        def _query(self, kind, topic):
            value = state.endpoints.get((kind, topic))
            return [] if value is None else [SimpleNamespace(topic_type=value)]

        def destroy_node(self):
            event('destroy')

    def init():
        state.ok = True
        event('init')

    def shutdown():
        state.ok = False
        event('shutdown')

    def spin_once(node, timeout_sec):
        assert timeout_sec == 0.1
        state.now += timeout_sec
        event('spin')
        if state.on_spin is not None:
            state.on_spin()

    monkeypatch.setitem(sys.modules, 'rclpy', SimpleNamespace(
        init=init, ok=lambda: state.ok, shutdown=shutdown, spin_once=spin_once))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=Node))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', state.types)
    monkeypatch.setattr(preflight.time, 'monotonic', lambda: state.now)
    return state


def test_ready_peers_require_typed_service_clients_and_release_ros(peers):
    preflight.wait_for_peers(1)
    assert peers.clients == [
        (peers.types.ClassifySpeechAddressee, '/malbut/speech/classify_addressee'),
        (peers.types.ControlSpeechPlayback, '/malbut/speech/playback_control'),
    ]
    assert peers.events == ['init', 'node', 'destroy', 'shutdown']


@pytest.mark.parametrize('service', [
    '/malbut/speech/classify_addressee', '/malbut/speech/playback_control',
])
def test_missing_service_blocks_readiness_and_times_out_with_cleanup(peers, service):
    peers.services[service] = False
    with pytest.raises(RuntimeError, match='speech_peers_not_ready'):
        preflight.wait_for_peers(0.2)
    assert peers.events[-2:] == ['destroy', 'shutdown']
    assert peers.events.count('spin') == 2


@pytest.mark.parametrize('endpoint', [
    ('subscription', '/malbut/speech/transcript'),
    ('subscription', '/malbut/speech/response'),
    ('publisher', '/malbut/speech/response'),
    ('publisher', '/malbut/speech/playback_status'),
])
@pytest.mark.parametrize('topic_type', [None, 'std_msgs/msg/String'])
def test_missing_or_wrong_type_endpoint_blocks_readiness(peers, endpoint, topic_type):
    peers.endpoints[endpoint] = topic_type
    with pytest.raises(RuntimeError, match='speech_peers_not_ready'):
        preflight.wait_for_peers(0.2)
    assert peers.events[-2:] == ['destroy', 'shutdown']


def test_discovery_can_become_ready_after_spinning(peers):
    name = '/malbut/speech/playback_control'
    peers.services[name] = False
    peers.on_spin = lambda: peers.services.update({name: True})
    preflight.wait_for_peers(1)
    assert peers.events.count('spin') == 1
    assert peers.events[-2:] == ['destroy', 'shutdown']


@pytest.mark.parametrize('failure', ['node', 'spin', 'destroy'])
def test_ros_failures_still_shutdown_context(peers, failure):
    peers.failure = failure
    if failure == 'spin':
        peers.services['/malbut/speech/playback_control'] = False
    with pytest.raises(RuntimeError, match=failure):
        preflight.wait_for_peers(1)
    assert peers.events[-1] == 'shutdown'
    assert peers.events.count('destroy') == (0 if failure == 'node' else 1)


@pytest.fixture
def audio_output(monkeypatch):
    state = SimpleNamespace(calls=[], closed=False, fail_write=False)

    def forbidden(*args, **kwargs):
        raise AssertionError('preflight must not construct an API client')

    def check_output_settings(**options):
        state.calls.append(('settings', options))

    class OutputStream:
        def __init__(self, **options):
            state.calls.append(('stream', options))

        def __enter__(self):
            return self

        def write(self, data):
            state.data = data
            if state.fail_write:
                raise RuntimeError('output failed')

        def __exit__(self, *_):
            state.closed = True

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key-never-sent')
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(AsyncOpenAI=forbidden))
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(
        check_output_settings=check_output_settings, OutputStream=OutputStream))
    return state


@pytest.mark.parametrize('device,expected', [(-1, None), (2, 2)])
def test_tts_validates_real_sdk_loader_and_writes_only_silence(audio_output, device, expected):
    preflight.check_tts(device)
    options = {'device': expected, 'channels': 1, 'dtype': 'float32', 'samplerate': 24000}
    assert audio_output.calls == [('settings', options), ('stream', options)]
    assert audio_output.data.shape == (2400, 1)
    assert audio_output.data.dtype == np.float32
    assert not np.any(audio_output.data)
    assert audio_output.closed


def test_tts_missing_key_fails_before_output_device_is_opened(audio_output, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY')
    with pytest.raises(RuntimeError, match='missing_api_key'):
        preflight.check_tts(-1)
    assert audio_output.calls == []


def test_tts_output_failure_closes_stream(audio_output):
    audio_output.fail_write = True
    with pytest.raises(RuntimeError, match='output failed'):
        preflight.check_tts(-1)
    assert audio_output.closed
