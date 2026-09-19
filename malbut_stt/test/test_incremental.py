"""Incremental decoding retains current audio and trims only stable boundaries."""

from types import SimpleNamespace

import numpy as np
import pytest

from malbut_stt.incremental import IncrementalWhisperStream
from malbut_stt.transcription import LocalWhisperTranscriber


def pcm(seconds):
    values = np.ones(int(seconds * 16000), dtype='<i2')
    values[-1] = 1234
    return values.tobytes()


def segments(count, *, first=0, offset=0.0, edits=None):
    edits = edits or {}
    words = [SimpleNamespace(
        start=index * 2 + 0.2 - offset, end=index * 2 + 1.5 - offset,
        word=' ' + edits.get(index, f'단어{index}'),
    ) for index in range(first, count)]
    return [SimpleNamespace(text=''.join(word.word for word in pair),
                            start=pair[0].start, end=pair[-1].end)
            for start in range(0, len(words), 2) if (pair := words[start:start + 2])]


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def transcribe(self, audio, **options):
        self.calls.append((audio, options))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return iter(response), None


def expected(count, **edits):
    return ' '.join(edits.get(str(index), f'단어{index}') for index in range(count))


def test_stable_prefix_is_reused_and_latest_suffix_is_revisable():
    model = Model(segments(12), segments(12),
                  segments(14, first=10, offset=19.5),
                  segments(14, first=10, offset=19.5, edits={13: '수정했어요.'}))
    stream = IncrementalWhisperStream(model)
    assert stream.transcribe(pcm(24), 16000) == expected(12)
    assert stream.last_metrics['trimmed_s'] == 0
    assert stream.transcribe(pcm(26), 16000) == expected(12)
    assert stream.last_metrics['trimmed_s'] == 19.5
    assert stream.transcribe(pcm(28), 16000) == expected(14)
    assert stream.last_metrics['input_window_s'] == 8.5
    assert stream.transcribe(pcm(30), 16000, final=True) == expected(14, **{'13': '수정했어요.'})
    audio, options = model.calls[-1]
    assert len(audio) == int(10.5 * 16000)
    assert audio[-1] == 1234 / 32768  # Includes the newest input sample.
    assert not options.get('word_timestamps', False)
    # Acoustic overlap supplies context; a text prefix can suppress new speech.
    assert options['initial_prompt'] is None
    assert stream.last_metrics['final'] is True


def test_final_changed_overlap_falls_back_instead_of_joining_incompatible_text():
    model = Model(segments(12), segments(12),
                  segments(14, first=10, offset=19.5, edits={10: '바뀜'}),
                  segments(14, edits={10: '바뀜'}))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.transcribe(pcm(28), 16000, final=True) == expected(14, **{'10': '바뀜'})
    assert [len(audio) / 16000 for audio, _ in model.calls] == [24, 26, 8.5, 28]
    assert stream.last_metrics['fallback_full'] is True
    assert stream.last_metrics['trimmed_s'] == stream.last_metrics['committed_s'] == 0


def test_changed_segment_partition_recovers_even_when_its_words_are_unchanged():
    suffix = segments(14, first=10, offset=19.5)
    combined = [SimpleNamespace(text=''.join(segment.text for segment in suffix),
                                start=suffix[0].start, end=suffix[-1].end)]
    model = Model(segments(12), segments(12), combined, segments(14))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.transcribe(pcm(28), 16000, final=True) == expected(14)
    assert stream.last_metrics['fallback_full'] is True


def test_repetition_after_the_overlap_is_not_deduplicated():
    repeated = {index: '다시' for index in range(14)}
    model = Model(segments(12, edits=repeated), segments(12, edits=repeated),
                  segments(14, first=10, offset=19.5, edits=repeated))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.transcribe(pcm(28), 16000, final=True).split() == ['다시'] * 14


@pytest.mark.parametrize('failure', [[], RuntimeError('decode failed')])
def test_final_failure_never_returns_stale_committed_prefix_and_can_retry(failure):
    model = Model(segments(12), segments(12), failure,
                  segments(14, first=10, offset=19.5))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    if isinstance(failure, Exception):
        with pytest.raises(RuntimeError, match='decode failed'):
            stream.transcribe(pcm(28), 16000, final=True)
    else:
        assert stream.transcribe(pcm(28), 16000, final=True) == ''
    assert stream.transcribe(pcm(28), 16000, final=True) == expected(14)
    assert stream.last_metrics['input_window_s'] == 8.5


