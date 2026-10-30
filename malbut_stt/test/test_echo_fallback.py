"""No unfiltered audio may reach a duplex dialogue after AEC failure."""
from queue import Empty, Queue
from time import monotonic
from types import SimpleNamespace

import pytest

from malbut_stt.echo.recorder import EchoCancelledRecorder, IsolatedMicrophoneRecorder
from malbut_stt.echo.vad import SuppressedSilenceVad
from malbut_stt.echo.worker import track_capture_clock
from malbut_stt.audio import CapturePCM, CaptureSettings
from malbut_stt.pipeline import pcm_bytes
from malbut_stt.streaming import StreamingUtteranceCollector
from test_dialogue_pipeline import harness, VOICE


@pytest.mark.parametrize('message', [
    ('error', 'reference disappeared', 0),
    ('audio', [99] * 512, monotonic() - 100),
])
def test_raw_fallback_requires_owner_acknowledgement(message):
    calls = []
    raw = SimpleNamespace(start=lambda: calls.append('start'),
                          read=lambda: [7] * 512,
                          stop=lambda: calls.append('stop'),
                          delete=lambda: calls.append('delete'))
    recorder = EchoCancelledRecorder({}, lambda: raw)
    recorder._queue = Queue()
    recorder._queue.put(message)
    recorder.aec_ready = True
    assert recorder.read() == [0] * 512
    assert not recorder.aec_ready
    assert not calls
    assert recorder.read() == [0] * 512
    recorder.acknowledge_half_duplex()
    assert recorder.read() == [7] * 512
    assert calls == ['start']
    recorder.stop()
    assert recorder.read() == [0] * 512


def test_fallback_keeps_reply_correlation_and_restores_volume(harness):
    harness.recorder.aec_ready = True
    acknowledged = []
    harness.recorder.acknowledge_half_duplex = lambda: acknowledged.append(
        (p.input_has_aec, p._input_blocked(harness.now)))
    p = harness.create(barge_in_mode='reachy')
    p._publish_transcript('request', '답변을 기다림')
    p.on_playback_status('answer', 'playing', request_id='request')
    p._ducked_playback = 'answer'
    harness.recorder.aec_ready = False
    # Even before poll serializes the state change, the capture thread gates it.
    assert p._input_blocked(harness.now)
    p.poll()
    assert acknowledged == [(False, True)]
    assert p.barge_in_mode == 'legacy'
    assert p._reply_request_id == 'request'
    assert ('answer', 'unduck') in harness.controls
    for _ in range(20):
        p.feed(VOICE)
    assert ('answer', 'stop') not in harness.controls
    p.on_playback_status('answer', 'finished', request_id='request')
    assert p._reply_request_id is None
    assert p.session.active  # The ordinary follow-up window still works.
    assert p._input_blocked(harness.now + 0.9)
    assert not p._input_blocked(harness.now + 1.1)


def test_failed_startup_closes_duplex_before_capture_starts(harness):
    harness.recorder.aec_ready = False
    acknowledged = []
    harness.recorder.acknowledge_half_duplex = lambda: acknowledged.append(True)
    p = harness.create(barge_in_mode='reachy')
    assert not p.input_has_aec
    assert p.barge_in_mode == 'legacy'
    assert acknowledged == [True]
    p.poll()
    assert acknowledged == [True]


def test_intentional_shutdown_does_not_report_a_worker_failure():
    recorder = EchoCancelledRecorder({}, lambda: None)
    def read_during_close(**_):
        recorder._closed.set()
        raise Empty()
    recorder._queue = SimpleNamespace(get=read_during_close)
    recorder.aec_ready = True
    assert recorder.read() == [0] * 512
    assert recorder.failure is None


def test_isolated_raw_failure_is_not_mistaken_for_a_working_silent_microphone():
    raw = IsolatedMicrophoneRecorder('test-source')
    raw._capture._queue = Queue()
    raw._capture._queue.put(('error', 'device removed', 0))
    raw._capture.aec_ready = True
    with pytest.raises(RuntimeError, match='device removed'):
        raw.read()
    raw.stop()
    assert raw.read() == [0] * 512


def test_suppressed_silence_cannot_inherit_voice_and_does_not_spend_idle_inference():
    calls = []
    underlying = SimpleNamespace(is_speech=lambda *args: calls.append('infer') or True,
                                 reset=lambda: calls.append('reset'))
    vad = SuppressedSilenceVad(underlying)
    assert vad.is_speech(VOICE, 16000)
    for _ in range(100):
        assert not vad.is_speech(bytes(640), 16000)
    assert calls.count('reset') == 1
    assert calls.count('infer') == 25  # Voice plus the first 480 ms of silence.
    assert vad.is_speech(VOICE, 16000)
    assert calls[-1] == 'infer'
    vad.reset()
    assert vad.quiet_samples == 0
    assert calls[-1] == 'reset'


def test_slow_independent_device_clock_drift_does_not_disable_barge_in():
    anchors = {'mic': 100.0, 'ref': 100.0}
    for second in range(1, 241):
        track_capture_clock(anchors, second * 0.0005)
    assert anchors['mic'] - anchors['ref'] == pytest.approx(0.12)
    with pytest.raises(RuntimeError, match='clock changed'):
        track_capture_clock(anchors, 0.20)


def test_echo_mask_controls_onset_without_erasing_recognition_samples():
    raw = b''.join((i + 1).to_bytes(2, 'little') * 320 for i in range(33))
    mask = bytes(5 * 640) + VOICE * 8 + bytes(20 * 640)
    collector = StreamingUtteranceCollector(
        lambda frame, rate: any(frame), settings=CaptureSettings(
            min_speech_s=0.16, silence_timeout_s=0.4))
    events = []
    start = 0
    sizes = [254, 1024, 640, 128]
    while start < len(raw):
        end = min(len(raw), start + sizes[(start // 2) % len(sizes)])
        frame = CapturePCM(raw[start:end], mask[start:end])
        assert pcm_bytes(frame) is frame
        events.extend(collector.feed(frame))
        start = end
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[-1].pcm == raw
    assert events[-1].voiced_s == pytest.approx(0.16)


def test_reset_discards_both_partial_audio_clocks():
    collector = StreamingUtteranceCollector(lambda *_: False)
    collector.feed(CapturePCM(b'\x01\x00' * 100, bytes(200)))
    collector.reset()
    assert not collector.pending
    assert not collector.pending_vad
    with pytest.raises(ValueError, match='identical sample clocks'):
        CapturePCM(bytes(640), bytes(642))
