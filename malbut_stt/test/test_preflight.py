"""Check robot STT preflight boundaries without a model, microphone, or network."""

import socket
import struct
import sys
from types import SimpleNamespace

import pytest

from malbut_stt.preflight import check_stt


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        calls={}, events=[], failure=None, sample_rate=16000,
        samples=[0, 1, -1, 32767, -32768] + [0] * 507,
    )

    def event(name):
        state.events.append(name)
        if state.failure == name:
            raise RuntimeError(f'{name} failed')

    def forbidden(*args, **kwargs):
        raise AssertionError('preflight must not transcribe or connect to the network')

    class Transcriber:
        def __init__(self, model_path, library_path, **options):
            state.calls['model'] = (model_path, library_path, options)
            event('model_load')
            self.metadata = {'bridge_abi': 2, 'model_type': 'small'}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            event('model_close')

        transcribe = forbidden

    class Recorder:
        def __init__(self, **options):
            state.calls['recorder'] = options
            event('recorder_init')

        @property
        def sample_rate(self):
            event('sample_rate')
            return state.sample_rate

        def start(self):
            event('start')

        def read(self):
            event('read')
            return state.samples

        def stop(self):
            event('stop')

        def delete(self):
            event('delete')

    class Vad:
        def __init__(self, mode):
            state.calls['vad_mode'] = mode
            event('vad_init')

        def is_speech(self, pcm, rate):
            state.calls['vad_frame'] = (pcm, rate)
            event('vad')
            return False

    monkeypatch.setitem(sys.modules, 'pvrecorder', SimpleNamespace(PvRecorder=Recorder))
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(Vad=Vad))
    monkeypatch.setattr('malbut_stt.cpp_transcription.CppWhisperTranscriber', Transcriber)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    return state


def check(**options):
    return check_stt('/models/small.bin', '/lib/bridge.so', **options)


def test_local_model_and_microphone_check_reports_only_verified_capabilities(runtime):
    result = check(device_index=3, cpp_threads=4)
    assert runtime.calls['model'] == (
        '/models/small.bin', '/lib/bridge.so', {'use_gpu': True, 'n_threads': 4},
    )
    assert runtime.calls['recorder'] == {'frame_length': 512, 'device_index': 3}
    assert runtime.calls['vad_mode'] == 2
    assert runtime.calls['vad_frame'] == (struct.pack('<320h', *runtime.samples[:320]), 16000)
    assert runtime.events[-3:] == ['stop', 'delete', 'model_close']
    assert result == {
        'bridge_abi': 2, 'model_type': 'small', 'requested_use_gpu': True,
        'cuda_execution_verified': False, 'microphone_sample_rate': 16000,
    }


@pytest.mark.parametrize('device_index', [-2, True, 0.5, '0', None])
def test_invalid_device_fails_before_loading_model_or_opening_microphone(runtime, device_index):
    with pytest.raises(ValueError, match='device_index'):
        check(device_index=device_index)
    assert runtime.events == []


def test_model_load_failure_never_opens_microphone(runtime):
    runtime.failure = 'model_load'
    with pytest.raises(RuntimeError, match='model_load failed'):
        check()
    assert runtime.events == ['vad_init', 'model_load']


@pytest.mark.parametrize('phase', [
    'recorder_init', 'sample_rate', 'start', 'read', 'vad', 'stop', 'delete',
])
def test_runtime_failure_releases_every_acquired_resource(runtime, phase):
    runtime.failure = phase
    with pytest.raises(RuntimeError, match=f'{phase} failed'):
        check()
    assert runtime.events[-1] == 'model_close'
    assert runtime.events.count('model_close') == 1
    assert runtime.events.count('delete') == (0 if phase == 'recorder_init' else 1)
    assert runtime.events.count('stop') == (1 if phase in ('read', 'vad', 'stop', 'delete') else 0)


@pytest.mark.parametrize('sample_rate', [8000, 48000])
def test_unsupported_microphone_format_releases_resources_without_starting(runtime, sample_rate):
    runtime.sample_rate = sample_rate
    with pytest.raises(ValueError, match='16 kHz PCM'):
        check()
    assert 'start' not in runtime.events
    assert runtime.events[-2:] == ['delete', 'model_close']


@pytest.mark.parametrize('sample_count', [0, 511, 513])
def test_short_or_oversized_microphone_frame_is_rejected_and_cleaned_up(runtime, sample_count):
    runtime.samples = [0] * sample_count
    with pytest.raises(ValueError, match='incomplete frame'):
        check()
    assert 'vad' not in runtime.events
    assert runtime.events[-3:] == ['stop', 'delete', 'model_close']


def test_invalid_pcm_sample_is_rejected_and_cleaned_up(runtime):
    runtime.samples[0] = 32768
    with pytest.raises(struct.error):
        check()
    assert 'vad' not in runtime.events
    assert runtime.events[-3:] == ['stop', 'delete', 'model_close']
