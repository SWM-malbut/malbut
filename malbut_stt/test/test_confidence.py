"""Check score-based filtering without models, microphones, or amplitude gates."""

import json
from types import SimpleNamespace

import pytest

from malbut_stt.diagnostics import TranscriptionDiagnostics
from malbut_stt.incremental import IncrementalWhisperStream
from malbut_stt.transcription import LocalWhisperTranscriber


def segment(text, start=0.0, end=0.1, **scores):
    return SimpleNamespace(text=text, start=start, end=end, **scores)


def transcriber_for(segments):
    transcriber = LocalWhisperTranscriber.__new__(LocalWhisperTranscriber)
    transcriber.model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (iter(segments), None))
    return transcriber


@pytest.mark.parametrize('text', ['구독과 좋아요', '어떤 문장이든 같은 기준'])
def test_joint_no_speech_evidence_is_rejected_without_a_phrase_blacklist(text):
    transcriber = transcriber_for([
        segment(text, no_speech_prob=.9, avg_logprob=-2.0),
    ])
    # Nonzero low-amplitude PCM still reaches inference; scores determine rejection.
    assert transcriber.transcribe(b'\x01\x00' * 1600, 16000) == ''


@pytest.mark.parametrize('scores', [
    {}, {'no_speech_prob': .9}, {'avg_logprob': -2.0},
    {'no_speech_prob': .9, 'avg_logprob': -.1},
    {'no_speech_prob': .1, 'avg_logprob': -2.0},
    {'no_speech_prob': .6, 'avg_logprob': -2.0},
    {'no_speech_prob': .9, 'avg_logprob': -1.0},
    {'no_speech_prob': float('nan'), 'avg_logprob': -2.0},
    {'no_speech_prob': 1.1, 'avg_logprob': -2.0},
    {'no_speech_prob': -.1, 'avg_logprob': -2.0},
    {'no_speech_prob': True, 'avg_logprob': -2.0},
    {'no_speech_prob': .9, 'avg_logprob': float('-inf')},
    {'no_speech_prob': .9, 'avg_logprob': '-2.0'},
    {'no_speech_prob': .9, 'avg_logprob': None},
    {'no_speech_prob': .9, 'avg_logprob': True},
    {'no_speech_prob': .9, 'avg_logprob': .1},
    {'no_speech_prob': 10 ** 1000, 'avg_logprob': -2.0},
    {'no_speech_prob': .9, 'avg_logprob': -(10 ** 1000)},
])
def test_short_weak_speech_survives_without_joint_valid_negative_evidence(scores):
    transcriber = transcriber_for([segment('네', **scores)])
    assert transcriber.transcribe(b'\xff\xff' * 1600, 16000) == '네'


def test_mixed_speech_keeps_confident_prefix_and_tail_in_original_order():
    transcriber = transcriber_for([
        segment('앞 문장 ', 0, 1, no_speech_prob=.1, avg_logprob=-.1),
        segment('삽입된 임의 문장 ', 1, 2, no_speech_prob=.95, avg_logprob=-2),
        segment('뒷 문장', 2, 3, no_speech_prob=.1, avg_logprob=-.1),
    ])
    assert transcriber.transcribe(b'\x01\x00' * 48000, 16000) == '앞 문장 뒷 문장'


def test_incremental_filter_preserves_timing_and_monotonic_released_prefix():
    initial = [
        segment('앞 ', 0, 10, no_speech_prob=.1, avg_logprob=-.1),
        segment('삭제할 구간 ', 10, 12, no_speech_prob=.95, avg_logprob=-2),
        segment('뒤 ', 12, 20, no_speech_prob=.1, avg_logprob=-.1),
    ]
    suffix = [
        segment('삭제할 구간 ', 0, 2, no_speech_prob=.95, avg_logprob=-2),
        segment('뒤 ', 2, 10, no_speech_prob=.1, avg_logprob=-.1),
        segment('끝', 10, 12, no_speech_prob=.1, avg_logprob=-.1),
    ]
    responses = iter([initial, initial, suffix, suffix])
    model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (iter(next(responses)), None))
    stream = IncrementalWhisperStream(model)
    pcm = b'\x01\x00' * (16000 * 24)
    assert stream.transcribe(pcm, 16000, speech_end_s=20) == '앞 뒤'
    first = stream.retained_start_s
    assert stream.transcribe(pcm, 16000, speech_end_s=20) == '앞 뒤'
    second = stream.retained_start_s
    assert second == 10
    retained_pcm = pcm[10 * 32000:]
    assert stream.transcribe(retained_pcm, 16000, audio_start_s=10,
                             speech_end_s=22) == '앞 뒤 끝'
    third = stream.retained_start_s
    assert stream.transcribe(retained_pcm, 16000, audio_start_s=10,
                             speech_end_s=22, final=True) == '앞 뒤 끝'
    assert [first, second, third, stream.retained_start_s] == [0, 10, 10, 20]
    assert [(s.start, s.end) for s in stream._committed] == [(0, 10), (12, 20), (20, 22)]


def test_rejected_tail_preserves_current_confident_prefix_with_vad_endpoint():
    transcriber = transcriber_for([
        segment('정상 발화', 0, 1, no_speech_prob=.1, avg_logprob=-.1),
        segment('제외할 끝 구간', 1, 2.8, no_speech_prob=.95, avg_logprob=-2),
    ])
    stream = IncrementalWhisperStream(transcriber.model)
    assert stream.transcribe(b'\x01\x00' * 48000, 16000,
                             speech_end_s=2.8, final=True) == '정상 발화'
    assert stream.last_metrics['tail_gap_s'] == 0
    assert [(s.start, s.end) for s in stream._previous] == [(0, 1)]


