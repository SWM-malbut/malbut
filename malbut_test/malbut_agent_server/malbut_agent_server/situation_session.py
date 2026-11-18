"""Correlate one confirmation dialogue with actual audio, independently of ROS."""

from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4


ANSWER_WAIT_SECONDS = 10.0
OPERATION_TIMEOUT_SECONDS = 60.0


class SituationSpeechSession:
    """Only silence after a played question is a user no-response.

    Ports provide ``open_session`` (a future yielding an accepted response),
    ``verify_session`` (a read-only active-session check), ``close_session``
    (a future yielding an accepted response),
    ``speak(text, playback_id)`` and ``stop(playback_id)``.
    Each completed question opens a new microphone session to reject late STT results.
    Input stays closed while a question is prepared or played, including with AEC.
    All public methods are owned by the event-loop thread; inference is isolated.
    """

    def __init__(self, request, engine_factory, ports, finished, *,
                 clock=time.monotonic, executor=None, on_result=None):
        self.request = request
        self.engine_factory = engine_factory
        self.ports = ports
        self.finished = finished
        self.on_result = on_result
        self.clock = clock
        self._started_at = clock()
        self.executor = executor or ThreadPoolExecutor(max_workers=1)
        self._owns_executor = executor is None
        self.engine = None
        self.phase = 'new'
        self.session_id = ''
        self._last_session_id = ''
        self.playback_id = ''
        self.utterance_id = ''
        self._heard = set()
        self._work = None
        self._opening = None
        self._closing_input = None
        self._verifying = None
        self._turn = None
        self._deadline = None
        self._answer_deadline = None
        self._outcome = None
        self._early_started = ''
        self._early_answer = None
        self.no_response = False
        self.failure = None
        self._work_method = None
        self._input_retry_used = False
        self._input_retry_deadline = None

    @property
    def done(self):
        return self.phase == 'done'

    def start(self):
        if self.phase != 'new':
            raise RuntimeError('confirmation already started')
        self._submit('start', self.request.request_id,
                     self.request.situation_type, self.request.summary)

    def _submit(self, method, *args):
        self.phase = 'thinking'
        self._work_method = method
        self.no_response = method == 'no_response'
        self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS

        def run():
            if self.engine is None:
                self.engine = self.engine_factory()
            return getattr(self.engine, method)(*args)

        self._work = self.executor.submit(run)

    def tick(self):
        if self.done or self._expire_operation():
            return
        operation = 'input_close'
        try:
            if self._closing_input is not None:
                if not self._closing_input.done():
                    return
                closing, self._closing_input = self._closing_input, None
                if not closing.result().accepted:
                    raise RuntimeError('microphone session closure rejected')
            if self.phase == 'retrying':
                self.playback_id = 'confirmation-' + uuid4().hex
                self.utterance_id = ''
                self.phase = 'replaying'
                operation = 'input_retry_speech'
                if not self.ports.speak(self._turn.text, self.playback_id):
                    raise RuntimeError('speech publication failed')
            if self._work is not None and self._work.done():
                operation = 'model_' + self._work_method
                work, self._work = self._work, None
                self._turn = work.result()
                self._outcome = self._turn.result
                self.playback_id = 'confirmation-' + uuid4().hex
                self.utterance_id = ''
                self._early_started = ''
                self._early_answer = None
                self._answer_deadline = None
                if self._outcome is not None:
                    self.phase = 'closing'
                    self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
                    if self.on_result is not None:
                        operation = 'result_record'
                        self.on_result(self._outcome)
                    if self.no_response:
                        # Silence only completes the record; it adds no speech.
                        self._finish('succeeded', self._outcome)
                        return
                else:
                    self.phase = 'speaking'
                    self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
                operation = 'speech_publish'
                if not self.ports.speak(self._turn.text, self.playback_id):
                    raise RuntimeError('speech publication failed')
            if self._opening is not None and self._opening.done():
                operation = 'input_open'
                opening, self._opening = self._opening, None
                if not opening.result().accepted:
                    raise RuntimeError('microphone session rejected')
                self._answer_deadline = self._input_deadline(ANSWER_WAIT_SECONDS)
                if self._early_started or self._early_answer:
                    # STT can publish a VAD event before its service ACK arrives.
                    # The question has finished; preserve its answer across the ACK race.
                    self.phase = 'hearing'
                    self.utterance_id = self._early_started
                    self._deadline = self._input_deadline(OPERATION_TIMEOUT_SECONDS)
                    if self._early_answer:
                        uid, text = self._early_answer
                        self.transcript(self.session_id, uid, text)
                else:
                    self.phase = 'listening'
                    self._deadline = self._answer_deadline
            if self._deadline is not None and self.clock() >= self._deadline:
                if self.phase == 'listening':
                    if self._input_retry_deadline is not None:
                        self.abort('input_retry_no_response')
                        return
                    # A restarted STT can be reachable while its original
                    # wake-free session is gone. Verify before calling it silence.
                    self.phase = 'checking_silence'
                    self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
                    operation = 'input_verify'
                    self._verifying = self.ports.verify_session(self.session_id)
                else:
                    # TTS, STT and model failures cannot fabricate a user's silence.
                    self.abort('operation_timeout')
            if self._verifying is not None and self._verifying.done():
                operation = 'input_verify'
                verifying, self._verifying = self._verifying, None
                if not verifying.result().accepted:
                    raise RuntimeError('microphone session no longer active')
                operation = 'input_close'
                self._close_input()
                operation = 'silence_finalize'
                self._submit('no_response')
        except Exception as error:
            self.abort(operation, error)

    def playback(self, playback_id, state, *, interim=False):
        if (self.done or interim or playback_id != self.playback_id
                or self._expire_operation()):
            return
        if state == 'finished':
            if self.phase == 'closing':
                self._finish('succeeded', self._outcome)
            elif self.phase in ('speaking', 'replaying'):
                self.session_id = 'confirmation-' + uuid4().hex
                self._last_session_id = self.session_id
                self.phase = 'opening'
                self._deadline = self._input_deadline(OPERATION_TIMEOUT_SECONDS)
                try:
                    self._opening = self.ports.open_session(self.session_id)
                except Exception as error:
                    self.abort('input_open', error)
        elif state in ('failed', 'stopped') and self.phase in ('speaking', 'replaying', 'closing'):
            # An undelivered question is an operational failure, never an answer.
            self.abort('playback_' + state)

    def input_status(self, session_id, utterance_id, state):
        if (self.done or not self.session_id or session_id != self.session_id
                or self._expire_operation()):
            return
        if state == 'failed' and not utterance_id:
            # The existing empty-ID failure covers the whole listening session.
            self.abort('input_failed')
            return
        if not isinstance(utterance_id, str) or not utterance_id.strip():
            return
        if utterance_id in self._heard:
            return
        if state == 'incomplete':
            current = self.utterance_id if self.phase == 'hearing' else self._early_started
            if self.phase not in ('hearing', 'opening') or current != utterance_id:
                return
            if self.request.situation_type != 'fall' or self._input_retry_used:
                self.abort('input_failed')
                return
            self._input_retry_used = True
            self._input_retry_deadline = self._deadline
            self._heard.add(utterance_id)
            self._clear_verification()
            if self._opening is not None:
                self._opening.cancel()
                self._opening = None
            self._early_started, self._early_answer = '', None
            self.phase = 'retrying'
            try:
                self._close_input()
            except Exception as error:
                self.abort('input_close', error)
            return
        if state == 'failed':
            current = self.utterance_id or self._early_started
            if (self._input_retry_deadline is not None
                    and self.phase in ('opening', 'listening', 'hearing', 'checking_silence')
                    and (not current or current == utterance_id)):
                self.abort('input_retry_failed')
                return
            # STT rejects non-speech/empty decodes for one utterance, while
            # operational faults fail the session with an empty utterance ID.
            if self.phase == 'opening':
                if self._early_answer or (
                        self._early_started and self._early_started != utterance_id):
                    return
                self._early_started = ''
            elif self.phase == 'hearing':
                if self.utterance_id != utterance_id:
                    return
                self.utterance_id = ''
                self._early_started = ''
                self.phase = 'listening'
                # Repeated noise must not restart or extend the ten-second wait.
                self._deadline = self._answer_deadline
            elif self.phase not in ('listening', 'checking_silence'):
                return
            self._heard.add(utterance_id)
            return
        if state != 'started':
            return
        if self.phase == 'opening':
            if not self._early_started:
                self._early_started = utterance_id
            return
        if self.phase in ('listening', 'checking_silence'):
            self._clear_verification()
            self.utterance_id = utterance_id
            self.phase = 'hearing'
            self._deadline = self._input_deadline(OPERATION_TIMEOUT_SECONDS)

    def transcript(self, session_id, utterance_id, text):
        if (self.done or not self.session_id or session_id != self.session_id
                or not isinstance(utterance_id, str) or not utterance_id.strip()
                or not isinstance(text, str) or not text.strip()
                or len(text) > 16000 or utterance_id in self._heard
                or self._expire_operation()):
            return False
        if self.phase == 'opening':
            if self._early_started and self._early_started != utterance_id:
                return False
            if self._early_answer is None:
                self._early_started = utterance_id
                self._early_answer = (utterance_id, text)
            return True
        if self.phase not in (
                'listening', 'hearing', 'checking_silence'):
            return False
        if self.utterance_id and self.utterance_id != utterance_id:
            return False
        self._heard.add(utterance_id)
        self._clear_verification()
        operation = 'input_close'
        try:
            self._close_input()
            self._input_retry_deadline = None  # A complete answer resumes normal model work.
            operation = 'model_submit'
            self._submit('answer', text)
        except Exception as error:
            self.abort(operation, error)
            return False
        return True

    def _expire_operation(self):
        """A ready future or late callback cannot renew an expired operation."""
        if (self._input_retry_deadline is not None
                and self.clock() >= self._input_retry_deadline):
            self.abort('input_retry_timeout')
            return True
        if (self.phase != 'listening' and self._deadline is not None
                and self.clock() >= self._deadline):
            self.abort('operation_timeout')
            return True
        return False

    def _input_deadline(self, seconds):
        deadline = self.clock() + seconds
        return (min(deadline, self._input_retry_deadline)
                if self._input_retry_deadline is not None else deadline)

    def _clear_verification(self):
        if self._verifying is not None:
            verifying, self._verifying = self._verifying, None
            try:
                verifying.cancel()
            except Exception:
                pass

    def _close_input(self):
        if self.session_id:
            old, self.session_id = self.session_id, ''
            closing = self.ports.close_session(old)
            if not self.done:
                # A delayed old session can otherwise STOP the next question.
                self._closing_input = closing

    def cancel(self):
        self._finish('canceled')

    def abort(self, reason='external_abort', error=None):
        if self.done:
            return
        # Keep correlation and failure type, never questions, answers or exception text.
        self.failure = dict(
            request_id=self.request.request_id, phase=self.phase, reason=reason,
            session_id=self.session_id or self._last_session_id, playback_id=self.playback_id,
            utterance_id=self.utterance_id, model_operation=self._work_method,
            elapsed_s=round(self.clock() - self._started_at, 3),
            exception_type=type(error).__name__ if error is not None else None,
        )
        self._finish('aborted')

    def _finish(self, status, result=None):
        if self.done:
            return
        self.phase = 'done'
        self._deadline = None
        for operation in (self._work, self._opening, self._closing_input, self._verifying):
            if operation is not None:
                try:
                    operation.cancel()
                except Exception:
                    pass
        self._work = self._opening = self._closing_input = self._verifying = None
        try:
            self._close_input()
        except Exception:
            pass
        try:
            if status != 'succeeded' and self.playback_id:
                self.ports.stop(self.playback_id)
        except Exception:
            pass
        try:
            if self._owns_executor:
                self.executor.shutdown(wait=False, cancel_futures=True)
        finally:
            self.finished(status, result)
