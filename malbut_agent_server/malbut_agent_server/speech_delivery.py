"""Optional ROS handoff of a validated answer before its SQLite commit.

The caller keeps the final write transaction open during the bounded handoff.
That snapshot fences memory erasure/correction until publication has happened
or the ticket has expired. No model output is delivered before final policy.
"""

from contextlib import contextmanager
from contextvars import ContextVar


_delivery = ContextVar('speech_before_save', default=None)


@contextmanager
def delivery_scope(callback):
    token = _delivery.set(callback)
    try:
        yield
    finally:
        _delivery.reset(token)


def deliver_before_save(result):
    callback = _delivery.get()
    return bool(callback is not None and callback(result))


def delivery_available():
    return _delivery.get() is not None
