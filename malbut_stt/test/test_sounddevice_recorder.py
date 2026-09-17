"""Verify PortAudio device IDs and PCM capture without opening real hardware."""

import struct
import sys
from types import SimpleNamespace

import pytest

from malbut_stt.audio import SoundDeviceRecorder


@pytest.fixture
def sounddevice(monkeypatch):
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
            return devices[4 if device is None else device]
        return devices

    def check_input_settings(**options):
        state.calls.append(('check', options))
        device = options['device']
        if device is not None and (
                device >= len(devices) or not devices[device]['max_input_channels']):
            raise RuntimeError('invalid input device')

    class Stream:
        def __init__(self, **options):
            state.calls.append(('open', options))

        def start(self):
            state.calls.append(('start',))

        def read(self, frame_length):
            state.calls.append(('read', frame_length))
            samples = state.samples
            if samples is None:
                samples = [-32768, -1, 0, 1, 32767] + [0] * (frame_length - 5)
            return struct.pack(f'<{len(samples)}h', *samples), state.overflowed

        def stop(self):
            state.calls.append(('stop',))

        def close(self):
            state.calls.append(('close',))

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
    assert recorder.read() == [-32768, -1, 0, 1, 32767] + [0] * (frame_length - 5)
    recorder.stop()
    recorder.delete()
    assert sounddevice.calls[2:] == [
        ('start',), ('read', frame_length), ('stop',), ('close',),
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
    with pytest.raises(RuntimeError, match='microphone input overflow'):
        recorder.read()


def test_incomplete_frame_is_reported(sounddevice):
    """Short captures must not reach the fixed-frame dialogue pipeline."""
    recorder = SoundDeviceRecorder()
    sounddevice.samples = [0, 1]
    with pytest.raises(RuntimeError, match='microphone returned an incomplete frame'):
        recorder.read()
