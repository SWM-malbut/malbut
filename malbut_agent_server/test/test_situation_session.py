"""Exercise audio and inference ordering with deterministic clocks and futures."""

from collections import deque
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from malbut_agent_server.situation_dialogue import SituationResult, SituationTurn
from malbut_agent_server.situation_session import SituationSpeechSession


class ManualExecutor:
    """Expose in-flight operations without starting threads or calling a model."""

    def __init__(self):
        self.jobs = deque()

    def submit(self, fn):
        future = Future()
        self.jobs.append((future, fn))
        return future

    def run_next(self):
        future, fn = self.jobs.popleft()
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn())
            except Exception as error:
                future.set_exception(error)
        return future


class Harness:
    def __init__(self, *turns):
        self.now = 0.0
        self.turns = deque(turns or [SituationTurn('실제로 넘어지셨나요?')])
        self.calls = []
        self.events = []
        self.openings = []
        self.verifications = []
        self.verify_automatically = True
        self.verify_accepted = True
        self.outcomes = []
        self.finishes = []
        self.speech_available = True
        self.executor = ManualExecutor()
        self.engine = SimpleNamespace(
            start=lambda *args: self.evaluate('start', *args),
            answer=lambda text: self.evaluate('answer', text),
            no_response=lambda: self.evaluate('no_response'),
        )
        self.session = SituationSpeechSession(
            SimpleNamespace(request_id='incident-1', situation_type='fall', summary='바닥에 누움'),
            lambda: self.engine, self, self.finish, clock=lambda: self.now,
            executor=self.executor, on_result=self.report_result,
        )

    def evaluate(self, method, *args):
        self.calls.append((method, args))
        result = self.turns.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def finish(self, status, result):
        self.finishes.append((status, result))

    def report_result(self, result):
        self.events.append(('result', result))
        self.outcomes.append(result)

    def open_session(self, session_id):
        future = Future()
        self.openings.append((session_id, future))
        self.events.append(('open', session_id))
        return future

    def close_session(self, session_id):
        self.events.append(('close', session_id))
        future = Future()
        future.set_result(SimpleNamespace(accepted=True))
        return future

    def verify_session(self, session_id):
        future = Future()
        self.verifications.append((session_id, future))
        self.events.append(('verify', session_id))
        if self.verify_automatically:
            future.set_result(SimpleNamespace(accepted=self.verify_accepted))
        return future

    def speak(self, text, playback_id):
        self.events.append(('speak', text, playback_id))
        return self.speech_available

    def stop(self, playback_id):
        self.events.append(('stop', playback_id))

    def complete_work(self):
        self.executor.run_next()
        self.session.tick()

    def ready_input(self, accepted=True):
        self.openings[-1][1].set_result(SimpleNamespace(accepted=accepted))
        self.session.tick()

    def begin_question(self):
        self.session.start()
        self.complete_work()
        assert self.session.phase == 'speaking'
        assert not self.session.session_id and not self.openings

    def played(self):
        self.session.playback(self.session.playback_id, 'finished')
        assert self.session.phase == 'opening'
        self.ready_input()
        assert self.session.phase == 'listening'


def ending(assessment='unknown', help_needed=True):
    return SituationTurn('확인을 마칠게요.', SituationResult(assessment, help_needed))


def test_answer_timeout_starts_only_at_actual_playback_completion():
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.begin_question()
    h.now = 40
    h.session.tick()
    assert h.session.phase == 'speaking' and len(h.calls) == 1
    h.played()
    h.now = 49.999
    h.session.tick()
    assert h.session.phase == 'listening' and len(h.calls) == 1
    h.now = 50
    h.session.tick()
    assert h.session.phase == 'thinking'
    h.complete_work()
    assert [call[0] for call in h.calls] == ['start', 'no_response']
    assert h.outcomes == [SituationResult('unknown', True)]


def test_speech_start_before_ten_seconds_allows_transcript_after_ten_seconds():
    h = Harness(SituationTurn('넘어지셨나요?'), ending('resolved', False))
    h.begin_question()
    h.played()
    sid = h.session.session_id
    h.now = 9.999
    h.session.input_status(sid, 'utterance-1', 'started')
    h.now = 20
    h.session.tick()
    assert h.session.phase == 'hearing'
    assert h.session.transcript(sid, 'utterance-1', '일부러 누웠어요')
    h.complete_work()
    assert [call[0] for call in h.calls] == ['start', 'answer']
    assert h.outcomes == [SituationResult('resolved', False)]


