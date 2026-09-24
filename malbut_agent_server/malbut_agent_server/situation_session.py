"""Correlate one confirmation dialogue with actual audio, independently of ROS."""

from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4


ANSWER_WAIT_SECONDS = 10.0
OPERATION_TIMEOUT_SECONDS = 60.0


class SituationSpeechSession:
    """Only silence after a played question is a user no-response.

    Ports provide ``open_session`` (a future yielding an accepted response),
    ``verify_session`` (a read-only active-session check), ``close_session``,
    ``speak(text, playback_id)`` and ``stop(playback_id)``.
    Every question gets its own microphone session to reject late STT results.
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
        self.executor = executor or ThreadPoolExecutor(max_workers=1)
        self._owns_executor = executor is None
        self.engine = None
        self.phase = 'new'
        self.session_id = ''
        self.playback_id = ''
        self.utterance_id = ''
        self._heard = set()
        self._work = None
        self._opening = None
        self._verifying = None
        self._turn = None
        self._deadline = None
        self._outcome = None
        self._early_started = ''
        self._early_answer = None

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
        self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS

        def run():
            if self.engine is None:
                self.engine = self.engine_factory()
            return getattr(self.engine, method)(*args)

        self._work = self.executor.submit(run)

    def tick(self):
        if self.done:
            return
        try:
            if self._work is not None and self._work.done():
                work, self._work = self._work, None
                self._turn = work.result()
                self._outcome = self._turn.result
                self.playback_id = 'confirmation-' + uuid4().hex
                self.utterance_id = ''
                self._early_started = ''
                self._early_answer = None
                if self._outcome is not None:
                    self.phase = 'closing'
                    self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
                    if self.on_result is not None:
                        self.on_result(self._outcome)
                    if not self.ports.speak(self._turn.text, self.playback_id):
                        raise RuntimeError('speech publication failed')
                else:
                    self.session_id = 'confirmation-' + uuid4().hex
                    self.phase = 'opening'
                    self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
                    self._opening = self.ports.open_session(self.session_id)
            if self._opening is not None and self._opening.done():
                opening, self._opening = self._opening, None
                if not opening.result().accepted:
                    raise RuntimeError('microphone session rejected')
                if self._early_started or self._early_answer:
                    # STT can publish a VAD event before its service ACK arrives.
                    # Do not speak over an answer already in progress.
                    self.phase = 'hearing'
                    self.utterance_id = self._early_started
                    self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
                    if self._early_answer:
                        uid, text = self._early_answer
                        self.transcript(self.session_id, uid, text)
                else:
                    self.phase = 'speaking'
                    self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
                    if not self.ports.speak(self._turn.text, self.playback_id):
                        raise RuntimeError('speech publication failed')
            if self._deadline is not None and self.clock() >= self._deadline:
                if self.phase == 'listening':
                    # A restarted STT can be reachable while its original
                    # wake-free session is gone. Verify before calling it silence.
                    self.phase = 'checking_silence'
                    self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
                    self._verifying = self.ports.verify_session(self.session_id)
                else:
                    # TTS, STT and model failures cannot fabricate a user's silence.
                    self._finish('aborted')
            if self._verifying is not None and self._verifying.done():
                verifying, self._verifying = self._verifying, None
                if not verifying.result().accepted:
                    raise RuntimeError('microphone session no longer active')
                self._close_input()
                self._submit('no_response')
        except Exception:
            self._finish('aborted')

    def playback(self, playback_id, state):
        if self.done or playback_id != self.playback_id:
            return
        if state == 'finished':
            if self.phase == 'closing':
                self._finish('succeeded', self._outcome)
            elif self.phase == 'speaking':
                self.phase = 'listening'
                self._deadline = self.clock() + ANSWER_WAIT_SECONDS
        elif state == 'failed':
            self._finish('aborted')
        elif state == 'stopped' and self.phase in ('speaking', 'closing'):
            # A barge-in may arrive on the VAD topic after TTS reports STOPPED.
            # Briefly await that correlated STARTED/Transcript, never start the
            # silence timer for a question that was not completed.
            self.phase = 'interrupted'
            self._deadline = self.clock() + 2.0

    def input_status(self, session_id, utterance_id, state):
        if self.done or not self.session_id or session_id != self.session_id:
            return
        if state == 'failed':
            if (not utterance_id or not self.utterance_id
                    or utterance_id == self.utterance_id):
                self._finish('aborted')
            return
        if not isinstance(utterance_id, str) or not utterance_id.strip():
            return
        if utterance_id in self._heard:
            return
        if state != 'started':
            return
        if self.phase == 'opening':
            if not self._early_started:
                self._early_started = utterance_id
            return
        if self.phase in ('speaking', 'listening', 'interrupted', 'checking_silence'):
            self._clear_verification()
            self.utterance_id = utterance_id
            self.phase = 'hearing'
            self._deadline = self.clock() + OPERATION_TIMEOUT_SECONDS
            try:
                self.ports.stop(self.playback_id)
            except Exception:
                self._finish('aborted')

    def transcript(self, session_id, utterance_id, text):
        if (self.done or not self.session_id or session_id != self.session_id
                or not isinstance(utterance_id, str) or not utterance_id.strip()
                or not isinstance(text, str) or not text.strip()
                or len(text) > 16000 or utterance_id in self._heard):
            return False
        if self.phase == 'opening':
            if self._early_started and self._early_started != utterance_id:
                return False
            if self._early_answer is None:
                self._early_answer = (utterance_id, text)
            return True
        if self.phase not in (
                'speaking', 'listening', 'hearing', 'interrupted', 'checking_silence'):
            return False
        if self.utterance_id and self.utterance_id != utterance_id:
            return False
        self._heard.add(utterance_id)
        self._clear_verification()
        try:
            self.ports.stop(self.playback_id)
            self._close_input()
            self._submit('answer', text)
        except Exception:
            self._finish('aborted')
            return False
        return True

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
            self.ports.close_session(old)

    def cancel(self):
        self._finish('canceled')

    def abort(self):
        self._finish('aborted')

    def _finish(self, status, result=None):
        if self.done:
            return
        self.phase = 'done'
        self._deadline = None
        for operation in (self._work, self._opening, self._verifying):
            if operation is not None:
                try:
                    operation.cancel()
                except Exception:
                    pass
        self._work = self._opening = self._verifying = None
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
