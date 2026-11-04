"""Correct floating-point boundary noise without widening missing-speech policy."""
from types import SimpleNamespace
import numpy as np
import pytest
from malbut_stt.incremental import IncrementalWhisperStream, _tail_gap_exceeds


def pcm(seconds):
    return np.ones(round(seconds*16000), dtype='<i2').tobytes()


class Model:
    def __init__(self, *ends):
        self.ends = iter(ends)
        self.calls = []

    def transcribe(self, audio, **options):
        self.calls.append(audio.copy())
        return iter([SimpleNamespace(start=0., end=next(self.ends), text='말끝까지 같은 문장')]), None


@pytest.mark.parametrize('final', [False, True])
def test_exact_eight_tenths_subtraction_noise_remains_allowed(final):
    assert 22.94 - 22.14 == 0.8000000000000007
    model = Model(22.14)
    stream = IncrementalWhisperStream(model)
    assert stream.transcribe(pcm(24), 16000, speech_end_s=22.94, final=final) == '말끝까지 같은 문장'
    assert stream.last_metrics['tail_gap_s'] == 0.8000000000000007
    assert len(model.calls) == 1 and not stream.last_metrics['fallback_full']


@pytest.mark.parametrize('extra', [1e-8, 1/16000, .01, .2])
def test_actual_extra_tail_still_rejected(extra):
    stream = IncrementalWhisperStream(Model(22.14))
    with pytest.raises(ValueError, match='omitted recent speech'):
        stream.transcribe(pcm(24), 16000, speech_end_s=22.94+extra)
    assert len(stream.last_metrics['decode_calls']) == 1


def test_prior_coverage_uses_same_boundary_without_extending_its_proof():
    model = Model(22.14, 21., 21.)
    stream = IncrementalWhisperStream(model)
    expected = stream.transcribe(pcm(23), 16000, speech_end_s=22.14)
    proof = stream._untrimmed_coverage
    assert stream.transcribe(pcm(24), 16000, speech_end_s=22.94) == expected
    assert stream.last_metrics['reused_tail_coverage_end_s'] == 22.14
    assert stream._untrimmed_coverage == proof
    assert stream.retained_start_s == 0 and stream.last_metrics['committed_s'] == 0
    with pytest.raises(ValueError, match='omitted recent speech'):
        stream.transcribe(pcm(24), 16000, speech_end_s=22.94+1/16000)
    assert stream._untrimmed_coverage == proof


@pytest.mark.parametrize('maximum', [.8, 3.])
def test_comparison_uses_absolute_not_relative_tolerance(maximum):
    assert not _tail_gap_exceeds(maximum, maximum)
    assert not _tail_gap_exceeds(maximum+1e-12, maximum)
    assert _tail_gap_exceeds(maximum+1e-8, maximum)
    assert _tail_gap_exceeds(maximum+1/16000, maximum)