@pytest.mark.parametrize('failure', [[], RuntimeError('recovery failed')])
def test_failed_full_fallback_does_not_erase_previous_state(failure):
    model = Model(segments(12), segments(12),
                  segments(14, first=10, offset=19.5, edits={10: '바뀜'}),
                  failure,
                  segments(14, first=10, offset=19.5))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    if isinstance(failure, Exception):
        with pytest.raises(RuntimeError, match='recovery failed'):
            stream.transcribe(pcm(28), 16000)
    else:
        assert stream.transcribe(pcm(28), 16000) == ''
    assert stream.transcribe(pcm(28), 16000, final=True) == expected(14)
    assert stream.last_metrics['input_window_s'] == 8.5


def test_stable_text_alone_does_not_cause_an_arbitrary_mid_segment_audio_cut():
    whole = [SimpleNamespace(text=expected(12), start=0.2, end=23.5)]
    model = Model(whole, whole, whole)
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.last_metrics['committed_s'] == 23.5
    assert stream.last_metrics['trimmed_s'] == 0
    assert stream.transcribe(pcm(28), 16000, final=True) == expected(12)
    assert len(model.calls[-1][0]) == 28 * 16000


def test_two_agreeing_recent_segments_wait_before_they_become_stable():
    stream = IncrementalWhisperStream(Model(segments(2), segments(2), segments(3)))
    stream.transcribe(pcm(4), 16000)
    stream.transcribe(pcm(4), 16000)
    assert stream.last_metrics['committed_s'] == 0
    assert stream.transcribe(pcm(6), 16000) == expected(3)


def test_a_changed_prefix_without_audio_trimming_needs_no_second_decode():
    model = Model(segments(2), segments(2), segments(3, edits={0: '다른말'}))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(4), 16000)
    stream.transcribe(pcm(5), 16000)
    assert stream.transcribe(pcm(7), 16000) == expected(3, **{'0': '다른말'})
    assert len(model.calls) == 3


def test_streams_share_model_but_not_utterance_state():
    engine = LocalWhisperTranscriber.__new__(LocalWhisperTranscriber)
    engine.model = Model(segments(2), segments(2), segments(1, edits={0: '새발화'}))
    first, second = engine.create_stream(), engine.create_stream()
    first.transcribe(pcm(4), 16000)
    first.transcribe(pcm(5), 16000)
    assert second.transcribe(pcm(2), 16000) == '새발화'
    assert second.last_metrics['committed_s'] == 0


def test_mlx_text_only_adapter_keeps_existing_full_decode_path():
    engine = LocalWhisperTranscriber.__new__(LocalWhisperTranscriber)
    engine.backend = 'mlx'
    assert engine.create_stream() is None


@pytest.mark.parametrize('data, rate', [(b'\x01', 16000), (b'\x01\x00', 8000)])
def test_invalid_pcm_is_rejected_before_inference(data, rate):
    model = Model()
    with pytest.raises(ValueError):
        IncrementalWhisperStream(model).transcribe(data, rate)
    assert model.calls == []


def test_silence_does_not_call_model():
    model = Model()
    assert IncrementalWhisperStream(model).transcribe(bytes(32000), 16000) == ''
    assert model.calls == []


def test_native_segments_do_not_need_expensive_word_alignment():
    model = Model([SimpleNamespace(text='인식결과', start=0.2, end=1)])
    assert IncrementalWhisperStream(model).transcribe(pcm(2), 16000) == '인식결과'
    assert 'word_timestamps' not in model.calls[0][1]


def test_hallucinated_timestamps_beyond_input_cannot_commit_or_trim():
    stream = IncrementalWhisperStream(Model(segments(12)))
    assert stream.transcribe(pcm(10), 16000) == expected(12)
    assert stream.last_metrics['committed_s'] == stream.last_metrics['trimmed_s'] == 0


def test_segment_boundaries_past_recording_are_ignored_for_trimming():
    invalid = segments(12)
    for segment in invalid:
        segment.end = 99.0
    stream = IncrementalWhisperStream(Model(invalid, invalid))
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.last_metrics['committed_s'] == 0
    assert stream.last_metrics['trimmed_s'] == 0


def test_last_frame_timestamp_rounding_is_bounded_to_real_audio():
    model = Model([SimpleNamespace(text=' 마지막', start=0.2, end=2.02)])
    stream = IncrementalWhisperStream(model)
    assert stream.transcribe(pcm(2), 16000, final=True) == '마지막'
    assert stream._previous[-1].end == 2


@pytest.mark.parametrize('final', [False, True])
def test_old_overlap_only_cannot_hide_new_voiced_tail_even_for_endpoint_preview(final):
    model = Model(segments(12), segments(12),
                  segments(12, first=10, offset=19.5), segments(14))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000, speech_end_s=23.5)
    stream.transcribe(pcm(26), 16000, speech_end_s=23.5)
    assert stream.transcribe(pcm(28), 16000, final=final, speech_end_s=27.5) == expected(14)
    assert stream.last_metrics['fallback_reason'] == 'missing_tail'
    assert stream.last_metrics['fallback_full'] is True
    assert stream.last_metrics['tail_gap_s'] == 0