def test_rejected_tail_after_released_audio_does_not_commit_suspect_span_or_return_stale_prefix():
    initial = [
        segment('앞 ', 0, 10, no_speech_prob=.1, avg_logprob=-.1),
        segment('뒤 ', 10, 20, no_speech_prob=.1, avg_logprob=-.1),
    ]
    suffix = [
        segment('뒤 ', 0, 10, no_speech_prob=.1, avg_logprob=-.1),
        segment('제외', 10, 13, no_speech_prob=.95, avg_logprob=-2),
    ]
    rejected_only = [segment('제외', 0, 13, no_speech_prob=.95, avg_logprob=-2)]
    responses = iter([initial, initial, suffix, rejected_only])
    model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (iter(next(responses)), None))
    stream = IncrementalWhisperStream(model)
    pcm = b'\x01\x00' * (16000 * 24)
    assert stream.transcribe(pcm, 16000, speech_end_s=20) == '앞 뒤'
    assert stream.transcribe(pcm, 16000, speech_end_s=20) == '앞 뒤'
    assert stream.retained_start_s == 10
    assert stream.transcribe(pcm[10 * 32000:], 16000, audio_start_s=10,
                             speech_end_s=23, final=True) == '앞 뒤'
    assert stream.retained_start_s == 10
    assert [(s.start, s.end) for s in stream._committed] == [(0, 10), (10, 20)]
    assert stream.transcribe(pcm[10 * 32000:], 16000, audio_start_s=10,
                             speech_end_s=23, final=True) == ''


@pytest.mark.parametrize('start,end', [(1, 1.5), (2.5, 2.8),
                                      (1, float('nan')), (1, 20), (2.8, 1)])
def test_rejected_tail_cannot_cover_missing_speech_or_invalid_timestamps(start, end):
    transcriber = transcriber_for([
        segment('예전 발화', 0, 1, no_speech_prob=.1, avg_logprob=-.1),
        segment('제외할 끝 구간', start, end, no_speech_prob=.95, avg_logprob=-2),
    ])
    stream = IncrementalWhisperStream(transcriber.model)
    with pytest.raises(ValueError, match='omitted recent speech'):
        stream.transcribe(b'\x01\x00' * 48000, 16000, speech_end_s=2.8, final=True)


def test_diagnostic_wrapper_preserves_existing_score_only_logger(tmp_path):
    transcriber = transcriber_for([
        segment('제외', no_speech_prob=.9, avg_logprob=-2.0),
    ])
    events = []
    transcriber.model.report_confidence = lambda event, **metadata: events.append((event, metadata))
    diagnostics = TranscriptionDiagnostics(tmp_path)
    transcriber.model = diagnostics.wrap_model(transcriber.model)
    assert transcriber.transcribe(b'\x01\x00' * 1600, 16000) == ''
    assert [event for event, _ in events] == ['confidence_segment_rejected',
                                             'confidence_filter_summary']
    assert events[0][1]['no_speech_prob'] == .9


def test_rejection_diagnostic_contains_scores_and_reason_without_transcript(tmp_path):
    transcriber = transcriber_for([
        segment('PRIVATE-SYNTHETIC-TEXT', no_speech_prob=.9, avg_logprob=-2.0),
    ])
    diagnostics = TranscriptionDiagnostics(tmp_path)
    transcriber.model = diagnostics.wrap_model(transcriber.model)
    assert transcriber.transcribe(b'\x01\x00' * 1600, 16000) == ''
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    rejection, = [event for event in events if event['event'] == 'confidence_segment_rejected']
    assert rejection['reason'] == 'no_speech_and_low_logprob'
    assert rejection['segment_index'] == 0
    assert rejection['no_speech_prob'] == .9 and rejection['avg_logprob'] == -2.0
    assert 'PRIVATE-SYNTHETIC-TEXT' not in json.dumps(rejection)


@pytest.mark.parametrize('scores', [
    {'no_speech_prob': float('nan'), 'avg_logprob': -2.0},
    {'no_speech_prob': .9, 'avg_logprob': float('-inf')},
    {'no_speech_prob': .9},
])
def test_unknown_confidence_is_reported_without_breaking_diagnostics(tmp_path, scores):
    transcriber = transcriber_for([segment('네', **scores)])
    diagnostics = TranscriptionDiagnostics(tmp_path)
    transcriber.model = diagnostics.wrap_model(transcriber.model)
    assert transcriber.transcribe(b'\x01\x00' * 1600, 16000) == '네'
    assert diagnostics.error is None
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    summary, = [event for event in events if event['event'] == 'confidence_filter_summary']
    assert summary['unavailable_segments'] == 1 and summary['scored_segments'] == 0
    assert summary['rejected_segments'] == 0


def test_broken_optional_diagnostic_hook_does_not_change_accepted_or_rejected_text():
    transcriber = transcriber_for([
        segment('네', no_speech_prob=.1, avg_logprob=-.1),
        segment('제외', no_speech_prob=.9, avg_logprob=-2),
    ])

    def fail(*_args, **_kwargs):
        raise OSError('synthetic diagnostic failure')

    transcriber.model.report_confidence = fail
    assert transcriber.transcribe(b'\x01\x00' * 1600, 16000) == '네'
