"""Verify PortAudio device IDs and PCM capture without opening real hardware."""

import struct
import sys
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from malbut_stt.audio import MicrophoneOverflow, SoundDeviceRecorder


@pytest.fixture
def sounddevice(monkeypatch):
    monkeypatch.delenv('MALBUT_SHARED_MICROPHONE', raising=False)
    devices = [
        {'name': 'XFM-DP-V0.0.18: USB Audio', 'max_input_channels': 1},
        {'name': 'Speaker', 'max_input_channels': 0},
        {'name': 'USB Audio Device', 'max_input_channels': 1},
        {'name': 'HDMI', 'max_input_channels': 0},
        {'name': 'default', 'max_input_channels': 2},
    ]
    state = SimpleNamespace(
        devices=devices, calls=[], samples=None, overflowed=False,
    )

    def query_devices(device=None, kind=None):
        if kind == 'input':
            if device == 'pulse':
                return {'name': 'pulse', 'max_input_channels': 32}
            return devices[4 if device is None else device]
        return devices

    def check_input_settings(**options):
        state.calls.append(('check', options))
        device = options['device']
        if device not in (None, 'pulse') and (
                device >= len(devices) or not devices[device]['max_input_channels']):
            raise RuntimeError('invalid input device')

    class Stream:
        def __init__(self, **options):
            state.callback = options.pop('callback')
            state.frame_length = options['blocksize']
            state.calls.append(('open', options))

        def start(self):
            state.calls.append(('start',))

        def stop(self):
            state.calls.append(('stop',))

        def close(self):
            state.calls.append(('close',))

    def emit():
        frame_length = state.frame_length
        samples = state.samples
        if samples is None:
            samples = [-32768, -1, 0, 1, 32767] + [0] * (frame_length - 5)
        state.callback(struct.pack(f'<{len(samples)}h', *samples), frame_length, None,
                       SimpleNamespace(input_overflow=state.overflowed))

    state.emit = emit

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(
        query_devices=query_devices,
        check_input_settings=check_input_settings,
        RawInputStream=Stream,
    ))
    return state


@pytest.mark.parametrize('device_index', [0, 2, 4, -1])
def test_device_id_matches_sounddevice_listing(sounddevice, device_index):
    """Output-only entries must not shift the requested microphone ID."""
    recorder = SoundDeviceRecorder(device_index=device_index)
    device = None if device_index == -1 else device_index
    assert sounddevice.calls == [
        ('check', {'device': device, 'channels': 1, 'dtype': 'int16',
                   'samplerate': 16000}),
        ('open', {'device': device, 'samplerate': 16000, 'channels': 1,
                  'dtype': 'int16', 'blocksize': 512}),
    ]
    selected = 4 if device is None else device
    assert recorder.selected_device == sounddevice.devices[selected]['name']


def test_shared_xfm_uses_pulse_without_changing_pcm(sounddevice, monkeypatch):
    """The busy hardware's PortAudio index must not select a different mic."""
    monkeypatch.setenv('MALBUT_SHARED_MICROPHONE', 'alsa_input.xfm')
    monkeypatch.setenv('PULSE_SOURCE', 'alsa_input.xfm')
    sounddevice.devices[0] = sounddevice.devices[2]
    recorder = SoundDeviceRecorder(device_index=0)
    assert sounddevice.calls == [
        ('check', {'device': 'pulse', 'channels': 1, 'dtype': 'int16',
                   'samplerate': 16000}),
        ('open', {'device': 'pulse', 'samplerate': 16000, 'channels': 1,
                  'dtype': 'int16', 'blocksize': 512}),
    ]
    assert recorder.selected_device == 'pulse'
    assert recorder.sample_rate == 16000
    sounddevice.emit()
    assert len(recorder.read()) == 512
    SoundDeviceRecorder(device_index=2)
    assert sounddevice.calls[-1][1]['device'] == 2


def test_shared_xfm_refuses_a_different_pulse_source(sounddevice, monkeypatch):
    """Never silently share the desktop's unrelated default input."""
    monkeypatch.setenv('MALBUT_SHARED_MICROPHONE', 'alsa_input.xfm')
    monkeypatch.setenv('PULSE_SOURCE', 'alsa_input.other')
    with pytest.raises(RuntimeError, match='does not match PULSE_SOURCE'):
        SoundDeviceRecorder(device_index=0)
    assert not sounddevice.calls


