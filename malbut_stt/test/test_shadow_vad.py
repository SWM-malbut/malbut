"""Shadow failures and overload must not become microphone or STT failures."""

from contextlib import nullcontext
import hashlib
from threading import Condition, Event, Thread
from types import SimpleNamespace

import pytest

from malbut_stt.incident import IncidentRecorder
from malbut_stt.shadow_vad import ENVIRONMENT_KEY, ShadowDiagnostics, from_environment


class Sink:
    def __init__(self):
        self.condition = Condition()
        self.rows, self.audio_rows = [], []
        self.closed = False

    def event(self, event, **fields):
        with self.condition:
            self.rows.append(dict(event=event, **fields))
            self.condition.notify_all()

    def wait(self, event, count=1):
        with self.condition:
            assert self.condition.wait_for(
                lambda: sum(r['event'] == event for r in self.rows) >= count, timeout=3)
            return [r for r in self.rows if r['event'] == event]

    def audio(self, pcm, **metadata):
        self.audio_rows.append((bytes(pcm), metadata))

    def wrap_vad(self, callback, **kwargs):
        return callback

    def context(self, **metadata):
        return nullcontext()

    def close(self):
        self.closed = True


class Detectors:
    def __init__(self):
        self.pcm, self.webrtc_pcm = [], []
        self.resets = self.webrtc_instances = 0
        self.before_probability = lambda: None

    def reset(self):
        self.resets += 1

    def probability(self, pcm):
        self.before_probability()
        self.pcm.append(pcm)
        return .01

    def make_webrtc(self):
        self.webrtc_instances += 1
        return SimpleNamespace(is_speech=self.is_speech)

    def is_speech(self, pcm, rate):
        assert rate == 16000
        self.webrtc_pcm.append(pcm)
        return True

    def factory(self):
        return self, self.make_webrtc


def test_continuous_frames_keep_exact_samples_without_changing_production_vad():
    sink, detectors = Sink(), Detectors()
    shadow = ShadowDiagnostics(sink, 'unused', detector_factory=detectors.factory)
    pcm = bytes(range(256)) * 8
    try:
        shadow.audio(pcm[:1024], captured_at=1., blocked=True, audio_generation=1)
        sink.wait('shadow_vad')
        shadow.audio(pcm[1024:], captured_at=1.032, blocked=False, audio_generation=2)
        rows = sink.wait('shadow_vad', 2)
        assert b''.join(detectors.pcm) == pcm
        assert b''.join(detectors.webrtc_pcm) == pcm[:1920]
        assert [f['start_sample'] for r in rows for f in r['webrtc_frames']] == [0, 320, 640]
        assert rows[0]['source']['blocked'] and not rows[1]['source']['blocked']
        assert rows[1]['sample_index'] == 512
        assert rows[0]['pcm_sha256'] == hashlib.sha256(pcm[:1024]).hexdigest()
        assert detectors.resets == 0  # Production resets do not break raw microphone continuity.

        def callback(frame, rate):
            return True

        assert shadow.wrap_vad(callback, kind='command') is callback
        assert callback(pcm[:640], 16000) is True and rows[0]['silero_speech'] is False
        with shadow.context(utterance_id='u1'):
            shadow.event('transcript_published', text='네', utterance_id='u1')
        assert sink.rows[-1]['text'] == '네'
    finally:
        shadow.close()
    assert sink.closed


def test_slow_model_drops_only_shadow_frames_and_resets_after_gap():
    sink, detectors, release, loading = Sink(), Detectors(), Event(), Event()

    def factory():
        loading.set()
        assert release.wait(3)
        return detectors.factory()

    shadow = ShadowDiagnostics(sink, 'unused', queue_size=1, detector_factory=factory)
    try:
        assert loading.wait(3)
        first = bytearray(b'\1\0' * 512)
        shadow.audio(first, captured_at=1.)
        first[:] = bytes(1024)  # Queued PCM is an owned immutable copy.
        shadow.audio(bytes(1024), captured_at=1.032)
        shadow.audio(bytes(1024), captured_at=1.064)
        assert len(sink.audio_rows) == 3  # Original recorder still sees every chunk.
        release.set()
        sink.wait('shadow_vad')
        shadow.audio(bytes(1024), captured_at=1.096)
        rows = sink.wait('shadow_vad', 2)
        gap = sink.wait('shadow_vad_gap')[0]
        assert gap['reason'] == 'queue_gap' and gap['missing_samples'] == 1024
        assert gap['dropped_chunks_total'] == 2 and detectors.resets == 1
        assert detectors.webrtc_instances == 2
        assert detectors.pcm[0] == b'\1\0' * 512
        assert rows[1]['webrtc_frames'][0]['start_sample'] == 1536
    finally:
        release.set()
        shadow.close()