def test_some_new_text_does_not_excuse_a_missing_last_sentence():
    model = Model(segments(12), segments(12),
                  segments(14, first=10, offset=19.5), segments(16))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000, speech_end_s=23.5)
    stream.transcribe(pcm(26), 16000, speech_end_s=23.5)
    assert stream.transcribe(pcm(32), 16000, speech_end_s=31.5) == expected(16)
    assert stream.last_metrics['fallback_reason'] == 'missing_tail'


def test_short_new_phrase_is_checked_before_the_three_second_fallback():
    complete = segments(12) + [SimpleNamespace(text=' 새 말', start=23.5, end=24.5)]
    model = Model(segments(12), segments(12),
                  segments(12, first=10, offset=19.5), complete)
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000, speech_end_s=23.5)
    stream.transcribe(pcm(26), 16000, speech_end_s=23.5)
    assert stream.transcribe(pcm(25), 16000, speech_end_s=24.5) == expected(12) + ' 새 말'
    assert stream.last_metrics['fallback_reason'] == 'missing_tail'


def test_known_trailing_silence_does_not_trigger_unnecessary_full_recovery():
    model = Model(segments(12), segments(12), segments(12, first=10, offset=19.5))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000, speech_end_s=23.5)
    stream.transcribe(pcm(26), 16000, speech_end_s=23.5)
    assert stream.transcribe(pcm(28), 16000, speech_end_s=23.5) == expected(12)
    assert len(model.calls) == 3
    assert stream.last_metrics['fallback_full'] is False


def test_direct_calls_without_vad_hint_recover_conservatively_after_large_gap():
    model = Model(segments(12), segments(12),
                  segments(12, first=10, offset=19.5), segments(14))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.transcribe(pcm(28), 16000) == expected(14)
    assert stream.last_metrics['fallback_reason'] == 'missing_tail'


def test_full_recovery_missing_the_same_tail_is_an_error_and_state_can_retry():
    model = Model(segments(12), segments(12),
                  segments(12, first=10, offset=19.5), segments(12),
                  segments(14, first=10, offset=19.5))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000, speech_end_s=23.5)
    stream.transcribe(pcm(26), 16000, speech_end_s=23.5)
    with pytest.raises(ValueError, match='omitted recent speech'):
        stream.transcribe(pcm(28), 16000, speech_end_s=27.5)
    assert stream.transcribe(pcm(28), 16000, speech_end_s=27.5) == expected(14)
    assert stream.last_metrics['input_window_s'] == 8.5


def test_full_input_with_uncovered_speech_is_not_returned_as_a_successful_preview():
    model = Model(segments(2), segments(2), segments(4))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(5), 16000, speech_end_s=3.5)
    with pytest.raises(ValueError, match='omitted recent speech'):
        stream.transcribe(pcm(7), 16000, speech_end_s=6.5)
    assert stream.transcribe(pcm(8), 16000, speech_end_s=7.5) == expected(4)


def test_clamped_invalid_suffix_timestamp_cannot_prove_tail_coverage():
    suffix = segments(14, first=10, offset=19.5)
    suffix[-1].end = 99
    model = Model(segments(12), segments(12), suffix, segments(14))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000, speech_end_s=23.5)
    stream.transcribe(pcm(26), 16000, speech_end_s=23.5)
    assert stream.transcribe(pcm(28), 16000, speech_end_s=27.5) == expected(14)
    assert stream.last_metrics['fallback_reason'] == 'invalid_timing'
    assert stream.last_metrics['fallback_full'] is True


@pytest.mark.parametrize('speech_end_s', [-1, 3, float('nan'), True])
def test_invalid_vad_hint_is_rejected_before_inference(speech_end_s):
    model = Model()
    with pytest.raises(ValueError, match='speech_end_s'):
        IncrementalWhisperStream(model).transcribe(pcm(2), 16000, speech_end_s=speech_end_s)
    assert model.calls == []


def test_released_pcm_prefix_is_retained_as_text_with_absolute_timestamps():
    model = Model(segments(12), segments(12),
                  segments(14, first=10, offset=19.5))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.retained_start_s == 19.5
    assert stream.transcribe(pcm(8.5), 16000, audio_start_s=19.5,
                             speech_end_s=27.5, final=True) == expected(14)
    assert len(model.calls[-1][0]) / 16000 == 8.5
    assert stream._previous[-1].end == 27.5
    assert stream.last_metrics['audio_start_s'] == 19.5