@pytest.mark.parametrize('playback_started', [False, True])
def test_input_before_question_finishes_cannot_stop_it_or_become_an_answer(playback_started):
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.begin_question()
    pid = h.session.playback_id
    if playback_started:
        h.session.playback(pid, 'playing')
    for sid in ('', 'previous-question'):
        h.session.input_status(sid, 'early', 'started')
        assert not h.session.transcript(sid, 'early', 'MBC 뉴스 김지경입니다.')
    h.session.tick()
    assert h.session.phase == 'speaking' and not h.openings
    assert [event[0] for event in h.events] == ['speak']
    assert [call[0] for call in h.calls] == ['start']
    assert not h.executor.jobs and not h.outcomes

    h.played()
    assert not h.session.transcript('previous-question', 'early', '늦은 전사')
    assert h.session.transcript(h.session.session_id, 'answer', '도와주세요')
    h.complete_work()
    assert h.calls[-1] == ('answer', ('도와주세요',))


def test_only_matching_final_finished_opens_input_once_and_ack_starts_answer_timeout():
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.begin_question()
    pid = h.session.playback_id
    h.session.playback('old-playback', 'finished')
    h.session.playback(pid, 'finished', interim=True)
    h.session.playback(pid, 'playing')
    assert not h.openings
    h.now = 20
    h.session.playback(pid, 'finished')
    h.session.playback(pid, 'finished')
    assert len(h.openings) == 1 and h.session.phase == 'opening'
    h.now = 40
    h.ready_input()
    h.now = 49.999
    h.session.tick()
    assert h.session.phase == 'listening'
    h.now = 50
    h.session.tick()
    h.complete_work()
    assert [call[0] for call in h.calls] == ['start', 'no_response']


@pytest.mark.parametrize('failure', ['send', 'response'])
def test_opening_after_playback_failure_is_not_an_answer_or_silence(failure):
    h = Harness()
    h.begin_question()
    if failure == 'send':
        def fail_open(_):
            raise RuntimeError('input unavailable')
        h.open_session = fail_open
    h.session.playback(h.session.playback_id, 'finished')
    if failure == 'response':
        h.openings[-1][1].set_exception(RuntimeError('input unavailable'))
        h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert not h.outcomes and [call[0] for call in h.calls] == ['start']


def test_stopped_question_without_user_input_is_not_user_silence():
    h = Harness()
    h.begin_question()
    h.session.playback(h.session.playback_id, 'stopped')
    h.now = 2
    h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert [call[0] for call in h.calls] == ['start']


def test_each_question_replaces_session_and_ignores_previous_audio_events():
    h = Harness(SituationTurn('넘어지셨나요?'), SituationTurn('도움이 필요하세요?'))
    h.begin_question()
    h.played()
    old_sid, old_pid = h.session.session_id, h.session.playback_id
    assert h.session.transcript(old_sid, 'first', '넘어졌어요')
    h.complete_work()
    new_pid = h.session.playback_id
    assert not h.session.session_id and new_pid != old_pid
    assert ('close', old_sid) in h.events
    h.session.playback(old_pid, 'finished')
    h.session.input_status(old_sid, 'late', 'started')
    h.session.input_status(old_sid, 'late', 'failed')
    assert not h.session.transcript(old_sid, 'late', '늦게 도착한 답변')
    assert h.session.phase == 'speaking' and h.session.utterance_id == ''
    h.played()
    assert h.session.session_id != old_sid
    h.now = 5
    h.session.tick()
    assert h.session.phase == 'listening'


def test_transcript_can_arrive_before_started_and_is_consumed_once():
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.begin_question()
    h.played()
    sid = h.session.session_id
    assert h.session.transcript(sid, 'u1', '도와주세요')
    h.session.input_status(sid, 'u1', 'started')
    assert not h.session.transcript(sid, 'u1', '도와주세요')
    h.complete_work()
    assert [call[0] for call in h.calls] == ['start', 'answer']