@pytest.mark.parametrize('metadata,reason', [
    (dict(captured_at=1.032, overflow=True), 'microphone_overflow'),
    (dict(captured_at=1.5), 'capture_time_gap'),
])
def test_microphone_discontinuity_resets_recurrent_context(metadata, reason):
    sink, detectors = Sink(), Detectors()
    shadow = ShadowDiagnostics(sink, 'unused', detector_factory=detectors.factory)
    try:
        shadow.audio(bytes(1024), captured_at=1.)
        sink.wait('shadow_vad')
        shadow.audio(bytes(1024), **metadata)
        sink.wait('shadow_vad', 2)
        assert sink.wait('shadow_vad_gap')[0]['reason'] == reason
        assert detectors.resets == 1
    finally:
        shadow.close()


@pytest.mark.parametrize('phase', ['load', 'inference'])
def test_observer_failure_does_not_stop_original_diagnostics(phase):
    sink, detectors, errors = Sink(), Detectors(), []

    def fail():
        raise RuntimeError('private detail')

    if phase == 'inference':
        detectors.before_probability = fail
    shadow = ShadowDiagnostics(sink, 'unused', on_error=errors.append,
                               detector_factory=fail if phase == 'load' else detectors.factory)
    try:
        shadow.audio(bytes(1024))
        assert sink.wait('shadow_vad_error')[0]['error'] == 'RuntimeError'
        shadow._thread.join(timeout=3)
        shadow.audio(bytes(1024))
        shadow.event('transcript_published', text='네')
        assert len(sink.audio_rows) == 2 and sink.rows[-1]['text'] == '네'
        assert errors == ['RuntimeError']
        assert 'private detail' not in str(sink.rows)
    finally:
        shadow.close()
        shadow.close()


def test_shutdown_reports_inflight_frame_even_when_queue_is_empty():
    sink, detectors, entered, release = Sink(), Detectors(), Event(), Event()

    def wait():
        entered.set()
        assert release.wait(3)

    detectors.before_probability = wait
    shadow = ShadowDiagnostics(sink, 'unused', detector_factory=detectors.factory)
    closer = Thread(target=shadow.close)
    try:
        shadow.audio(bytes(1024))
        assert entered.wait(3)
        closer.start()
        assert shadow._stop.wait(3)
        release.set()
        closer.join(timeout=3)
        assert not closer.is_alive()
        row = sink.wait('shadow_vad_stopped')[0]
        assert row['pending_chunks'] == 0 and row['unprocessed_chunks'] == 1
        assert row['observed_chunks'] == 1 and row['processed_chunks'] == 0
        assert not row['worker_still_running'] and sink.closed
        assert not any(r['event'] == 'shadow_vad' for r in sink.rows)
    finally:
        release.set()
        shadow.close()


def test_opt_in_and_missing_sink_do_not_load_optional_models(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('model must not be loaded')

    monkeypatch.setattr('malbut_stt.shadow_vad.ShadowDiagnostics', forbidden)
    sink, errors = Sink(), []
    assert from_environment({}, sink) is sink
    assert from_environment({ENVIRONMENT_KEY: 'local.onnx'}, None,
                            on_error=errors.append) is None
    assert errors == ['diagnostics_required']


def test_wrong_weights_are_reported_without_loading_onnx(tmp_path):
    path = tmp_path / 'wrong.onnx'
    path.write_bytes(b'wrong model')
    sink = Sink()
    shadow = ShadowDiagnostics(sink, path)
    try:
        assert sink.wait('shadow_vad_error')[0]['error'] == 'ValueError'
    finally:
        shadow.close()


def test_shadow_evidence_is_saved_in_incident_without_wav(tmp_path):
    import json

    recorder = IncidentRecorder(tmp_path, save_audio=False, exact=['테스트종료'])
    detectors, processed = Detectors(), Event()
    original_event = recorder.event

    def observe(name, **fields):
        original_event(name, **fields)
        if name == 'shadow_vad':
            processed.set()

    recorder.event = observe
    shadow = ShadowDiagnostics(recorder, 'unused', detector_factory=detectors.factory)
    shadow.audio(bytes(1024), captured_at=1., blocked=True)
    assert processed.wait(3)
    shadow.event('transcript_published', text='테스트 종료', utterance_id='test-1')
    shadow.close()
    bundle = next(tmp_path.glob('incident-*'))
    rows = [json.loads(line) for line in (bundle / 'events.jsonl').read_text().splitlines()]
    assert any(r['event'] == 'shadow_vad' for r in rows)
    assert any(r['event'] == 'microphone_chunk' and not r['audio_saved'] for r in rows)
    assert not list(bundle.rglob('*.wav'))
