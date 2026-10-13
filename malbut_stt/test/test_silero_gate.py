"""Rejected, failed and cancelled admission must never invoke Whisper."""

from contextlib import nullcontext
import hashlib
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.incremental import IncrementalWhisperStream
from malbut_stt.silero_gate import (
    SileroGate, SileroGateCancelled, SileroGateError, SileroNonSpeech, from_environment,
)
from malbut_stt.transcription import LocalWhisperTranscriber
from malbut_stt.wake import LocalWakeRecognizer


VOICE = b'\1\0' * 320
QUIET = bytes(640)


class Scores:
    def __init__(self, score=.01):
        self.score = score
        self.resets = 0

    def reset(self):
        self.resets += 1

    def probability(self, pcm):
        return self.score


class Sink:
    error = None

    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append(dict(event=name, **fields))

    def context(self, **fields):
        return nullcontext()


@pytest.fixture
def gate(monkeypatch):
    scores = Scores()
    monkeypatch.setattr('malbut_stt.silero_gate.load_pinned_model', lambda _: scores)
    return SileroGate('local.onnx')


@pytest.mark.parametrize('samples,allowed', [(0, False), (1279, False), (1280, True)])
def test_only_real_consecutive_samples_satisfy_gate(gate, samples, allowed):
    gate.model.score = .5
    pcm = b'\1\0' * samples
    result = gate.check(pcm)
    assert result['allowed'] is allowed
    assert result['scanned_samples'] == samples
    assert result['longest_speech_s'] == samples / 16000
    assert result['pcm_sha256'] == hashlib.sha256(pcm).hexdigest()


def test_gaps_break_runs_and_each_decode_resets_model(gate):
    for _ in range(2):
        scores = iter([.9, .9, .01, .9, .9])
        gate.model.probability = lambda _: next(scores)
        assert not gate.check(VOICE * 8)['allowed']
    assert gate.model.resets == 2


def test_model_gets_original_audio_and_options_only_after_allow(gate):
    sink, calls = Sink(), []
    result = (iter(()), SimpleNamespace())
    model = gate.wrap_model(SimpleNamespace(
        transcribe=lambda audio, **kw: calls.append((audio, kw)) or result), sink)
    audio = np.ones(1600, dtype=np.float32) / 32768
    with pytest.raises(SileroNonSpeech):
        model.transcribe(audio, initial_prompt='제이크')
    assert calls == [] and sink.events[-1]['allowed'] is False
    gate.model.score = .9
    assert model.transcribe(audio, initial_prompt='제이크') is result
    assert calls[0][0] is audio and calls[0][1] == {'initial_prompt': '제이크'}
    assert sink.events[-1]['allowed'] is True


@pytest.mark.parametrize('audio', [np.array([np.nan]), np.zeros((1, 512)), np.array([.123456])])
def test_unrepresentable_audio_is_rejected_before_model(gate, audio):
    model = gate.wrap_model(SimpleNamespace(transcribe=lambda *_: pytest.fail('Whisper called')))
    with pytest.raises(SileroGateError):
        model.transcribe(audio)


def test_gate_error_does_not_fall_back_to_whisper_and_next_input_can_recover(gate):
    def fail(_):
        raise RuntimeError('private model detail')

    sink = Sink()
    model = gate.wrap_model(SimpleNamespace(transcribe=lambda *_: 'decoded'), sink)
    gate.model.probability = fail
    audio = np.ones(1600, dtype=np.float32) / 32768
    with pytest.raises(SileroGateError, match='silero_gate_unavailable'):
        model.transcribe(audio)
    assert sink.events == [dict(event='silero_gate_error', error='RuntimeError', allowed=False)]
    gate.model.probability = lambda _: .9
    assert model.transcribe(audio) == 'decoded'


@pytest.mark.parametrize('action', ['cancel', 'close'])
def test_cancellation_during_silero_cannot_start_whisper(gate, action):
    entered, release, errors = Event(), Event(), []

    def probability(_):
        entered.set()
        assert release.wait(3)
        return .9

    gate.model.probability = probability
    calls = []
    model = gate.wrap_model(SimpleNamespace(
        transcribe=lambda *_: calls.append('Whisper'), cancel=lambda: calls.append('cancel'),
        close=lambda: calls.append('close')))

    def run():
        try:
            model.transcribe(np.ones(1600, dtype=np.float32) / 32768)
        except Exception as error:
            errors.append(error)

    worker = Thread(target=run)
    worker.start()
    try:
        assert entered.wait(3)
        getattr(model, action)()
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive() and isinstance(errors[0], SileroGateCancelled)
    assert 'Whisper' not in calls and 'cancel' in calls
    if action == 'cancel':
        model.transcribe(np.ones(1600, dtype=np.float32) / 32768)
        assert calls[-1] == 'Whisper'


