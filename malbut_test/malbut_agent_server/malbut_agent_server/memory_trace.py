"""Content-free SQLite evidence for memory experiments, never model authority.

Write events join the caller's transaction: a rolled-back save has no receipt.
Only identifiers, versions, counts and hashes are retained. Dialogue and memory
text remain in their canonical tables so deletion does not leave a log copy.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
import sqlite3
import time


_observer = ContextVar('malbut_memory_input_observer', default=None)
_logger = logging.getLogger(__name__)


def record_trace(connection, event, user_id, *, request_id='', conversation_id='',
                 turn_id='', data=None):
    """Best-effort observation inside an existing transaction; never commit it."""
    try:
        if not connection.in_transaction:
            return False
        connection.execute('''CREATE TABLE IF NOT EXISTS memory_trace_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            wall_time REAL NOT NULL, event TEXT NOT NULL, user_id TEXT NOT NULL,
            request_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            turn_id TEXT NOT NULL, data_json TEXT NOT NULL)''')
        connection.execute('''CREATE INDEX IF NOT EXISTS memory_trace_request_idx
            ON memory_trace_events(user_id, request_id, sequence)''')
        connection.execute('''INSERT INTO memory_trace_events
            (wall_time,event,user_id,request_id,conversation_id,turn_id,data_json)
            VALUES (?,?,?,?,?,?,?)''', (
                time.time(), event, user_id, request_id, conversation_id, turn_id,
                json.dumps(data or {}, ensure_ascii=False, allow_nan=False)))
        return True
    except (sqlite3.Error, ValueError, TypeError):
        _logger.warning('Memory trace unavailable (%s)', event)
        return False


@contextmanager
def observe_inputs(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def model_input_ready(text, *, provider, model):
    """Observe the bounded payload immediately before the provider transport."""
    callback = _observer.get()
    if callback is None:
        return
    try:
        context = json.loads(text[text.index('{'):])
        management = context.get('memory_management_context', {})
        stories = management.get('story_memory_untrusted', {})
        callback('model_input', {
            'provider': provider, 'model': model,
            'input_chars': len(text),
            'input_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'fact_ids': [item['id'] for item in context.get('memory_context_untrusted', [])],
            'stories': [{'story_id': item['story_id'], 'version': item.get('version')}
                        for item in stories.get('stories', [])],
            'evidence_count': len(stories.get('evidence', [])),
            'history_messages': len(context.get('conversation_history_untrusted', [])),
            'summary_present': bool(context.get('conversation_summary_untrusted')),
            'context_truncated': bool(context.get('context_truncated')),
        })
    except Exception:
        # Observability must never change provider success/failure behavior.
        _logger.warning('Memory input observation unavailable')