def test_opening_started_before_ack_preserves_late_answer_without_speaking_over_it():
    h = Harness(SituationTurn('넘어지셨나요?'), ending('resolved', False))
    h.session.start()
    h.complete_work()
    h.session.playback(h.session.playback_id, 'finished')
    sid = h.session.session_id
    h.session.input_status(sid, 'u1', 'started')
    assert h.session.phase == 'opening'
    assert len([event for event in h.events if event[0] == 'speak']) == 1
    h.now = 1
    h.ready_input()
    assert h.session.phase == 'hearing' and h.session.utterance_id == 'u1'
    assert len([event for event in h.events if event[0] == 'speak']) == 1
    h.now = 20
    h.session.tick()
    assert h.session.phase == 'hearing'
    assert h.session.transcript(sid, 'u1', '일부러 누워 있었어요')
    h.complete_work()
    assert [call[0] for call in h.calls] == ['start', 'answer']
    assert h.outcomes == [SituationResult('resolved', False)]


def test_opening_transcript_before_ack_is_buffered_and_consumed_once_after_ack():
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.session.start()
    h.complete_work()
    h.session.playback(h.session.playback_id, 'finished')
    sid = h.session.session_id
    assert h.session.transcript(sid, 'u1', '도와주세요')
    assert h.session.phase == 'opening' and len(h.calls) == 1
    assert not h.executor.jobs
    assert len([event for event in h.events if event[0] == 'speak']) == 1
    h.ready_input()
    assert h.session.phase == 'thinking'
    assert ('close', sid) in h.events
    assert len([event for event in h.events if event[0] == 'speak']) == 1
    assert not h.session.transcript(sid, 'u1', '중복 답변')
    h.complete_work()
    assert h.calls[-1] == ('answer', ('도와주세요',))
    assert [call[0] for call in h.calls] == ['start', 'answer']
    assert h.outcomes == [SituationResult('unknown', True)]


def test_opening_completed_answer_owns_correlation_before_later_started_event():
    h = Harness(SituationTurn('넘어지셨나요?'), ending('resolved', False))
    h.session.start()
    h.complete_work()
    h.session.playback(h.session.playback_id, 'finished')
    sid = h.session.session_id
    assert h.session.transcript(sid, 'first-answer', '그냥 누워 있었어요')
    h.session.input_status(sid, 'next-utterance', 'started')
    assert not h.session.transcript(sid, 'next-utterance', '도와주세요')
    h.session.input_status(sid, 'next-utterance', 'failed')
    assert not h.session.done
    h.ready_input()
    assert h.session.phase == 'thinking'
    assert not h.session.transcript(sid, 'next-utterance', '도와주세요')
    h.complete_work()
    assert h.calls[-1] == ('answer', ('그냥 누워 있었어요',))
    assert h.outcomes == [SituationResult('resolved', False)]


@pytest.mark.parametrize('utterance_id', ['first-answer', ''])
def test_opening_buffered_answer_preserves_matching_or_session_wide_failure(utterance_id):
    h = Harness()
    h.session.start()
    h.complete_work()
    h.session.playback(h.session.playback_id, 'finished')
    sid = h.session.session_id
    assert h.session.transcript(sid, 'first-answer', '도와주세요')
    h.session.input_status(sid, utterance_id, 'failed')
    assert h.finishes == [('aborted', None)]
    assert h.outcomes == [] and not h.executor.jobs


@pytest.mark.parametrize('early_input', ['started', 'transcript'])
def test_opening_input_is_discarded_if_microphone_ack_rejects_session(early_input):
    h = Harness()
    h.session.start()
    h.complete_work()
    h.session.playback(h.session.playback_id, 'finished')
    sid = h.session.session_id
    if early_input == 'started':
        h.session.input_status(sid, 'u1', 'started')
    else:
        assert h.session.transcript(sid, 'u1', '도와주세요')
    h.ready_input(accepted=False)
    assert h.finishes == [('aborted', None)]
    assert ('close', sid) in h.events
    assert h.outcomes == [] and not h.executor.jobs
    assert [call[0] for call in h.calls] == ['start']
    assert len([event for event in h.events if event[0] == 'speak']) == 1


