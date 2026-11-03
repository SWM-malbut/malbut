"""Boilerplate must never become a preview, an Agent request or an interruption."""

from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
import unicodedata

import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.transcript_filter import boilerplate_reason


BLOCKED = [
    '감사합니다.', ' 감 사 합 니 다! ', '감사합니다. 감사합니다!',
    unicodedata.normalize('NFD', '감사합니다.'),
    '시청해 주셔서 감사합니다.',
    'MBC 뉴스 김민수입니다.', 'mbc뉴스000입니다', 'ＭＢＣ 뉴스입니다.',
    '지금까지 MBC 뉴스 홍길동 기자입니다.', 'KBS 뉴스 김민수입니다.',
    '구독과 좋아요', '구독, 좋아요 부탁드립니다!', '좋아요와 구독 부탁드려요.',
    '구 독 과 좋 아 요! 알림 설정도 부탁드립니다.',
    '아' * 300, '아, ' * 12, '음' * 12,
    '오늘 날씨 알려 줘. ' + '아' * 12,
    unicodedata.normalize('NFD', '아' * 12),
]


@pytest.mark.parametrize('text', BLOCKED)
def test_recurring_boilerplate_is_blocked_despite_formatting(text):
    assert boilerplate_reason(text) is not None


@pytest.mark.parametrize('text', [
    '네', '아니요', '좋아요.', '고마워요.', '거실로 가 주세요.',
    '말씀 감사합니다. 그런데 오늘 날씨는?', '도와주셔서 감사합니다.',
    'MBC 뉴스 틀어 줘.', '뉴스가 궁금해요.', '구독 취소 방법 알려 줘.',
    '아, 아.', '아' * 11, '아' * 6 + '잠깐만' + '아' * 6,
    '', None,
])
def test_ordinary_speech_is_not_blocked_by_individual_keywords(text):
    assert boilerplate_reason(text) is None


@pytest.mark.parametrize('count,expected', [(11, None), (12, 'repeated_syllable')])
def test_repeated_syllable_threshold(count, expected):
    assert boilerplate_reason('아' * count) == expected


VOICE = b'\x01\x00' * 320
QUIET = bytes(640)


def pump(pipeline, done):
    deadline = monotonic() + 3
    while not done():
        pipeline.poll()
        assert monotonic() < deadline
        sleep(.001)


@pytest.fixture
def create():
    pipelines = []

    def factory(text, *, streaming=False, followup_timeout_s=10., input_has_aec=True):
        state = SimpleNamespace(
            text=text, now=0., transcripts=[], partials=[], interruptions=[],
            controls=[], statuses=[], reports=[], failures=[], calls=[],
        )

        def decode(pcm, rate, **options):
            state.calls.append(options)
            return state.text

        transcriber = SimpleNamespace(transcribe=decode)
        if streaming:
            transcriber.create_stream = lambda: SimpleNamespace(transcribe=decode)
        p = DialoguePipeline(
            recorder_factory=lambda: None, wake=SimpleNamespace(transcribe=decode),
            transcriber=transcriber, is_speech=lambda frame, _: frame == VOICE,
            publish_transcript=lambda *args: state.transcripts.append(args),
            publish_control=lambda *args: state.controls.append(args),
            publish_interruption=lambda *args: state.interruptions.append(args),
            publish_input_status=lambda *args: state.statuses.append(args),
            on_partial=lambda *args: state.partials.append(args),
            on_failure=lambda: state.failures.append('failure'),
            report=state.reports.append, clock=lambda: state.now,
            input_has_aec=input_has_aec, endpoint_predecode_s=.8,
            followup_timeout_s=followup_timeout_s,
        )
        pipelines.append(p)
        p.session.activate()
        p.asr_thread = Thread(target=p._infer, daemon=True)
        p.asr_thread.start()
        state.pipeline = p
        return state

    yield factory
    for pipeline in pipelines:
        pipeline.close()


@pytest.mark.parametrize('text', [
    '감사합니다.', 'MBC 뉴스 김민수입니다.', '구독과 좋아요!', '아' * 300,
])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('input_has_aec', [False, True])
def test_default_filter_requests_notice_then_accepts_next_request(
        create, text, streaming, input_has_aec):
    state = create(text, streaming=streaming, input_has_aec=input_has_aec)
    p = state.pipeline
    p.feed(VOICE * 50 + QUIET * 100)
    pump(p, lambda: any(r.startswith('transcript_filtered:') for r in state.reports))
    assert state.transcripts == state.partials == state.interruptions == state.failures == []
    uid = state.statuses[0][1]
    assert state.statuses == [('', uid, 'started'), ('', uid, 'filtered')]
    assert not p.session.active and p.session.utterance_id is None
    assert p._reply_request_id == uid and p._pending is None

    # Neither another reply nor interim audio completion opens the retry window.
    p.on_playback_status('other', 'finished', request_id='other')
    p.on_playback_status('progress', 'finished', request_id=uid, interim=True)
    assert not p.session.active and p._reply_request_id == uid
    p.on_playback_status('retry-notice', 'playing', request_id=uid)
    assert not p.session.active
    p.feed(VOICE * 50 + QUIET * 100)
    assert state.statuses == [('', uid, 'started'), ('', uid, 'filtered')]
    p.on_playback_status('retry-notice', 'finished', request_id=uid)
    assert p.session.active and p._reply_request_id is None

    state.text = '거실로 가 주세요.'
    state.now = 1.1  # Also clear the speaker tail guard when AEC is unavailable.
    p.feed(VOICE * 50 + QUIET * 100)
    pump(p, lambda: bool(state.transcripts))
    assert [text for _, text in state.transcripts] == ['거실로 가 주세요.']