def test_incremental_checks_actual_cropped_suffix_not_old_speech(gate):
    gate.model.probability = lambda pcm: .9 if any(pcm) else .01
    calls = []
    stream = IncrementalWhisperStream(gate.wrap_model(SimpleNamespace(
        transcribe=lambda *a, **kw: calls.append(a))))
    # A prior committed prefix is still in the caller's snapshot, but Whisper
    # would only decode the retained suffix. Prefix speech must not admit it.
    stream._offset_samples = 1600
    with pytest.raises(SileroNonSpeech):
        stream.transcribe(VOICE * 5 + QUIET * 10, 16000, audio_start_s=0.)
    assert calls == [] and stream.retained_start_s == .1


def test_gate_outside_diagnostics_has_no_false_decode_record(gate, tmp_path):
    from malbut_stt.diagnostics import TranscriptionDiagnostics

    diagnostics = TranscriptionDiagnostics(tmp_path)
    inner = diagnostics.wrap_model(SimpleNamespace(
        transcribe=lambda *a, **kw: pytest.fail('Whisper called')))
    with pytest.raises(SileroNonSpeech):
        gate.wrap_model(inner, diagnostics).transcribe(np.ones(1600, dtype=np.float32) / 32768)
    assert not list(tmp_path.glob('decode-*')) and not list(tmp_path.glob('*.wav'))
    assert 'silero_gate' in (tmp_path / 'events.jsonl').read_text()


def test_uninstalled_model_does_not_load_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr('malbut_stt.silero_gate.SileroGate', lambda _: pytest.fail('loaded'))
    assert from_environment({'XDG_CACHE_HOME': str(tmp_path)}) is None


@pytest.mark.parametrize('custom_cache', [False, True])
def test_installed_model_loads_without_shell_configuration(monkeypatch, tmp_path, custom_cache):
    monkeypatch.setenv('HOME', str(tmp_path))
    cache = tmp_path / ('custom cache' if custom_cache else '.cache')
    model = cache / 'malbut_speech/models/silero-v6.2.3.onnx'
    model.parent.mkdir(parents=True)
    model.touch()
    loaded = []
    sentinel = object()
    monkeypatch.setattr('malbut_stt.silero_gate.SileroGate',
                        lambda path: loaded.append(path) or sentinel)
    environ = {'XDG_CACHE_HOME': str(cache)} if custom_cache else {}
    assert from_environment(environ) is sentinel
    assert loaded == [str(model)]


@pytest.mark.parametrize('override', ['', '  ', '/explicit/silero.onnx'])
def test_explicit_override_takes_precedence_over_installed_model(
        monkeypatch, tmp_path, override):
    model = tmp_path / 'malbut_speech/models/silero-v6.2.3.onnx'
    model.parent.mkdir(parents=True)
    model.touch()
    loaded = []
    sentinel = object()
    monkeypatch.setattr('malbut_stt.silero_gate.SileroGate',
                        lambda path: loaded.append(path) or sentinel)
    result = from_environment({
        'XDG_CACHE_HOME': str(tmp_path), 'MALBUT_STT_SILERO_GATE_MODEL': override})
    assert result is (sentinel if override.strip() else None)
    assert loaded == ([override] if override.strip() else [])


def test_corrupt_installed_model_does_not_silently_disable_gate(tmp_path):
    model = tmp_path / 'malbut_speech/models/silero-v6.2.3.onnx'
    model.parent.mkdir(parents=True)
    model.write_bytes(b'corrupt model')
    with pytest.raises(ValueError, match='checksum mismatch'):
        from_environment({'XDG_CACHE_HOME': str(tmp_path)})


