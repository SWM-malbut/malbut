"""Observational metrics must preserve model inputs, results and failure behavior."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
import malbut_stt.incremental as incremental
from malbut_stt.incremental import IncrementalWhisperStream


def pcm(seconds):
    return np.ones(round(seconds * 16000), dtype='<i2').tobytes()


def segment(start, end, text):
    return SimpleNamespace(start=start, end=end, text=text)


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.inputs = []

    def transcribe(self, audio, **options):
        self.inputs.append(audio.copy())
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return iter(result), SimpleNamespace(native_decode={'return_code': 0})


def test_success_metrics_are_content_free_and_do_not_change_audio(monkeypatch):
    ticks = iter((10_000_000_000, 10_250_000_000))
    monkeypatch.setattr(incremental, 'monotonic_ns', lambda: next(ticks))
    model = Model([segment(0, 1.5, '보존할 원문')])
    stream = IncrementalWhisperStream(model)
    assert stream.transcribe(pcm(2), 16000, speech_end_s=1.5) == '보존할 원문'
    call, = stream.last_metrics['decode_calls']
    assert call['input_start_s'] == 0 and call['input_end_s'] == 2
    assert call['input_window_s'] == 2 and call['model_elapsed_s'] == .25
    assert call['native_return_code'] == 0 and not call['cancelled']
    assert call['segment_count'] == 1 and call['outcome'] == 'completed'
    assert np.array_equal(model.inputs[0], np.ones(32000, dtype=np.float32) / 32768)
    assert '보존할 원문' not in json.dumps(stream.last_metrics, ensure_ascii=False)


def test_full_fallback_records_both_actual_windows():
    original = [segment(0, 5, '첫째'), segment(5, 7, '둘째')]
    corrected = [segment(0, 5, '첫째'), segment(5, 7, '수정'), segment(7, 9, '셋째')]
    model = Model(original, original, [segment(0, 2, '변경')], corrected)
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(8), 16000)
    stream.transcribe(pcm(9), 16000)
    assert stream.retained_start_s == 5
    assert stream.transcribe(pcm(10), 16000, final=True) == '첫째수정셋째'
    assert stream.last_metrics['fallback_reason'] == 'overlap_changed'
    assert stream.last_metrics['fallback_full']
    calls = stream.last_metrics['decode_calls']
    assert [c['input_start_s'] for c in calls] == [5, 0]
    assert [c['input_window_s'] for c in calls] == [5, 10]
    assert [len(a) / 16000 for a in model.inputs] == [8, 9, 5, 10]


@pytest.mark.parametrize('code,cancelled,deadline', [(-100, True, False), (-101, False, True), (-7, False, False)])
def test_native_failure_is_recorded_without_swallowing_original(code, cancelled, deadline):
    error = RuntimeError('test native error')
    error.native_decode = {'return_code': code}
    stream = IncrementalWhisperStream(Model(error))
    with pytest.raises(RuntimeError) as caught:
        stream.transcribe(pcm(1), 16000)
    assert caught.value is error
    call, = stream.last_metrics['decode_calls']
    assert call['native_return_code'] == code
    assert call['cancelled'] is cancelled and call['deadline_exceeded'] is deadline
    assert call['outcome'] == 'error' and call['error_type'] == 'RuntimeError'


def test_lazy_failure_is_included_in_duration_and_propagated():
    error = ValueError('lazy failure')
    def outputs():
        yield segment(0, .5, '부분')
        raise error
    stream = IncrementalWhisperStream(Model(outputs()))
    with pytest.raises(ValueError) as caught:
        stream.transcribe(pcm(1), 16000)
    assert caught.value is error
    call, = stream.last_metrics['decode_calls']
    assert call['segment_count'] == 1 and call['outcome'] == 'error'
    assert call['native_return_code'] is None and not call['cancelled']


def test_invalid_and_silent_inputs_cannot_reuse_stale_metrics():
    model = Model([segment(0, .5, '처음')])
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(1), 16000)
    with pytest.raises(ValueError):
        stream.transcribe(pcm(1), 24000)
    assert stream.last_metrics == {}
    assert stream.transcribe(bytes(32000), 16000) == ''
    assert stream.last_metrics['decode_calls'] == []
    assert len(model.inputs) == 1


def test_decode_call_records_are_bounded():
    model = Model(*[[segment(0, .5, '내용')]] * 6)
    stream = IncrementalWhisperStream(model)
    for _ in range(6):
        stream._decode(pcm(1), 0)
    assert len(stream.last_metrics['decode_calls']) == 4
    assert stream.last_metrics['decode_calls_truncated'] is True