@pytest.mark.parametrize('partition_changed', [False, True])
def test_window_fallback_keeps_released_text_and_revises_only_available_suffix(partition_changed):
    suffix = segments(14, first=10, offset=19.5, edits={10: '고쳤어요'})
    if partition_changed:
        suffix = [SimpleNamespace(text=''.join(segment.text for segment in suffix),
                                  start=suffix[0].start, end=suffix[-1].end)]
    model = Model(segments(12), segments(12), suffix)
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.transcribe(pcm(8.5), 16000, audio_start_s=19.5,
                             speech_end_s=27.5, final=True) == expected(14, **{'10': '고쳤어요'})
    assert stream.retained_start_s == 19.5
    assert stream.last_metrics['fallback_reason'] == 'overlap_changed'
    assert len(model.calls) == 3  # Already decoded all available PCM; no pointless repeat.


def test_window_fallback_can_redecode_earlier_retained_audio_without_losing_frozen_prefix():
    model = Model(segments(12), segments(12),
                  segments(14, first=10, offset=19.5, edits={10: '고쳤어요'}),
                  segments(14, first=8, offset=15.5, edits={10: '고쳤어요'}))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.transcribe(pcm(12.5), 16000, audio_start_s=15.5,
                             speech_end_s=27.5, final=True) == expected(14, **{'10': '고쳤어요'})
    assert [len(audio) / 16000 for audio, _ in model.calls[-2:]] == [8.5, 12.5]
    assert stream.retained_start_s == 15.5


def test_identical_repeated_phrases_cross_released_window_without_deduplication():
    repeated = {index: '다시' for index in range(14)}
    model = Model(segments(12, edits=repeated), segments(12, edits=repeated),
                  segments(14, first=10, offset=19.5, edits=repeated))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    assert stream.transcribe(pcm(8.5), 16000, audio_start_s=19.5,
                             speech_end_s=27.5, final=True).split() == ['다시'] * 14


@pytest.mark.parametrize('failure', [[], RuntimeError('decode failed'),
                                     segments(12, first=10, offset=19.5)])
def test_released_prefix_survives_failure_but_never_becomes_stale_final(failure):
    model = Model(segments(12), segments(12), failure,
                  segments(14, first=10, offset=19.5))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    arguments = dict(audio_start_s=19.5, speech_end_s=27.5, final=True)
    if isinstance(failure, Exception):
        with pytest.raises(RuntimeError, match='decode failed'):
            stream.transcribe(pcm(8.5), 16000, **arguments)
    elif failure:
        with pytest.raises(ValueError, match='omitted recent speech'):
            stream.transcribe(pcm(8.5), 16000, **arguments)
    else:
        assert stream.transcribe(pcm(8.5), 16000, **arguments) == ''
    assert stream.transcribe(pcm(8.5), 16000, **arguments) == expected(14)


@pytest.mark.parametrize('start', [-1, True, float('nan'), float('inf'), '1', 20, 18])
def test_invalid_or_unsafe_window_start_is_rejected_before_decode(start):
    model = Model(segments(12), segments(12))
    stream = IncrementalWhisperStream(model)
    stream.transcribe(pcm(24), 16000)
    stream.transcribe(pcm(26), 16000)
    with pytest.raises(ValueError):
        stream.transcribe(pcm(8.5), 16000, audio_start_s=start)
    assert len(model.calls) == 2


def test_ten_minutes_of_speech_accumulates_all_text_from_bounded_pcm_windows():
    class TimelineModel:
        def __init__(self):
            self.end_s = 0
            self.largest_window_s = 0

        def transcribe(self, audio, **_):
            self.largest_window_s = max(self.largest_window_s, len(audio) / 16000)
            offset = self.end_s - len(audio) / 16000
            words = [SimpleNamespace(start=index * 2 + .1 - offset,
                                     end=index * 2 + 1.8 - offset,
                                     text=' ' + ('다시' if index % 3 == 0 else f'단어{index}'))
                     for index in range(min(300, int(self.end_s // 2)))
                     if index * 2 + 1.8 > offset + .01]
            return iter(words), None

    model = TimelineModel()
    stream = IncrementalWhisperStream(model)
    window_start = 0.0
    largest_pcm = 0
    for end_s in range(2, 601, 2):
        model.end_s = end_s
        snapshot = pcm(end_s - window_start)
        largest_pcm = max(largest_pcm, len(snapshot))
        preview = stream.transcribe(snapshot, 16000, audio_start_s=window_start,
                                    speech_end_s=end_s - .2)
        assert len(preview.split()) == end_s // 2
        window_start = stream.retained_start_s
    model.end_s = 602
    final = stream.transcribe(pcm(602 - window_start), 16000,
                              audio_start_s=window_start, speech_end_s=599.8, final=True)
    assert final.split() == ['다시' if index % 3 == 0 else f'단어{index}'
                             for index in range(300)]
    assert window_start > 580
    assert largest_pcm <= 60 * 32000 and model.largest_window_s <= 60