@pytest.fixture
def pipeline(gate):
    states = []

    def create(*, active=True, text='네', streaming=True):
        state = SimpleNamespace(
            now=0., calls=[], transcripts=[], partials=[], reports=[], sink=Sink())

        def decode(audio, **options):
            state.calls.append((audio.copy(), options))
            return iter([SimpleNamespace(text=text, start=0., end=.08)]), SimpleNamespace()

        transcriber = LocalWhisperTranscriber.__new__(LocalWhisperTranscriber)
        transcriber.model = SimpleNamespace(transcribe=decode)
        wake = LocalWakeRecognizer.from_transcriber(transcriber)
        transcriber.model = gate.wrap_model(transcriber.model, state.sink)
        if not streaming:
            transcriber.create_stream = None
        p = DialoguePipeline(
            recorder_factory=lambda: None, wake=wake,
            transcriber=transcriber, is_speech=lambda frame, _: frame == VOICE,
            publish_transcript=lambda *args: state.transcripts.append(args),
            publish_control=lambda *_: None, publish_interruption=lambda *_: None,
            report=state.reports.append, on_partial=lambda *args: state.partials.append(args),
            diagnostics=state.sink, on_wake=lambda: None, on_failure=lambda: None,
            clock=lambda: state.now, endpoint_predecode_s=.8,
        )
        if active:
            p.session.activate()
        p.asr_thread = Thread(target=p._infer, daemon=True)
        p.asr_thread.start()
        state.pipeline = p
        states.append(state)
        return state

    yield create
    for state in states:
        state.pipeline.close()


def pump(state, predicate):
    deadline = monotonic() + 3
    while not predicate():
        state.pipeline.poll()
        assert monotonic() < deadline
        sleep(.001)


@pytest.mark.parametrize('streaming', [False, True])
def test_nonspeech_endpoint_and_final_release_busy_without_decoding(pipeline, streaming):
    state = pipeline(streaming=streaming, text='구독과 좋아요 부탁드려요!')
    p = state.pipeline
    p.feed(VOICE * 14 + QUIET * 40)
    pump(state, lambda: 'partial_failed' in state.reports)
    p.feed(QUIET * 60)
    pump(state, lambda: 'transcription_failed:SileroNonSpeech' in state.reports)
    assert not state.calls and not state.transcripts and not state.partials
    assert not p._busy and p._endpoint_job is None and not p.session.active
    assert p.start_session('next-turn')


def test_wake_bypasses_silero_but_next_command_is_still_gated(pipeline, gate):
    state = pipeline(active=False, text='제이크야')
    state.pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(state, lambda: state.pipeline.session.active)
    assert len(state.calls) == 1 and gate.model.resets == 0
    assert state.calls[0][1]['initial_prompt'] == '로봇 이름은 제이크입니다.'
    assert not any(row['event'].startswith('silero_gate') for row in state.sink.events)
    state.now = .31  # Existing acknowledgement tail has ended.
    state.pipeline.feed(VOICE * 4 + QUIET * 100)
    pump(state, lambda: 'transcription_failed:SileroNonSpeech' in state.reports)
    assert len(state.calls) == 1 and gate.model.resets > 0
    assert not state.transcripts and not state.pipeline.session.active


@pytest.mark.parametrize('text', ['네', '아니요', '구독과 좋아요 부탁드려요!'])
def test_positive_short_input_reaches_whisper_and_publication_without_text_blacklist(
        pipeline, gate, text):
    gate.model.probability = lambda pcm: .9 if any(pcm) else .01
    state = pipeline(text=text)
    state.pipeline.feed(VOICE * 4 + QUIET * 100)
    pump(state, lambda: bool(state.transcripts))
    assert state.transcripts[0][1] == text and len(state.calls) == 1


@pytest.mark.parametrize('streaming', [False, True])
def test_followup_nonspeech_is_gated_before_whisper(pipeline, gate, streaming):
    gate.model.probability = lambda pcm: .9 if any(pcm) else .01
    state = pipeline(streaming=streaming)
    state.pipeline.feed(VOICE * 4 + QUIET * 100)
    pump(state, lambda: bool(state.transcripts))
    uid = state.transcripts[0][0]
    state.pipeline.on_playback_status('reply', 'finished', request_id=uid)
    assert state.pipeline.session.active
    decoded = len(state.calls)
    gate.model.probability = lambda pcm: .01
    state.pipeline.feed(VOICE * 4 + QUIET * 100)
    pump(state, lambda: 'transcription_failed:SileroNonSpeech' in state.reports)
    assert len(state.calls) == decoded and len(state.transcripts) == 1
    assert not state.pipeline.session.active


def test_nonspeech_partial_cannot_publish_preview(pipeline):
    state = pipeline()
    state.pipeline.feed(VOICE * 125)
    pump(state, lambda: 'partial_failed' in state.reports)
    assert not state.calls and not state.partials and not state.transcripts
    assert state.pipeline._endpoint_job is None
