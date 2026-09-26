"""Fence live inference before recovering turns abandoned by a process crash."""

from contextlib import contextmanager
import fcntl
from functools import wraps
import hashlib
import os
from pathlib import Path

from malbut_agent_server.conversation import ConversationConflictError


@contextmanager
def conversation_ownership(store, user_id, conversation_id, *, blocking=True):
    """Hold the same OS lock for recovery and every persisted model turn."""
    if store.database_path == ':memory:':
        yield True
        return
    # A different session must remain able to revoke memory during inference.
    identity = hashlib.sha256((user_id + '\0' + conversation_id).encode()).hexdigest()
    lock_path = (str(Path(store.database_path).expanduser().resolve())
                 + '.conversation-' + identity + '.lock')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


def _discard_abandoned_turns(store, user_id, conversation_id):
    """The caller holds ownership; completed context and its clock stay intact."""
    with store._lock, store._connection:
        store._connection.execute(
            "DELETE FROM conversation_turns WHERE status = 'pending' "
            'AND user_id = ? AND conversation_id = ?',
            (user_id, conversation_id),
        )


def recover_abandoned_turns(store):
    """Recover at startup, or defer until a live owner's request finishes."""
    if store.database_path == ':memory:':
        return set()
    with store._lock:
        rows = store._connection.execute(
            "SELECT DISTINCT user_id, conversation_id FROM conversation_turns "
            "WHERE status = 'pending'",
        ).fetchall()
    deferred = set()
    for row in rows:
        identity = (row['user_id'], row['conversation_id'])
        with conversation_ownership(store, *identity, blocking=False) as acquired:
            if acquired:
                _discard_abandoned_turns(store, *identity)
            else:
                deferred.add(identity)
    return deferred


def owned_conversation_request(method):
    """Keep recovery from removing another process's in-flight reservation."""
    @wraps(method)
    def wrapped(self, request, *args, **kwargs):
        identity = (request.user_id, request.conversation_id)
        with self._handle_lock:
            with conversation_ownership(
                self.conversation_store, *identity, blocking=False,
            ) as acquired:
                if not acquired:
                    raise ConversationConflictError('another turn is already in progress')
                # File ownership also fences turns abandoned after startup.
                # In-memory stores have no OS lock, so preserve their reservations.
                if self.conversation_store.database_path != ':memory:':
                    _discard_abandoned_turns(self.conversation_store, *identity)
                    self._deferred_conversation_recovery.discard(identity)
                return method(self, request, *args, **kwargs)
    return wrapped
