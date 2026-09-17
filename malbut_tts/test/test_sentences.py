"""CPU-only losslessness and boundary tests for speech work units."""

from inspect import isgenerator
from random import Random

import pytest

from malbut_tts.sentences import iter_speech_segments


def assert_lossless(text, max_chars=80):
    segments = list(iter_speech_segments(text, max_chars))
    assert ''.join(segments) == text
    assert all(0 < len(segment) <= max_chars and segment.strip()
               for segment in segments)
    return segments


def test_preserves_leading_trailing_spacing_and_sentence_closers():
    text = '  안녕하세요?!")  다음 문장입니다。』\n마지막 답변！  '
    assert assert_lossless(text, 24) == [
        '  안녕하세요?!")  ', '다음 문장입니다。』\n', '마지막 답변！  ',
    ]


@pytest.mark.parametrize('ending', ['.', '!', '?', '。', '！', '？'])
def test_sentence_endings_without_spaces(ending):
    text = '첫 문장' + ending + '다음 문장은 충분히 길어서 마지막까지 이어집니다'
    segments = assert_lossless(text, 16)
    assert segments[0] == '첫 문장' + ending


def test_newlines_and_crlf_are_retained():
    text = '  첫 줄\r\n둘째 줄\n\n셋째 줄은 길게 이어져요. 마지막 줄'
    segments = assert_lossless(text, 16)
    assert segments[:2] == ['  첫 줄\r\n', '둘째 줄\n\n']
    assert all(not (left.endswith('\r') and right.startswith('\n'))
               for left, right in zip(segments, segments[1:]))


def test_decimal_and_domain_dots_are_not_sentence_boundaries():
    text = '값은 3.14, example.com 입니다. 다음 문장을 읽습니다! 끝.'
    segments = assert_lossless(text, 32)
    assert segments[0] == '값은 3.14, example.com 입니다. '


def test_long_sentence_uses_nearest_space_or_comma_without_rewriting():
    text = 'abcdefghijkl,mnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz'
    segments = assert_lossless(text, 16)
    assert segments[:2] == ['abcdefghijkl,', 'mnopqrstuvwxyz ']


def test_long_unbroken_text_has_hard_bounded_chunks():
    text = '가' * 161
    assert assert_lossless(text) == ['가' * 80, '가' * 80, '가']


def test_trailing_whitespace_keeps_a_content_character_in_final_chunk():
    text = '가' * 80 + ' ' * 79
    assert assert_lossless(text) == ['가' * 79, '가' + ' ' * 79]


@pytest.mark.parametrize('text', [
    ' ' * 15 + '가' + ' ' * 15 + '나',
    '가.' + ' ' * 15 + '나',
    '가' * 15 + '\r\n나' * 10,
    '\u00a0' * 15 + '가나' + '\u2003' * 15,
    '\x00\x1b[31m안녕?!\x1b[0m\u202e답변\u202c' * 12,
    'e\u0301 👩\u200d👩\u200d👧\u200d👦 가 你好。』 안녕！？”' * 9,
    '!' * 120 + '」' * 20 + '\n답변',
    'https://example.com/a.b?x=3.14 다음 문장이 이어집니다.' * 3,
])
def test_whitespace_controls_unicode_and_punctuation_are_lossless(text):
    assert_lossless(text, 16)


@pytest.mark.parametrize('text', ['', ' ', '\r\n\t\u00a0\u2003' * 100])
def test_blank_input_has_no_speech_segment(text):
    assert list(iter_speech_segments(text)) == []


@pytest.mark.parametrize('text', [None, 123, b'hello', ['hello']])
def test_non_text_is_rejected(text):
    with pytest.raises(TypeError, match='must be a string'):
        next(iter_speech_segments(text))


@pytest.mark.parametrize('limit', [True, False, 0, 15, 513, 80.0, '80', None])
def test_invalid_limit_is_rejected(limit):
    with pytest.raises(ValueError, match='16 through 512'):
        next(iter_speech_segments('시험', limit))


@pytest.mark.parametrize('limit', [16, 80, 512])
@pytest.mark.parametrize('location', ['leading', 'middle', 'trailing'])
def test_oversized_whitespace_fails_before_first_segment(limit, location):
    spacing = ' ' * limit
    sentinel = 'PRIVATE_SENTENCE_SENTINEL'
    if location == 'leading':
        text = spacing + sentinel
    elif location == 'middle':
        text = '이미 끝난 첫 문장. ' + sentinel + spacing + '다음 문장'
    else:
        text = '이미 끝난 첫 문장. ' + sentinel + spacing
    segments = iter_speech_segments(text, limit)
    with pytest.raises(ValueError) as error:
        next(segments)
    assert sentinel not in str(error.value)


@pytest.mark.parametrize('text', [
    '\u00a0' * 15 + '가' + '\u2003' * 15,
    ' ' * 15 + '가' + ' ' * 14 + '나' + ' ' * 15,
    '첫 문장입니다.\n' + ' ' * 10 + '나' + ' ' * 15,
])
def test_stranded_spacing_fails_before_any_partial_segment(text):
    with pytest.raises(ValueError, match='spacing cannot fit'):
        next(iter_speech_segments(text, 16))


def test_short_input_still_splits_each_complete_sentence():
    assert assert_lossless('안녕. 반가워요! 끝.') == ['안녕. ', '반가워요! ', '끝.']


def test_generator_is_lazy_and_does_not_prepare_a_chunk_list():
    segments = iter_speech_segments('가' * 10000, 16)
    assert isgenerator(segments)
    assert next(segments) == '가' * 16
    assert not any(isinstance(value, list) for value in segments.gi_frame.f_locals.values())
    segments.close()


def test_deterministic_mixed_text_property_cases():
    random = Random(917)
    atoms = ['가', '나', 'A', '3.14', 'example.com', '.', '?', '!', '。',
             '！', '？', '”', ')', ',', ' ', '\t', '\r\n', '\u00a0',
             '\u0301', '\u200d', '\u202e', '\x00', '🙂']
    for limit in (16, 17, 32, 80, 512):
        for _ in range(60):
            text = '시작' + ''.join(random.choice(atoms) for _ in range(180)) + '끝'
            assert_lossless(text, limit)