@pytest.mark.parametrize('failure', ['provider', 'tts', 'stt', 'opening', 'publication'])
def test_operational_failure_does_not_become_a_no_response(failure):
    h = Harness(RuntimeError('provider failure') if failure == 'provider'
                else SituationTurn('넘어지셨나요?'))
    h.speech_available = failure != 'publication'
    h.session.start()
    h.complete_work()
    if failure == 'tts':
        h.session.playback(h.session.playback_id, 'failed')
    elif failure in ('stt', 'opening'):
        h.session.playback(h.session.playback_id, 'finished')
        h.ready_input(accepted=failure != 'opening')
        if failure == 'stt':
            h.session.input_status(h.session.session_id, 'u1', 'started')
            h.session.input_status(h.session.session_id, 'u1', 'failed')
    assert h.finishes == [('aborted', None)]
    assert h.outcomes == []
    assert [call[0] for call in h.calls] == ['start']


def test_session_wide_input_failure_before_speech_is_not_user_silence():
    h = Harness()
    h.begin_question()
    h.played()
    h.session.input_status(h.session.session_id, '', 'failed')
    assert h.finishes == [('aborted', None)]
    assert [call[0] for call in h.calls] == ['start']


@pytest.mark.parametrize('phase', ['thinking', 'opening', 'speaking', 'hearing'])
def test_operation_timeout_does_not_become_user_silence(phase):
    h = Harness()
    h.session.start()
    if phase != 'thinking':
        h.complete_work()
    if phase in ('opening', 'hearing'):
        h.session.playback(h.session.playback_id, 'finished')
    if phase == 'hearing':
        h.ready_input()
    if phase == 'hearing':
        h.session.input_status(h.session.session_id, 'u1', 'started')
    h.now = 60
    h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert all(call[0] != 'no_response' for call in h.calls)


@pytest.mark.parametrize('late_by', [0.0, 0.001])
@pytest.mark.parametrize('operation', ['initial_model', 'answer_model', 'opening'])
def test_completed_future_cannot_bypass_an_elapsed_operation_deadline(operation, late_by):
    h = Harness(SituationTurn('넘어지셨나요?'), ending('resolved', False))
    if operation == 'answer_model':
        h.begin_question()
        h.played()
        h.session.transcript(h.session.session_id, 'u1', '그냥 누워 있었어요')
    else:
        h.session.start()
        if operation == 'opening':
            h.complete_work()
            h.session.playback(h.session.playback_id, 'finished')
    spoken = [event for event in h.events if event[0] == 'speak']
    h.now = 60.0 + late_by
    if operation == 'opening':
        h.openings[-1][1].set_result(SimpleNamespace(accepted=True))
    else:
        h.executor.run_next()
    h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert h.outcomes == []
    assert [event for event in h.events if event[0] == 'speak'] == spoken


@pytest.mark.parametrize('phase,event', [
    ('speaking', 'finished'), ('closing', 'finished'), ('closing', 'stopped'),
    ('opening', 'started'), ('opening', 'transcript'),
    ('speaking', 'started'), ('speaking', 'transcript'), ('hearing', 'transcript'),
    ('checking_silence', 'started'), ('checking_silence', 'transcript'),
])
def test_callback_before_tick_cannot_revive_an_expired_operation(phase, event):
    h = Harness(SituationTurn('넘어지셨나요?'), ending('resolved', False))
    h.session.start()
    h.complete_work()
    if phase != 'speaking':
        h.session.playback(h.session.playback_id, 'finished')
        if phase != 'opening':
            h.ready_input()
    sid, pid = h.session.session_id, h.session.playback_id
    if phase == 'closing':
        h.session.transcript(sid, 'u1', '그냥 누워 있었어요')
        h.complete_work()
        pid = h.session.playback_id
    elif phase == 'hearing':
        h.session.input_status(sid, 'u1', 'started')
    elif phase == 'checking_silence':
        h.verify_automatically = False
        h.now = 10.0
        h.session.tick()
    assert h.session.phase == phase
    delivered = list(h.outcomes)
    h.now = 70.0 if phase == 'checking_silence' else 60.0
    if event in {'finished', 'stopped'}:
        h.session.playback(pid, event)
    elif event == 'started':
        h.session.input_status(sid, 'u1', 'started')
    else:
        assert not h.session.transcript(sid, 'u1', '그냥 누워 있었어요')
    h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert h.outcomes == delivered
    assert all(call[0] != 'no_response' for call in h.calls)


