"""Keep a leading wake and its command without matching incidental mentions."""

import unicodedata
from types import SimpleNamespace

import pytest

from malbut_stt import wake


@pytest.mark.parametrize('text, command', [
    ('제이크야 거실로 가', '거실로 가'),
    ('제이크, 거실로 가 주세요.', '거실로 가 주세요.'),
    ('  제이 크야! 지금 몇 시야?  ', '지금 몇 시야?'),
    (unicodedata.normalize('NFD', '제이크야 문을 열어 줘.'), '문을 열어 줘.'),
    ('제이크야: 네.', '네.'),
    ('제이크야 “문을 열어”라고 말해 줘.', '“문을 열어”라고 말해 줘.'),
    ('제이크야', ''),
    (' “제이 크야?” ', ''),
    ('제이크!', ''),
])
def test_leading_wake_returns_the_same_utterance_command(text, command):
    assert wake.split_wake_command(text) == command


@pytest.mark.parametrize('text', [
    '', ' ', None, 123, 'Jake 거실로 가', '안녕 제이크야 거실로 가',
    '친구가 제이크야 하고 불렀어', '제이크를 불러 줘', '제이크는 로봇이야',
    '제이크야말로 로봇이야', '제이크야거실로 가', '제이크2 거실로 가', '제이크야옹',
    '제이크라는 이름이 좋아', '“제이크야”라고 말했다',
])
def test_mentions_and_unbounded_prefixes_do_not_open_a_turn(text):
    assert wake.split_wake_command(text) is None


@pytest.mark.parametrize('text', [
    '“제이크야 거실로 가”라고 적혀 있어',
    '‘제이크야, 문 열어’라는 문장이야',
    '"제이크야 거실로 가"라고 말했어',
    "'제이크야 거실로 가'라고 말했어",
    '… “제이크야 거실로 가”를 읽어 줘',
    '「제이크야 거실로 가」라는 문장',
])
def test_quoted_report_of_a_whole_wake_command_does_not_open_a_turn(text):
    assert wake.split_wake_command(text) is None


def test_pure_wake_predicate_keeps_its_existing_exact_only_contract():
    assert wake.is_wake_phrase('제이크야!')
    assert not wake.is_wake_phrase('제이크야 거실로 가')


def recognizer(segments):
    model = SimpleNamespace(transcribe=lambda *_, **__: (iter(segments), None))
    return wake.LocalWakeRecognizer.from_transcriber(SimpleNamespace(model=model))


@pytest.mark.parametrize('text', ['제이크야', '제이크야 거실로 가'])
def test_confident_no_speech_segment_cannot_create_a_wake_or_command(text):
    engine = recognizer([SimpleNamespace(text=text, no_speech_prob=0.95, avg_logprob=-2.0)])
    assert engine.transcribe(b'\x01\x00' * 1600, 16000) == ''


def test_wake_decode_preserves_valid_segments_around_a_rejected_span():
    engine = recognizer([
        SimpleNamespace(text='제이크야 ', no_speech_prob=0.1, avg_logprob=-0.1),
        SimpleNamespace(text='가짜 꼬리 ', no_speech_prob=0.95, avg_logprob=-2.0),
        SimpleNamespace(text='거실로 가.', no_speech_prob=0.1, avg_logprob=-0.2),
    ])
    text = engine.transcribe(b'\x01\x00' * 1600, 16000)
    assert text == '제이크야 거실로 가.'
    assert wake.split_wake_command(text) == '거실로 가.'


@pytest.mark.parametrize('metadata', [
    {}, {'no_speech_prob': 0.95},
    {'no_speech_prob': 0.95, 'avg_logprob': -0.2},
    {'no_speech_prob': 0.1, 'avg_logprob': -2.0},
])
def test_low_level_short_wake_is_preserved_without_joint_negative_evidence(metadata):
    engine = recognizer([SimpleNamespace(text='제이크야 네.', **metadata)])
    # The adapter must not invent an amplitude floor for a valid short decode.
    assert engine.transcribe(b'\x01\x00' * 1600, 16000) == '제이크야 네.'
