"""Verify acknowledgement PCM and device lifecycle without opening a speaker."""

from array import array
import hashlib
import sys
from threading import Event, Thread
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_stt.chime import play_endpoint_chime, play_wake_chime


@pytest.fixture
def output(monkeypatch):
    seen = SimpleNamespace(events=[], options=None, pcm=None, fail=None)

    def event(name):
        seen.events.append(name)
        if seen.fail == name:
            raise RuntimeError(name + ' failed')

    class Output:
        def __init__(self, **options):
            seen.options = options
            event('open')

        def start(self):
            event('start')

        def write(self, pcm):
            seen.pcm = pcm
            event('write')

        def stop(self, *, ignore_errors):
            assert ignore_errors is False
            event('stop')

        def close(self, *, ignore_errors):
            assert ignore_errors is False
            event('close')

    seen.stream_type = Output
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(RawOutputStream=Output))
    return seen


@pytest.mark.parametrize('device', [-1, 3])
@pytest.mark.parametrize('play,count,amplitude', [
    (play_wake_chime, 4320, 3900),
    (play_endpoint_chime, 3600, 2600),
])
def test_chime_plays_bounded_pcm_and_drains_before_close(output, device, play, count, amplitude):
    play(device)
    assert output.options == dict(
        device=None if device == -1 else device, channels=1,
        dtype='int16', samplerate=24000,
    )
    pcm = array('h')
    pcm.frombytes(output.pcm)
    assert len(pcm) == count
    assert amplitude * 0.9 < max(pcm) <= amplitude and min(pcm) >= -amplitude
    assert pcm[0] == pcm[-1] == 0
    assert output.events == ['open', 'start', 'write', 'stop', 'close']
    if play is play_wake_chime:
        # Captured before sharing the playback helper: preserve the old tone.
        assert hashlib.sha256(output.pcm).hexdigest() == (
            '77a89abed524cc3fb697fd5df9f59511c1f276f2636a176c217f6a97af39a6d8')
    else:
        # The endpoint acknowledgement is one lower note, not another wake pair.
        spectrum = np.abs(np.fft.rfft(np.asarray(pcm, dtype=float)))
        assert np.argmax(spectrum) * 24000 / len(pcm) == 660


@pytest.mark.parametrize('play', [play_wake_chime, play_endpoint_chime])
@pytest.mark.parametrize('failure,events', [
    ('open', ['open']),
    ('start', ['open', 'start', 'close']),
    ('write', ['open', 'start', 'write', 'close']),
    ('stop', ['open', 'start', 'write', 'stop', 'close']),
    ('close', ['open', 'start', 'write', 'stop', 'close']),
])
def test_audio_failures_propagate_and_opened_streams_are_closed(output, play, failure, events):
    output.fail = failure
    with pytest.raises(RuntimeError, match=failure + ' failed'):
        play()
    assert output.events == events


@pytest.mark.parametrize('play', [play_wake_chime, play_endpoint_chime])
def test_chime_does_not_return_before_output_drain(output, monkeypatch, play):
    entered, release, returned = Event(), Event(), Event()
    original_stop = output.stream_type.stop

    def stop(stream, **options):
        original_stop(stream, **options)
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(output.stream_type, 'stop', stop)

    def run():
        play()
        returned.set()

    worker = Thread(target=run)
    worker.start()
    try:
        assert entered.wait(1)
        assert not returned.is_set() and 'close' not in output.events
    finally:
        release.set()
        worker.join(2)
    assert returned.is_set() and output.events[-1] == 'close'