def test_final_result_is_reported_before_closing_audio_finishes():
    result = SituationResult('resolved', False)
    h = Harness(SituationTurn('넘어지셨나요?'), SituationTurn('알겠어요.', result))
    h.begin_question()
    h.played()
    assert h.session.transcript(h.session.session_id, 'u1', '그냥 누워 있었어요')
    h.complete_work()
    assert h.outcomes == [result]
    assert h.finishes == [] and h.session.phase == 'closing'
    assert h.events[-2][0] == 'result' and h.events[-1][0] == 'speak'
    h.session.playback(h.session.playback_id, 'finished')
    assert h.finishes == [('succeeded', result)]
    h.session.tick()
    assert h.outcomes == [result]


def test_closing_audio_failure_does_not_retract_already_delivered_judgment():
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.begin_question()
    h.played()
    h.session.transcript(h.session.session_id, 'u1', '도와주세요')
    h.complete_work()
    result = h.outcomes[0]
    h.session.playback(h.session.playback_id, 'failed')
    assert h.outcomes == [result] and h.finishes == [('aborted', None)]


def test_cancel_ignores_late_running_inference_completion():
    h = Harness()
    h.session.start()
    future, fn = h.executor.jobs.popleft()
    assert future.set_running_or_notify_cancel()
    h.session.cancel()
    future.set_result(fn())
    h.session.tick()
    h.session.cancel()
    assert h.finishes == [('canceled', None)]
    assert h.openings == [] and h.events == [] and h.outcomes == []


def test_cancel_ignores_late_session_open_acknowledgment():
    h = Harness()
    h.session.start()
    h.complete_work()
    h.session.playback(h.session.playback_id, 'finished')
    sid, future = h.openings[-1]
    assert future.set_running_or_notify_cancel()
    h.session.cancel()
    future.set_result(SimpleNamespace(accepted=True))
    h.session.tick()
    assert h.finishes == [('canceled', None)]
    assert ('close', sid) in h.events
    assert len([event for event in h.events if event[0] == 'speak']) == 1


def test_input_close_failure_aborts_only_the_confirmation():
    h = Harness()
    h.begin_question()
    h.played()
    sid = h.session.session_id

    def fail_send(_):
        raise RuntimeError('service send failed')

    h.close_session = fail_send
    assert not h.session.transcript(sid, 'u1', '도와주세요')
    assert h.session.done and h.finishes == [('aborted', None)]
    assert h.outcomes == [] and not h.executor.jobs
    assert all(call[0] != 'no_response' for call in h.calls)


@pytest.mark.parametrize('termination', ['cancel', 'abort'])
def test_cleanup_service_failures_do_not_escape_or_block_completion(termination):
    h = Harness()
    h.begin_question()
    h.played()
    failures = []

    def fail_close(_):
        failures.append('close')
        raise RuntimeError('input service unavailable')

    def fail_stop(_):
        failures.append('stop')
        raise RuntimeError('playback service unavailable')

    h.close_session = fail_close
    h.stop = fail_stop
    getattr(h.session, termination)()
    getattr(h.session, termination)()
    assert failures == ['close', 'stop']
    expected = 'canceled' if termination == 'cancel' else 'aborted'
    assert h.finishes == [(expected, None)]
    assert h.session.done and not h.session.session_id


def test_no_response_cleanup_failure_is_not_submitted_as_user_silence():
    h = Harness()
    h.begin_question()
    h.played()

    def fail_close(_):
        raise RuntimeError('input service unavailable')

    h.close_session = fail_close
    h.now = 10
    h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert [call[0] for call in h.calls] == ['start']


def test_stt_restart_missing_original_session_aborts_instead_of_no_response():
    h = Harness()
    h.begin_question()
    h.played()
    sid = h.session.session_id
    h.verify_accepted = False
    h.now = 10
    h.session.tick()
    assert h.verifications[0][0] == sid
    assert h.finishes == [('aborted', None)]
    assert h.outcomes == [] and [call[0] for call in h.calls] == ['start']


