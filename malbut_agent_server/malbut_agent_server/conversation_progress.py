"""Request-local retry allowance and cancellable speech progress notices."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import RLock, Timer


DELAY_SECONDS = 5.0
DELAY_NOTICE = '답변을 준비하는 데 조금 시간이 걸리고 있어요.'
MODEL_RETRY_NOTICE = '답변 생성을 한 번 다시 시도할게요.'
WEATHER_RETRY_NOTICE = '날씨 정보 조회를 한 번 다시 시도할게요.'
_current = ContextVar('conversation_progress', default=None)


class RequestProgress:
    def __init__(self, notify=None):
        self._lock = RLock()
        self.active = True
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
            self._retry_notice = text
        self._notice(text)
        return True

    def publish(self, publish, text):
        with self._lock:
            published = self.active and publish(text)
            if published and text == self._retry_notice:
                self._retry_published = True
            return published

    def final_text(self, text):
        """A fast retry still gets one receipt even if no progress was drained."""
        with self._lock:
            if self._retry_notice is None or self._retry_published:
                return text
            self._retry_published = True
            return self._retry_notice.replace('시도할게요.', '시도했어요.') + ' ' + text

    def finish(self):
        with self._lock:
            self.active = False
            if self._timer is not None:
                self._timer.cancel()


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
    finally:
        progress.finish()
        _current.reset(token)


def conversation_request(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with request_scope():
            return method(*args, **kwargs)
    return wrapped
