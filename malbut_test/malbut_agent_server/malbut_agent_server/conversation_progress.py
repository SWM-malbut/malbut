"""Request-local retry allowance and cancellable speech progress notices."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import RLock, Timer

from malbut_agent_server.function_speech import FUNCTION_STARTS

DELAY_SECONDS = 5.0
DELAY_NOTICE = '답변을 준비하는 데 조금 시간이 걸리고 있어요.'
MODEL_RETRY_NOTICE = '답변 생성을 한 번 다시 시도할게요.'
WEATHER_RETRY_NOTICE = '날씨 정보 조회를 한 번 다시 시도할게요.'
SERVICE_UNAVAILABLE_NOTICE = '지금은 대화를 할 수 없어요.'
_current = ContextVar('conversation_progress', default=None)


class RequestProgress:
    def __init__(self, notify=None):
        self._lock = RLock()
        self.active = True
        self._aborted = False
        self._started = set()
        self._published_starts = set()
        self._retried = False
        self._retry_notice = None
        self._retry_published = False
        self._notify = notify
        self._timer = None
        if notify is not None:
            self._timer = Timer(DELAY_SECONDS, self._notice, args=(DELAY_NOTICE,))
            self._timer.daemon = True
            self._timer.start()

    def _notice(self, text):
        # Publication checks active again; never call the worker under this lock.
        with self._lock:
            notify = self._notify if self.active else None
        if notify is not None:
            notify(text, self)

    def retry(self, text):
        with self._lock:
            if not self.active or self._retried:
                return False
            self._retried = True
            if self._started:
                return True
            self._retry_notice = text
        self._notice(text)
        return True

    def start_function(self, tool):
        with self._lock:
            if not self.active or tool not in FUNCTION_STARTS or tool in self._started:
                return
            self._started.add(tool)
            if self._timer is not None:
                self._timer.cancel()
        self._notice(FUNCTION_STARTS[tool])

    def can_publish(self, text):
        with self._lock:
            return (not self._aborted and text not in self._published_starts
                    and not (self._started and text in {
                        DELAY_NOTICE, MODEL_RETRY_NOTICE, WEATHER_RETRY_NOTICE,
                    })
                    and (self.active or text in FUNCTION_STARTS.values()))

    def publish(self, publish, text):
        with self._lock:
            published = self.can_publish(text) and publish(text)
            if published and text in FUNCTION_STARTS.values():
                self._published_starts.add(text)
            if published and text == self._retry_notice:
                self._retry_published = True
            return published

    def final_text(self, text):
        """A fast retry still gets one receipt even if no progress was drained."""
        with self._lock:
            # Keep the prerecorded failure notice recognizable by TTS.
            if text == SERVICE_UNAVAILABLE_NOTICE:
                return text
            if self._started or self._retry_notice is None or self._retry_published:
                return text
            self._retry_published = True
            return self._retry_notice.replace('시도할게요.', '시도했어요.') + ' ' + text

    def finish(self, *, cancelled=False):
        with self._lock:
            self.active = False
            self._aborted = self._aborted or cancelled
            if self._timer is not None:
                self._timer.cancel()


def announce_function_start(tool):
    """Emit one fixed receipt per function, even when a read is retried."""
    progress = _current.get()
    if progress is not None:
        progress.start_function(tool)


def claim_retry(text):
    """Standalone adapters retain their config; conversation requests retry once."""
    progress = _current.get()
    return progress is None or progress.retry(text)


def in_request():
    return _current.get() is not None


@contextmanager
def request_scope(notify=None, *, progress=None):
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    progress = progress or RequestProgress(notify)
    token = _current.set(progress)
    try:
        yield progress
    except BaseException:
        progress.finish(cancelled=True)
        raise
    finally:
        progress.finish()
        _current.reset(token)


def conversation_request(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with request_scope():
            return method(*args, **kwargs)
    return wrapped
