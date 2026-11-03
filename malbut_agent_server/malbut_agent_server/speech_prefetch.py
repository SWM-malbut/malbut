"""Request-local hints for silent speech preparation, never execution authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from uuid import uuid4


_current = ContextVar('speech_prefetch', default=None)
MAX_CANDIDATE_CHARS = 512


@dataclass(frozen=True)
class Preparation:
    """Internal typed command mapped to SpeechPreparation on the ROS thread."""

    operation: str
    request_id: str
    generation_id: str
    prepared_id: str
    text: str = ''


class PrefetchTurn:
    """Bind one candidate to one provider attempt and a final validated text."""

    def __init__(self, request_id, utterance_id, emit, *, lock=None, current=None):
        self.request_id = request_id
        self.utterance_id = utterance_id
        self._emit = emit
        self._lock = lock if lock is not None else RLock()
        self._current = current or (lambda: True)
        self._closed = False
        self._candidate = None
        self._generation = ''
        self._approved_text = None
        self._started = monotonic()
        self.metrics = {}

    def mark(self, event):
        """Retain bounded, content-free durations for the first selected event."""
        with self._lock:
            self.metrics.setdefault(event + '_ms', round((monotonic() - self._started) * 1000, 3))

    def begin_attempt(self):
        with self._lock:
            self._discard()
            if self._closed or not self._current():
                return False
            self._generation = uuid4().hex
            self._approved_text = None
            return True

    def prepare(self, text):
        """Enqueue at most one quiet candidate; transport errors are cache misses."""
        with self._lock:
            if (self._closed or not self._current() or self._candidate is not None
                    or not self._generation or not isinstance(text, str)
                    or not text or len(text) > MAX_CANDIDATE_CHARS):
                return
            try:
                text.encode('utf-8')
                candidate = Preparation('prepare', self.utterance_id,
                                        self._generation, uuid4().hex, text)
                if self._emit(candidate) is not False:
                    self._candidate = candidate
                    self.mark('candidate_enqueued')
            except Exception:
                self.mark('prepare_skipped')

    def approve_provider(self, decision):
        """Provider validation is necessary but does not authorize publication."""
        with self._lock:
            if (decision.type == 'message' and self._candidate is not None
                    and decision.message.startswith(self._candidate.text)):
                self._approved_text = decision.message
                self.mark('provider_validated')
            else:
                self._discard()

    def hint(self, final_text):
        """Called only after normal orchestration and publication guards pass."""
        with self._lock:
            if (self._closed or not self._current() or self._candidate is None
                    or self._approved_text != final_text):
                self._discard()
                return {}
            return {'prepared_id': self._candidate.prepared_id,
                    'generation_id': self._candidate.generation_id}

    def _discard(self):
        candidate, self._candidate = self._candidate, None
        self._approved_text = None
        if candidate is not None:
            try:
                self._emit(Preparation('cancel', candidate.request_id,
                                       candidate.generation_id, candidate.prepared_id))
            except Exception:
                pass  # TTS also bounds stale work by generation, TTL and final text.
            self.mark('candidate_discarded')

    def fail_attempt(self):
        """Discard a failed attempt while allowing an explicit provider retry."""
        with self._lock:
            self._discard()

    def cancel(self):
        with self._lock:
            self._closed = True
            self._discard()


@contextmanager
def prefetch_scope(turn):
    """Expose hints only to this foreground call, never background workers."""
    token = _current.set(turn)
    try:
        yield
    finally:
        _current.reset(token)


def current_prefetch(request_id):
    turn = _current.get()
    return turn if turn is not None and turn.request_id == request_id else None