@pytest.mark.parametrize('terminal', ['failed', 'stopped', 'timeout'])
def test_filtered_notice_failure_or_timeout_returns_to_wake(create, terminal):
    state = create('감사합니다.')
    p = state.pipeline
    p.feed(VOICE * 50 + QUIET * 100)
    pump(p, lambda: any(r.startswith('transcript_filtered:') for r in state.reports))
    uid = p._reply_request_id
    if terminal == 'timeout':
        state.now = 46.
        p.poll()
    else:
        p.on_playback_status('retry-notice', terminal, request_id=uid)
    assert not p.session.active and p._reply_request_id is None
    assert p._retry_notice_deadline is None and p._followup_deadline is None
    p.on_playback_status('retry-notice', 'finished', request_id=uid)
    assert not p.session.active
    assert state.transcripts == state.failures == []


def test_filtered_notice_allows_retry_even_when_normal_followup_is_disabled(create):
    state = create('감사합니다.', followup_timeout_s=0.)
    p = state.pipeline
    p.feed(VOICE * 50 + QUIET * 100)
    pump(p, lambda: any(r.startswith('transcript_filtered:') for r in state.reports))
    uid = p._reply_request_id
    p.on_playback_status('retry-notice', 'finished', request_id=uid)
    assert p.session.active
    assert p._followup_deadline == p.command_stream.settings.start_timeout_s
    state.now = p._followup_deadline + .1
    p.poll()
    assert not p.session.active and state.failures == []


@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('text', ['감사합니다.', '아, ' * 12])
def test_boilerplate_preview_cannot_publish_or_trim_audio_and_can_recover(create, partial, text):
    state = create(text, streaming=True)
    p = state.pipeline
    p.feed(VOICE * 50)
    uid = p.session.utterance_id
    token = (uid, p.command_stream.collector.revision)
    before = p.command_stream.collector.snapshot('partial_check').pcm
    p._accept_endpoint(p._generation, token, state.text, None, .8, partial=partial)
    assert state.partials == state.transcripts == []
    assert not any(status == 'filtered' for _, _, status in state.statuses)
    assert p.command_stream.collector.snapshot('partial_check').pcm == before
    assert not p._usable_text(state.text, None)
    state.text = '내일 날씨 알려 줘.'
    p.feed(QUIET * 100)
    pump(p, lambda: bool(state.transcripts))
    assert state.transcripts == [(uid, state.text)]


@pytest.mark.parametrize('paused_first', [False, True])
def test_filtered_interruption_resumes_answer_without_agent_classification(create, paused_first):
    state = create('MBC 뉴스 김민수입니다.')
    p = state.pipeline
    p.on_playback_status('answer', 'playing')
    p.feed(VOICE * 50)
    assert state.controls == [('answer', 'pause')]
    if paused_first:
        p.on_playback_status('answer', 'paused')
    p.feed(QUIET * 100)
    pump(p, lambda: any(r.startswith('transcript_filtered:') for r in state.reports))
    if not paused_first:
        p.on_playback_status('answer', 'paused')
    assert state.controls == [('answer', 'pause'), ('answer', 'resume')]
    assert state.transcripts == state.interruptions == state.failures == []
    assert not any(status == 'filtered' for _, _, status in state.statuses)


def test_confirmation_filter_releases_only_current_utterance(create):
    state = create('감사합니다.')
    p = state.pipeline
    assert p.start_session('confirmation')
    p.feed(VOICE * 50)
    uid = p.session.utterance_id
    p.feed(QUIET * 100)
    pump(p, lambda: any(r.startswith('transcript_filtered:') for r in state.reports))
    assert state.statuses == [('confirmation', uid, 'started'), ('confirmation', uid, 'failed')]
    assert p.session_is_active('confirmation')
    assert state.transcripts == state.interruptions == state.failures == []


def test_wake_attached_boilerplate_is_also_filtered_and_late_result_ignored(create):
    state = create('제이크야, 감사합니다.')
    p = state.pipeline
    p.session.terminate()
    p.feed(VOICE * 50 + QUIET * 20)
    pump(p, lambda: any(r.startswith('transcript_filtered:') for r in state.reports))
    uid = state.statuses[0][1]
    p._accept_result('command', p._generation, uid, '감사합니다.', None)
    assert state.reports.count('transcript_filtered:standalone_thanks') == 1
    assert state.statuses == [('', uid, 'started'), ('', uid, 'filtered')]
    assert state.transcripts == state.interruptions == state.failures == []
    p.on_playback_status('retry-notice', 'finished', request_id=uid)
    assert p.session.active
    state.now = 11.
    p.poll()
    assert not p.session.active and state.failures == []
