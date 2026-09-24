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
        self.ready_input()
        assert self.session.phase == 'speaking'

    def played(self):
        self.session.playback(self.session.playback_id, 'finished')
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


@pytest.mark.parametrize('stopped_first', [False, True])
def test_barge_in_accepts_started_and_stopped_in_either_order(stopped_first):
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.begin_question()
    sid, pid = h.session.session_id, h.session.playback_id
    if stopped_first:
        h.session.playback(pid, 'stopped')
        assert h.session.phase == 'interrupted'
    h.session.input_status(sid, 'utterance-1', 'started')
    if not stopped_first:
        h.session.playback(pid, 'stopped')
    assert h.session.phase == 'hearing'
    assert ('stop', pid) in h.events
    assert h.session.transcript(sid, 'utterance-1', '도와주세요')
    h.complete_work()
    assert [call[0] for call in h.calls] == ['start', 'answer']


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
    old_sid, old_pid = h.session.session_id, h.session.playback_id
    assert h.session.transcript(old_sid, 'first', '넘어졌어요')
    h.complete_work()
    new_sid, new_pid = h.session.session_id, h.session.playback_id
    assert new_sid != old_sid and new_pid != old_pid
    assert ('close', old_sid) in h.events
    h.ready_input()
    h.session.playback(old_pid, 'finished')
    h.session.input_status(old_sid, 'late', 'started')
    h.session.input_status(old_sid, 'late', 'failed')
    assert not h.session.transcript(old_sid, 'late', '늦게 도착한 답변')
    assert h.session.phase == 'speaking' and h.session.utterance_id == ''
    h.played()
    h.now = 5
    h.session.tick()
    assert h.session.phase == 'listening'


def test_transcript_can_arrive_before_started_and_is_consumed_once():
    h = Harness(SituationTurn('넘어지셨나요?'), ending())
    h.begin_question()
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
    sid = h.session.session_id
    h.session.input_status(sid, 'u1', 'started')
    assert h.session.phase == 'opening'
    assert all(event[0] != 'speak' for event in h.events)
    h.now = 1
    h.ready_input()
    assert h.session.phase == 'hearing' and h.session.utterance_id == 'u1'
    assert all(event[0] != 'speak' for event in h.events)
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
    sid = h.session.session_id
    assert h.session.transcript(sid, 'u1', '도와주세요')
    assert h.session.phase == 'opening' and len(h.calls) == 1
    assert not h.executor.jobs
    assert all(event[0] != 'speak' for event in h.events)
    h.ready_input()
    assert h.session.phase == 'thinking'
    assert ('close', sid) in h.events
    assert all(event[0] != 'speak' for event in h.events)
    assert not h.session.transcript(sid, 'u1', '중복 답변')
    h.complete_work()
    assert h.calls[-1] == ('answer', ('도와주세요',))
    assert [call[0] for call in h.calls] == ['start', 'answer']
    assert h.outcomes == [SituationResult('unknown', True)]


@pytest.mark.parametrize('early_input', ['started', 'transcript'])
def test_opening_input_is_discarded_if_microphone_ack_rejects_session(early_input):
    h = Harness()
    h.session.start()
    h.complete_work()
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
    assert all(event[0] != 'speak' for event in h.events)


@pytest.mark.parametrize('failure', ['provider', 'tts', 'stt', 'opening', 'publication'])
def test_operational_failure_does_not_become_a_no_response(failure):
    h = Harness(RuntimeError('provider failure') if failure == 'provider'
                else SituationTurn('넘어지셨나요?'))
    h.session.start()
    h.complete_work()
    if failure != 'provider':
        if failure == 'publication':
            h.speech_available = False
        h.ready_input(accepted=failure != 'opening')
        if failure == 'tts':
            h.session.playback(h.session.playback_id, 'failed')
        elif failure == 'stt':
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
    if phase in ('speaking', 'hearing'):
        h.ready_input()
    if phase == 'hearing':
        h.session.input_status(h.session.session_id, 'u1', 'started')
    h.now = 60
    h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert all(call[0] != 'no_response' for call in h.calls)


def test_final_result_is_reported_before_closing_audio_finishes():
    result = SituationResult('resolved', False)
    h = Harness(SituationTurn('넘어지셨나요?'), SituationTurn('알겠어요.', result))
    h.begin_question()
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
    sid, future = h.openings[-1]
    assert future.set_running_or_notify_cancel()
    h.session.cancel()
    future.set_result(SimpleNamespace(accepted=True))
    h.session.tick()
    assert h.finishes == [('canceled', None)]
    assert ('close', sid) in h.events
    assert all(event[0] != 'speak' for event in h.events)


@pytest.mark.parametrize('event,failed_port', [
    ('started', 'stop'), ('transcript', 'stop'), ('transcript', 'close_session'),
])
def test_input_service_send_failure_aborts_only_the_confirmation(event, failed_port):
    h = Harness()
    h.begin_question()
    sid = h.session.session_id

    def fail_send(_):
        raise RuntimeError('service send failed')

    setattr(h, failed_port, fail_send)
    if event == 'started':
        h.session.input_status(sid, 'u1', 'started')
    else:
        assert not h.session.transcript(sid, 'u1', '도와주세요')
    assert h.session.done and h.finishes == [('aborted', None)]
    assert h.outcomes == [] and not h.executor.jobs
    assert all(call[0] != 'no_response' for call in h.calls)


@pytest.mark.parametrize('termination', ['cancel', 'abort'])
def test_cleanup_service_failures_do_not_escape_or_block_completion(termination):
    h = Harness()
    h.begin_question()
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