def test_verified_active_session_is_closed_before_no_response_judgment():
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.begin_question()
    h.played()
    sid = h.session.session_id
    h.verify_automatically = False
    h.now = 10
    h.session.tick()
    assert h.session.phase == 'checking_silence'
    assert ('close', sid) not in h.events
    assert [call[0] for call in h.calls] == ['start']
    h.verifications[-1][1].set_result(SimpleNamespace(accepted=True))
    h.session.tick()
    assert ('close', sid) in h.events
    h.complete_work()
    assert [call[0] for call in h.calls] == ['start', 'no_response']


@pytest.mark.parametrize('event', ['started', 'transcript'])
def test_real_answer_during_silence_check_invalidates_late_rejected_query(event):
    h = Harness(SituationTurn('넘어지셨나요?'), ending('resolved', False))
    h.begin_question()
    h.played()
    sid = h.session.session_id
    h.verify_automatically = False
    h.now = 10
    h.session.tick()
    future = h.verifications[-1][1]
    assert future.set_running_or_notify_cancel()
    h.now = 69.999
    if event == 'started':
        h.session.input_status(sid, 'u1', 'started')
        assert h.session.phase == 'hearing'
    else:
        assert h.session.transcript(sid, 'u1', '그냥 누워 있었어요')
    future.set_result(SimpleNamespace(accepted=False))
    h.session.tick()
    assert h.finishes == []
    if event == 'started':
        assert h.session.transcript(sid, 'u1', '그냥 누워 있었어요')
    h.complete_work()
    assert [call[0] for call in h.calls] == ['start', 'answer']
    assert h.outcomes == [SituationResult('resolved', False)]


@pytest.mark.parametrize('failure', ['send', 'response', 'timeout'])
def test_silence_verification_failure_is_an_operational_abort(failure):
    h = Harness()
    h.begin_question()
    h.played()
    h.verify_automatically = False
    if failure == 'send':
        def fail_send(_):
            raise RuntimeError('verification service send failed')
        h.verify_session = fail_send
    h.now = 10
    h.session.tick()
    if failure == 'response':
        h.verifications[-1][1].set_exception(RuntimeError('verification failed'))
        h.session.tick()
    elif failure == 'timeout':
        h.now = 69.999
        h.session.tick()
        assert h.session.phase == 'checking_silence'
        h.now = 70
        h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert h.outcomes == [] and [call[0] for call in h.calls] == ['start']


@pytest.mark.parametrize('next_turn', [SituationTurn('도움이 필요하세요?'), ending()])
@pytest.mark.parametrize('outcome', ['accepted', 'rejected', 'error', 'timeout', 'cancel'])
def test_next_question_or_closing_waits_for_input_close_ack(next_turn, outcome):
    h = Harness(SituationTurn('넘어지셨나요?'), next_turn)
    h.begin_question()
    h.played()
    old_sid = h.session.session_id
    close = Future()
    h.close_session = lambda _sid: close
    assert h.session.transcript(old_sid, 'u1', '넘어졌어요')
    h.complete_work()
    assert h.session.phase == 'thinking' and not h.session.session_id
    assert [event[0] for event in h.events].count('speak') == 1
    assert not h.outcomes
    h.session.input_status(old_sid, 'late', 'started')
    assert not h.session.transcript(old_sid, 'late', '늦은 전사')
    if outcome == 'cancel':
        h.session.cancel()
        assert close.cancelled()
    elif outcome == 'timeout':
        h.now = 60
    elif outcome == 'error':
        close.set_exception(RuntimeError('close failed'))
    else:
        close.set_result(SimpleNamespace(accepted=outcome == 'accepted'))
    h.session.tick()
    if outcome == 'accepted':
        assert [event[0] for event in h.events].count('speak') == 2
        assert h.outcomes == ([next_turn.result] if next_turn.result else [])
    else:
        assert h.finishes == [('canceled' if outcome == 'cancel' else 'aborted', None)]
        assert [event[0] for event in h.events].count('speak') == 1
        assert not h.outcomes