@pytest.mark.parametrize('device_index', [1, 3, 9])
def test_invalid_input_device_is_rejected_before_stream_open(sounddevice, device_index):
    """Output-only and missing IDs must not silently select another microphone."""
    with pytest.raises(RuntimeError, match='invalid input device'):
        SoundDeviceRecorder(device_index=device_index)
    assert len(sounddevice.calls) == 1
    assert sounddevice.calls[0][0] == 'check'


def test_device_listing_preserves_global_ids(sounddevice):
    """Enumerating the public list must retain gaps between input devices."""
    assert SoundDeviceRecorder.get_available_devices() == [
        info['name'] for info in sounddevice.devices
    ]


@pytest.mark.parametrize('frame_length', [320, 512])
def test_pcm16_capture_preserves_samples_and_lifecycle(sounddevice, frame_length):
    """The dialogue pipeline still receives complete mono 16 kHz sample lists."""
    recorder = SoundDeviceRecorder(frame_length=frame_length, device_index=0)
    assert recorder.sample_rate == 16000
    assert sounddevice.calls[1][1]['blocksize'] == frame_length
    recorder.start()
    sounddevice.emit()
    assert recorder.read() == [-32768, -1, 0, 1, 32767] + [0] * (frame_length - 5)
    recorder.stop()
    recorder.delete()
    assert sounddevice.calls[2:] == [
        ('start',), ('stop',), ('close',),
    ]


@pytest.mark.parametrize('options', [
    {'frame_length': 0}, {'frame_length': -1}, {'frame_length': True},
    {'frame_length': 1.5}, {'device_index': -2}, {'device_index': True},
    {'device_index': 1.5}, {'device_index': None},
])
def test_invalid_options_fail_before_audio_import(monkeypatch, options):
    """Preserve constructor validation before touching optional audio libraries."""
    monkeypatch.setitem(sys.modules, 'sounddevice', None)
    with pytest.raises(ValueError):
        SoundDeviceRecorder(**options)


def test_overflow_is_reported(sounddevice):
    """An input discontinuity must not be passed off as a valid frame."""
    recorder = SoundDeviceRecorder()
    sounddevice.overflowed = True
    sounddevice.emit()
    with pytest.raises(MicrophoneOverflow, match='microphone input overflow'):
        recorder.read()
    sounddevice.overflowed = False
    sounddevice.emit()
    assert len(recorder.read()) == 512


def test_incomplete_frame_is_reported(sounddevice):
    """Short captures must not reach the fixed-frame dialogue pipeline."""
    recorder = SoundDeviceRecorder()
    sounddevice.samples = [0, 1]
    sounddevice.emit()
    with pytest.raises(RuntimeError, match='microphone returned an incomplete frame'):
        recorder.read()


def test_cancel_releases_reader_without_calling_native_stop(sounddevice):
    recorder = SoundDeviceRecorder()
    entered = Event()
    errors = []

    def read():
        entered.set()
        try:
            recorder.read()
        except RuntimeError as error:
            errors.append(str(error))

    worker = Thread(target=read, daemon=True)
    worker.start()
    assert entered.wait(1)
    recorder.cancel_read()
    worker.join(timeout=0.5)
    assert not worker.is_alive()
    assert errors == ['microphone capture cancelled']
    assert all(call[0] not in ('stop', 'close') for call in sounddevice.calls)


def test_missing_callbacks_time_out_instead_of_blocking_native_read(sounddevice, monkeypatch):
    from malbut_stt.audio import MicrophoneTimeout
    ticks = iter([0.0, 5.1])
    monkeypatch.setattr('malbut_stt.audio.monotonic', lambda: next(ticks))
    recorder = SoundDeviceRecorder()
    with pytest.raises(MicrophoneTimeout):
        recorder.read()


def test_callback_queue_overflow_discards_stale_audio_and_recovers(sounddevice):
    recorder = SoundDeviceRecorder()
    for _ in range(100):
        sounddevice.emit()
    with pytest.raises(MicrophoneOverflow):
        recorder.read()
    sounddevice.samples = [7] * 512
    sounddevice.emit()
    assert recorder.read() == [7] * 512
