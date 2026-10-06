"""Diagnostic evidence must reflect real decode input without changing STT."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Barrier
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from malbut_stt.diagnostics import TranscriptionDiagnostics
from malbut_stt.incremental import IncrementalWhisperStream


def records(directory):
    return [json.loads(path.read_text()) for path in sorted(directory.glob('*.json'))]


def saved_pcm(directory, record):
    with wave.open(str(directory / record['wav']), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        return wav.readframes(wav.getnframes())


class Model:
    def __init__(self, segments=()):
        self.segments = segments
        self.calls = []
        self.info = object()

    def transcribe(self, audio, *args, **options):
        self.calls.append((audio, args, options))
        return iter(self.segments), self.info


def test_pcm16_roundtrip_and_raw_segments_preserve_arguments_and_laziness(tmp_path):
    pcm = np.arange(-32768, 32768, dtype='<i2')
    audio = pcm.astype(np.float32) / 32768
    segment = SimpleNamespace(text='  구독과 좋아요! ', start=0.1, end=2.0,
                              no_speech_prob=0.91, avg_logprob=-1.2)
    model = Model([segment])
    diagnostics = TranscriptionDiagnostics(tmp_path)
    wrapped = diagnostics.wrap_model(model)
    with diagnostics.context(kind='command', utterance_id='new', session_id='confirmation',
                             generation=7):
        output, info = wrapped.transcribe(audio, 'positional', initial_prompt=None)
    assert model.calls == [(audio, ('positional',), {'initial_prompt': None})]
    assert info is model.info
    assert not records(tmp_path), 'a lazy result must not be consumed by diagnostics'
    assert list(output) == [segment]
    record, = records(tmp_path)
    assert record['context'] == dict(kind='command', utterance_id='new',
                                     session_id='confirmation', generation=7)
    assert record['segments'] == [vars(segment)]
    assert record['error'] is None and not record['segments_truncated']
    assert saved_pcm(tmp_path, record) == pcm.tobytes()
    assert record['pcm_sha256'] == hashlib.sha256(pcm.tobytes()).hexdigest()
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.iterdir())
    assert wrapped.calls is model.calls


@pytest.mark.parametrize('lazy', [False, True])
def test_original_model_exception_and_partial_results_are_preserved(tmp_path, lazy):
    failure = ValueError('incremental transcription omitted recent speech')
    segment = SimpleNamespace(text=' earlier', start=0.0, end=0.1)

    class FailingModel:
        def transcribe(self, audio, **options):
            if not lazy:
                raise failure

            def result():
                yield segment
                raise failure

            return result(), None

    diagnostics = TranscriptionDiagnostics(tmp_path)
    with diagnostics.context(kind='endpoint', utterance_id='current', generation=2):
        with pytest.raises(ValueError) as caught:
            output, _ = diagnostics.wrap_model(FailingModel()).transcribe(np.full(16, 0.5))
            assert next(output) is segment
            next(output)
    assert caught.value is failure
    record, = records(tmp_path)
    assert record['error'] == dict(type='ValueError', message=str(failure))
    assert record['segments'] == ([vars(segment)] if lazy else [])


def test_quota_includes_previous_runs_and_never_deletes_evidence(tmp_path):
    model = Model()
    first = TranscriptionDiagnostics(tmp_path, max_bytes=66000)
    list(first.wrap_model(model).transcribe(np.zeros(16))[0])
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    second = TranscriptionDiagnostics(tmp_path, max_bytes=66000)
    list(second.wrap_model(model).transcribe(np.zeros(16))[0])
    assert second.error == 'diagnostic_limit_reached'
    assert len(model.calls) == 2
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize('fail_result', [False, True])
def test_storage_failure_does_not_change_model_output(tmp_path, monkeypatch, fail_result):
    segment = SimpleNamespace(text='original', start=0, end=1)
    model = Model([segment])
    diagnostics = TranscriptionDiagnostics(tmp_path)

    original_open = diagnostics._open

    def no_space(name):
        if name.endswith('.json') or not fail_result:
            raise OSError('disk full')
        return original_open(name)

    monkeypatch.setattr(diagnostics, '_open', no_space)
    output, info = diagnostics.wrap_model(model).transcribe(np.zeros(16))
    assert list(output) == [segment] and info is model.info
    assert diagnostics.error == 'OSError: disk full'


def test_lazy_decodes_keep_their_own_thread_context(tmp_path):
    diagnostics = TranscriptionDiagnostics(tmp_path)
    wrapped = diagnostics.wrap_model(Model())
    barrier = Barrier(2)

    def decode(uid):
        with diagnostics.context(utterance_id=uid):
            result, _ = wrapped.transcribe(np.zeros(16))
            barrier.wait(timeout=2)
        list(result)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(decode, ['first', 'second']))
    assert {record['context']['utterance_id'] for record in records(tmp_path)} == {'first', 'second'}


def test_incremental_records_actual_trimmed_model_input_not_entire_capture(tmp_path):
    model = Model([SimpleNamespace(text=' retained', start=0.0, end=1.0)])
    diagnostics = TranscriptionDiagnostics(tmp_path)
    stream = IncrementalWhisperStream(diagnostics.wrap_model(model))
    pcm = np.arange(32000, dtype='<i2').tobytes()
    with diagnostics.context(kind='partial', utterance_id='same-utterance', generation=3):
        stream._decode(pcm, offset_samples=16000)
    record, = records(tmp_path)
    assert saved_pcm(tmp_path, record) == pcm[32000:]
    assert record['sample_count'] == 16000
    assert record['options']['initial_prompt'] is None


def test_non_pcm16_input_disables_diagnostics_without_quantizing_model_input(tmp_path):
    model = Model()
    diagnostics = TranscriptionDiagnostics(tmp_path)
    audio = np.array([0.123456], dtype=np.float32)
    list(diagnostics.wrap_model(model).transcribe(audio)[0])
    assert model.calls[0][0] is audio
    assert 'losslessly' in diagnostics.error
    assert not list(tmp_path.iterdir())


def test_pipeline_events_share_quota_with_decode_files_and_previous_runs(tmp_path):
    diagnostics = TranscriptionDiagnostics(tmp_path, max_bytes=66000)
    with diagnostics.context(kind='command', utterance_id='current', generation=4):
        list(diagnostics.wrap_model(Model()).transcribe(np.zeros(16))[0])
        diagnostics.event('inference_result', text='raw result', error=None)
    event, = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    assert event['event'] == 'inference_result' and event['text'] == 'raw result'
    assert event['context'] == dict(kind='command', utterance_id='current', generation=4)
    used = sum(path.stat().st_size for path in tmp_path.iterdir())
    assert diagnostics._used == used
    previous = (tmp_path / 'events.jsonl').read_bytes()
    restarted = TranscriptionDiagnostics(tmp_path, max_bytes=used + 1)
    restarted.event('speech_started')
    assert restarted.error == 'diagnostic_limit_reached'
    assert (tmp_path / 'events.jsonl').read_bytes() == previous
    assert restarted._used == used


def test_event_storage_errors_never_escape_to_pipeline(tmp_path, monkeypatch):
    diagnostics = TranscriptionDiagnostics(tmp_path)

    def no_space(*args):
        raise OSError('disk full')

    monkeypatch.setattr('malbut_stt.diagnostics.os.open', no_space)
    diagnostics.event('speech_started', utterance_id='new')
    assert diagnostics.error == 'OSError: disk full'
