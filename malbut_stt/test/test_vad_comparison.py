"""Check experiment integrity without downloading models or opening devices."""

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest


def load(name):
    path = Path(__file__).resolve().parents[1] / 'tools' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = load('compare_vad')
audit = load('audit_native_decisions')


def wav(path, pcm, rate=16000):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(pcm)
    return path


def frame(start, end, speech):
    return dict(start_sample=start, end_sample=end, speech=speech)


def test_gate_needs_contiguous_speech_and_does_not_count_padding():
    frames = [frame(0, 960, True), frame(960, 1280, False), frame(1280, 2240, True)]
    summary = compare.summarize_frames(frames, 80)
    assert summary['voiced_s'] == .12 and not summary['gate_pass']
    assert not compare.summarize_frames([frame(0, 1279, True)], 80)['gate_pass']
    assert compare.summarize_frames([frame(0, 1280, True)], 80)['gate_pass']


def test_silero_context_and_recurrent_state_reset_between_cases(monkeypatch):
    calls = []

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        def get_inputs(self):
            return [SimpleNamespace(name=n) for n in ('input', 'state', 'sr')]

        def run(self, outputs, inputs):
            calls.append({k: v.copy() for k, v in inputs.items()})
            return np.array([[.9]], dtype=np.float32), inputs['state'] + 1

    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(
        SessionOptions=SimpleNamespace, InferenceSession=Session))
    detector = compare.SileroDetector('local.onnx')
    pcm = (np.arange(600) + 1).astype('<i2').tobytes()
    first = detector.frames(pcm)
    assert first[-1]['end_sample'] == 600
    assert np.all(calls[0]['state'] == 0) and np.all(calls[0]['input'][0, :64] == 0)
    assert np.all(calls[1]['state'] == 1)
    np.testing.assert_array_equal(calls[1]['input'][0, :64], calls[0]['input'][0, -64:])
    assert np.all(calls[1]['input'][0, 64 + 88:] == 0)
    assert detector.frames(pcm) == first
    assert np.all(calls[2]['state'] == 0) and np.all(calls[2]['input'][0, :64] == 0)


def test_webrtc_state_is_not_shared_and_partial_frame_stays_input_bounded(monkeypatch):
    models, calls = [], []

    def create(mode):
        models.append(mode)
        return SimpleNamespace(is_speech=lambda pcm, rate: calls.append((pcm, rate)) or True)

    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(Vad=create))
    detector = compare.WebRtcDetector()
    result = detector.frames(bytes(642))
    detector.frames(bytes(640))
    assert models == [2, 2]
    assert result[-1]['end_sample'] == 321
    assert len(calls[1][0]) == 640


@pytest.mark.parametrize('change', ['label', 'checksum', 'duplicate', 'format', 'truncated'])
def test_invalid_corpus_is_rejected_before_models_are_used(tmp_path, change):
    path = wav(tmp_path / 'sample.wav', bytes(640), 8000 if change == 'format' else 16000)
    case = dict(id='one', wav='sample.wav', label='speech')
    if change == 'label':
        case['label'] = 'silence_probably'
    if change == 'checksum':
        case['pcm_sha256'] = 'wrong'
    if change == 'truncated':
        path.write_bytes(path.read_bytes()[:-2])
    manifest = tmp_path / 'cases.json'
    manifest.write_text(json.dumps([case, case] if change == 'duplicate' else [case]))
    with pytest.raises(ValueError):
        compare.load_cases(manifest)


def test_whisper_gets_same_full_input_once_and_gate_only_changes_admission():
    calls = []

    def transcribe(audio, **options):
        calls.append((audio.copy(), options))
        return iter([SimpleNamespace(text=' 네', start=0., end=.1)]), SimpleNamespace(
            native_decode=dict(status='observed', windows=[]))

    pcm = (np.arange(1600) - 800).astype('<i2').tobytes()
    result = compare.compare_case(
        dict(id='one', label='unknown'), pcm,
        dict(webrtc=SimpleNamespace(frames=lambda pcm: [frame(0, 1600, True)]),
             silero=SimpleNamespace(frames=lambda pcm: [frame(0, 1600, False)])),
        transcriber=SimpleNamespace(model=SimpleNamespace(transcribe=transcribe)))
    assert len(calls) == 1
    np.testing.assert_array_equal(
        calls[0][0], np.frombuffer(pcm, '<i2').astype(np.float32) / 32768)
    assert calls[0][1]['initial_prompt'] is None
    assert result['vad']['webrtc']['would_emit_nonempty_text'] is True
    assert result['vad']['silero']['would_emit_nonempty_text'] is False
    summary = compare.summarize_results([result])
    assert summary['webrtc']['non_speech_admitted'] == 0
    assert summary['silero']['speech_rejected'] == 0
    assert summary['silero']['unknown_cases'] == 1


def test_decoder_error_is_not_counted_as_successful_silence():
    def fail(*args, **kwargs):
        raise TimeoutError('deadline')

    result = compare.compare_case(
        dict(id='one', label='non_speech'), bytes(3200),
        {name: SimpleNamespace(frames=lambda pcm: [frame(0, 1600, True)])
         for name in ('webrtc', 'silero')},
        transcriber=SimpleNamespace(model=SimpleNamespace(transcribe=fail)))
    assert result['raw_whisper']['status'] == 'error'
    assert result['vad']['silero']['would_emit_nonempty_text'] is None
    assert compare.summarize_results([result])['silero']['whisper_evaluated_cases'] == 0


def incident(path, events):
    path.mkdir()
    (path / 'incident.json').write_text(json.dumps(dict(audio_saved=False, triggers=[])))
    (path / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\n')


def test_audit_deduplicates_windows_and_explains_combined_gate(tmp_path):
    window = dict(no_speech_prob=.000001, avg_logprobs=-1.2, no_speech_threshold=.6,
                  logprob_threshold=-1, skipped_no_speech=False)
    event = dict(event='decode_result', decode_id='same', wall_ns=1, sample_count=16000,
                 native_decode=dict(status='observed', windows=[window]), segments=[])
    incident(tmp_path / 'one', [event])
    incident(tmp_path / 'two', [event])
    report = audit.audit(tmp_path)
    assert report['summary']['unique_decodes'] == 1
    assert report['summary']['decision_mismatches'] == 0
    row, = report['windows']
    assert not row['probability_condition'] and row['score_condition']
    assert row['expected_skip'] is False and row['decision_matches'] is True


def test_audit_reports_unavailable_nonfinite_and_inconsistent_decisions(tmp_path):
    window = dict(no_speech_prob=.9, avg_logprobs=-1.2, no_speech_threshold=.6,
                  logprob_threshold=-1, skipped_no_speech=False)
    events = [dict(event='decode_result', decode_id='old', wall_ns=1, segments=[]),
              dict(event='decode_result', decode_id='new', wall_ns=2, segments=[],
                   native_decode=dict(status='observed', windows=[
                       window, dict(window, no_speech_prob=None)]))]
    incident(tmp_path / 'one', events)
    report = audit.audit(tmp_path)
    assert report['summary']['unavailable_decodes'] == 1
    assert report['summary']['decision_mismatches'] == 1
    assert report['summary']['uncheckable_windows'] == 1
