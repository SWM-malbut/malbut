"""Exercise conversation deadlines and playback races without audio or ROS."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from malbut_stt.conversation import ConversationSession


@pytest.fixture
def run():
    state = SimpleNamespace(now=0.0, events=[])
    state.session = ConversationSession(
        clock=lambda: state.now,
        publish_transcript=lambda uid, text: state.events.append(('text', uid, text)),
        publish_control=lambda pid, command: state.events.append(('control', pid, command)),
    )
    state.session.activate()
    return state


def test_activation_has_no_deadline_and_repeated_sentences_get_new_ids(run):
    session = run.session
    run.now = 100.0
    assert not session.tick()
    original = '  거실로 가줘.\n'
    first = session.user_speech_started()
    assert session.user_speech_started() == first
    assert session.finish_utterance(first, original, addressed=True)
    assert not session.finish_utterance(first, original, addressed=True)
    second = session.user_speech_started()
    assert str(UUID(first)) == first
    assert second != first
    assert session.finish_utterance(second, original, addressed=True)
    assert run.events == [('text', first, original), ('text', second, original)]


def test_finished_starts_five_seconds_and_duplicate_does_not_extend(run):
    session = run.session
    session.on_playback_status('reply', 'playing')
    run.now = 10.0
    session.on_playback_status('reply', 'finished')
    assert session.deadline == 15.0
    run.now = 14.9
    session.on_playback_status('reply', 'finished')
    assert session.deadline == 15.0
    assert not session.tick()
    run.now = 15.0
    assert session.tick()
    assert not session.active
    assert session.user_speech_started() is None


def test_speech_cancels_deadline_and_next_answer_starts_a_new_wait(run):
    session = run.session
    session.on_playback_status('first', 'playing')
    session.on_playback_status('first', 'finished')
    run.now = 4.0
    uid = session.user_speech_started()
    assert session.deadline is None
    run.now = 20.0
    assert not session.tick()
    session.finish_utterance(uid, '계속 이야기하자', addressed=True)
    session.on_playback_status('second', 'playing')
    session.on_playback_status('second', 'finished')
    assert session.deadline == 25.0


def test_speech_start_enforces_expired_deadline_without_a_timer_tick(run):
    session = run.session
    session.on_playback_status('reply', 'playing')
    session.on_playback_status('reply', 'finished')
    run.now = 5.0
    assert session.user_speech_started() is None
    assert not session.active


@pytest.mark.parametrize('state', ['paused', 'stopped', 'failed'])
def test_noncompletion_states_do_not_start_timeout(run, state):
    session = run.session
    session.on_playback_status('reply', 'playing')
    session.on_playback_status('reply', state)
    run.now = 100.0
    assert session.deadline is None
    assert not session.tick()


def test_completion_during_live_utterance_does_not_end_dialogue(run):
    session = run.session
    session.on_playback_status('reply', 'playing')
    uid = session.user_speech_started()
    session.on_playback_status('reply', 'finished')
    run.now = 20.0
    assert not session.tick()
    assert session.finish_utterance(uid, '새로운 질문', addressed=True)
    assert run.events == [('control', 'reply', 'pause'), ('text', uid, '새로운 질문')]


@pytest.mark.parametrize('pause_first', [True, False])
def test_nonaddressed_barge_in_waits_for_pause_ack_before_resume(run, pause_first):
    session = run.session
    session.on_playback_status('reply', 'playing')
    uid = session.user_speech_started()
    assert session.user_speech_started() == uid
    if pause_first:
        session.on_playback_status('reply', 'paused')
    assert not session.finish_utterance(uid, '옆 사람에게 하는 말', addressed=False)
    if not pause_first:
        assert run.events == [('control', 'reply', 'pause')]
        session.on_playback_status('reply', 'paused')
    session.on_playback_status('reply', 'paused')
    assert run.events == [('control', 'reply', 'pause'), ('control', 'reply', 'resume')]
    assert session.deadline is None


@pytest.mark.parametrize('pause_first', [True, False])
def test_addressed_barge_in_stops_old_answer_before_publishing(run, pause_first):
    session = run.session
    session.on_playback_status('reply', 'playing')
    uid = session.user_speech_started()
    if pause_first:
        session.on_playback_status('reply', 'paused')
    assert session.finish_utterance(uid, '아니, 가지 마', addressed=True)
    session.on_playback_status('reply', 'paused')
    session.on_playback_status('reply', 'finished')
    assert session.deadline is None
    assert run.events == [
        ('control', 'reply', 'pause'), ('control', 'reply', 'stop'),
        ('text', uid, '아니, 가지 마'),
    ]


@pytest.mark.parametrize('end_state', ['finished', 'failed', 'stopped'])
def test_terminal_playback_cannot_resume_after_late_pause_ack(run, end_state):
    session = run.session
    session.on_playback_status('reply', 'playing')
    uid = session.user_speech_started()
    session.finish_utterance(uid, '다른 대화', addressed=False)
    session.on_playback_status('reply', end_state)
    session.on_playback_status('reply', 'paused')
    session.on_playback_status('reply', 'playing')
    assert run.events == [('control', 'reply', 'pause')]


def test_replaced_playback_cannot_resume_or_reset_new_completion_deadline(run):
    session = run.session
    session.on_playback_status('old', 'playing')
    uid = session.user_speech_started()
    session.finish_utterance(uid, '다른 대화', addressed=False)
    session.on_playback_status('new', 'playing')
    session.on_playback_status('old', 'paused')
    session.on_playback_status('old', 'finished')
    session.on_playback_status('old', 'playing')
    assert session.playback_id == 'new'
    assert session.deadline is None
    session.on_playback_status('new', 'finished')
    run.now = 3.0
    session.on_playback_status('old', 'finished')
    assert session.deadline == 5.0
    assert run.events == [('control', 'old', 'pause')]


def test_new_speech_cancels_pending_resume_before_pause_ack(run):
    session = run.session
    session.on_playback_status('reply', 'playing')
    first = session.user_speech_started()
    session.finish_utterance(first, '옆 사람에게', addressed=False)
    second = session.user_speech_started()
    session.on_playback_status('reply', 'paused')
    assert run.events == [('control', 'reply', 'pause')]
    session.finish_utterance(second, '이번에는 로봇에게', addressed=True)
    assert run.events[-2:] == [('control', 'reply', 'stop'), ('text', second, '이번에는 로봇에게')]


@pytest.mark.parametrize('replacing', [True, False])
@pytest.mark.parametrize('addressed', [True, False])
def test_new_playback_during_pending_utterance_is_paused_and_owned(run, replacing, addressed):
    session = run.session
    if replacing:
        session.on_playback_status('old', 'playing')
    uid = session.user_speech_started()
    session.on_playback_status('new', 'playing')
    session.on_playback_status('new', 'playing')
    expected = [('control', 'old', 'pause')] if replacing else []
    expected.append(('control', 'new', 'pause'))
    assert run.events == expected
    assert session.utterance_id == uid
    session.on_playback_status('old', 'paused')
    session.on_playback_status('new', 'paused')
    assert session.finish_utterance(uid, '이어지는 발화', addressed=addressed) is addressed
    expected.append(('control', 'new', 'stop' if addressed else 'resume'))
    if addressed:
        expected.append(('text', uid, '이어지는 발화'))
    assert run.events == expected


@pytest.mark.parametrize('addressed', [True, False])
def test_late_resume_ack_after_second_onset_does_not_duplicate_pause(run, addressed):
    session = run.session
    session.on_playback_status('reply', 'playing')
    first = session.user_speech_started()
    session.on_playback_status('reply', 'paused')
    session.finish_utterance(first, '옆 사람에게', addressed=False)
    second = session.user_speech_started()
    expected = [
        ('control', 'reply', 'pause'), ('control', 'reply', 'resume'),
        ('control', 'reply', 'pause'),
    ]
    session.on_playback_status('reply', 'playing')
    session.on_playback_status('reply', 'playing')
    assert run.events == expected
    assert session.utterance_id == second
    session.finish_utterance(second, '두 번째 발화', addressed=addressed)
    if addressed:
        expected += [('control', 'reply', 'stop'), ('text', second, '두 번째 발화')]
    assert run.events == expected
    session.on_playback_status('reply', 'paused')
    if not addressed:
        expected.append(('control', 'reply', 'resume'))
    assert run.events == expected


@pytest.mark.parametrize('addressed', [None, 0, 'yes'])
def test_unknown_addressee_keeps_utterance_pending_without_guessing(run, addressed):
    session = run.session
    session.on_playback_status('reply', 'playing')
    uid = session.user_speech_started()
    with pytest.raises(ValueError, match='addressee'):
        session.finish_utterance(uid, '누구에게 하는 말일까', addressed=addressed)
    assert session.utterance_id == uid
    assert run.events == [('control', 'reply', 'pause')]


def test_termination_rejects_old_transcript_and_terminal_playback_after_reactivation(run):
    session = run.session
    session.on_playback_status('old', 'playing')
    uid = session.user_speech_started()
    session.terminate()
    assert not session.finish_utterance(uid, '늦은 결과', addressed=True)
    session.on_playback_status('old', 'paused')
    session.on_playback_status('old', 'finished')
    session.activate()
    assert not session.finish_utterance(uid, '늦은 결과', addressed=True)
    session.on_playback_status('old', 'playing')
    assert session.playback_id == 'old'
    assert session.playback_state == 'finished'
    assert session.deadline is None
    assert run.events == [('control', 'old', 'pause')]


def test_playback_started_while_inactive_is_paused_after_activation(run):
    session = run.session
    session.terminate()
    session.on_playback_status('reply', 'playing')
    assert session.playback_id == 'reply'
    assert session.playback_state == 'playing'
    assert run.events == []
    session.activate()
    assert session.user_speech_started() is not None
    assert run.events == [('control', 'reply', 'pause')]


def test_inactive_completion_is_tracked_without_arming_timeout(run):
    session = run.session
    session.terminate()
    session.on_playback_status('reply', 'playing')
    session.on_playback_status('reply', 'finished')
    assert session.playback_state == 'finished'
    assert session.deadline is None
    session.activate()
    session.on_playback_status('reply', 'finished')
    run.now = 20.0
    assert not session.tick()
    assert session.deadline is None
    assert run.events == []


def test_termination_preserves_current_playback_for_the_next_conversation(run):
    session = run.session
    session.on_playback_status('reply', 'playing')
    session.terminate()
    assert session.playback_id == 'reply'
    assert session.playback_state == 'playing'
    session.activate()
    session.user_speech_started()
    assert run.events == [('control', 'reply', 'pause')]


def test_inactive_pause_ack_updates_state_without_resuming(run):
    session = run.session
    session.on_playback_status('reply', 'playing')
    uid = session.user_speech_started()
    session.finish_utterance(uid, '다른 대화', addressed=False)
    session.terminate()
    session.on_playback_status('reply', 'paused')
    assert session.playback_state == 'paused'
    assert session.deadline is None
    assert run.events == [('control', 'reply', 'pause')]


def test_replaced_playback_remains_stale_while_inactive(run):
    session = run.session
    session.terminate()
    session.on_playback_status('old', 'playing')
    session.on_playback_status('new', 'playing')
    for state in ('paused', 'finished', 'playing'):
        session.on_playback_status('old', state)
    assert session.playback_id == 'new'
    assert session.playback_state == 'playing'
    assert session.deadline is None
    assert run.events == []
    session.activate()
    session.user_speech_started()
    assert run.events == [('control', 'new', 'pause')]


@pytest.mark.parametrize('pid,state', [('', 'playing'), (' ', 'paused'), (None, 'playing'),
                                       ('reply', 'done'), ('reply', None)])
def test_invalid_playback_status_is_rejected_without_state_change(run, pid, state):
    with pytest.raises(ValueError):
        run.session.on_playback_status(pid, state)
    assert run.session.playback_id is None
    assert run.events == []


def test_unknown_completion_cannot_start_dialogue_timeout(run):
    run.session.on_playback_status('unseen', 'finished')
    assert run.session.deadline is None


@pytest.mark.parametrize('text', ['', ' \n', None])
def test_invalid_final_text_does_not_consume_pending_utterance(run, text):
    uid = run.session.user_speech_started()
    with pytest.raises(ValueError, match='transcript'):
        run.session.finish_utterance(uid, text, addressed=True)
    assert run.session.utterance_id == uid
    assert run.events == []


def test_discarded_utterance_keeps_dialogue_active_and_next_speech_gets_new_id(run):
    session = run.session
    first = session.user_speech_started()
    assert session.discard_utterance(first)
    assert session.active and session.utterance_id is None
    assert session.deadline is None and run.events == []
    second = session.user_speech_started()
    assert second != first
    assert not session.discard_utterance(first)
    assert not session.discard_utterance(None)
    assert session.utterance_id == second
    assert session.finish_utterance(second, '다시 말할게요.', addressed=True)
    assert run.events == [('text', second, '다시 말할게요.')]


@pytest.mark.parametrize('pause_first', [True, False])
def test_discarded_interruption_preserves_pause_without_guessing_resume(run, pause_first):
    session = run.session
    session.on_playback_status('reply', 'playing')
    first = session.user_speech_started()
    if pause_first:
        session.on_playback_status('reply', 'paused')
    assert session.discard_utterance(first)
    if not pause_first:
        session.on_playback_status('reply', 'paused')
    assert session.active and session.utterance_id is None
    assert session.playback_id == 'reply' and session.playback_state == 'paused'
    assert session.deadline is None and session._control == 'pause'
    assert run.events == [('control', 'reply', 'pause')]
    second = session.user_speech_started()
    assert second != first and session.interrupted_playback_id == 'reply'
    assert session.finish_utterance(second, '새 질문이에요.', addressed=True)
    assert run.events == [
        ('control', 'reply', 'pause'), ('control', 'reply', 'stop'),
        ('text', second, '새 질문이에요.'),
    ]


def test_discard_cannot_reactivate_an_ended_session(run):
    uid = run.session.user_speech_started()
    run.session.terminate()
    assert not run.session.discard_utterance(uid)
    assert not run.session.active and run.events == []
