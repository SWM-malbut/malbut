"""Durable, bounded automatic-memory jobs with transaction-owned fencing.

This module never starts a worker or invokes a provider. Private source text
and proposals exist only while a job is queued or running; terminal transitions
erase that payload. Conversation and memory authority remain with the caller.
"""

import json
import math
import secrets
import time
from collections.abc import Mapping

from malbut_agent_server.automatic_memory_policy import automatic_deferred
from malbut_agent_server.memory_contract import validate_memory_proposal


_TERMINAL = frozenset({'saved', 'discarded', 'failed'})
_SAFE_REASONS = frozenset({
    'superseded', 'expired', 'disabled', 'deleted', 'reset', 'closed',
    'canceled', 'shutdown', 'stale', 'invalidated',
    'memory_control',
})
_IDENTITY = (
    'user_id', 'request_id', 'conversation_id', 'session_instance_id',
    'generation', 'source_turn_id', 'ordinal', 'expected_session_revision',
    'queue_sequence',
)
_USAGE_FIELDS = ('input_tokens', 'output_tokens', 'total_tokens')


class AutomaticMemoryJobs:
    """Use the conversation store's connection and its existing lock."""

    def __init__(self, conversation_store, max_pending=64,
                 ttl_seconds=120, clock=None):
        """Create queue storage only; do not schedule background work."""
        if type(max_pending) is not int or not 1 <= max_pending <= 64:
            raise ValueError('max_pending must be an integer from 1 to 64')
        if (type(ttl_seconds) not in {int, float}
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0):
            raise ValueError('ttl_seconds must be positive and finite')
        if clock is not None and not callable(clock):
            raise TypeError('clock must be callable')
        self.conversations = conversation_store
        self.max_pending = max_pending
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock if clock is not None else time.time
        with self.conversations._lock:
            conn = self.conversations._connection
            if conn.in_transaction:
                raise RuntimeError(
                    'queue initialization requires no transaction',
                )
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute('''CREATE TABLE IF NOT EXISTS automatic_memory_jobs (
                    user_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    session_instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    source_turn_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
                    expected_session_revision INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'queued', 'running', 'saved', 'discarded', 'failed')),
                    claim TEXT, payload_json TEXT,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    started_at REAL, finished_at REAL, review_ms REAL,
                    reason TEXT,
                    PRIMARY KEY (user_id, request_id),
                    FOREIGN KEY (user_id, conversation_id)
                        REFERENCES conversation_sessions (
                            user_id, conversation_id)
                        ON DELETE CASCADE,
                    CHECK (expires_at > created_at),
                    CHECK (review_ms IS NULL OR review_ms >= 0),
                    CHECK ((state IN ('queued', 'running')
                            AND payload_json IS NOT NULL
                            AND finished_at IS NULL)
                        OR (state IN ('saved', 'discarded', 'failed')
                            AND payload_json IS NULL
                            AND finished_at IS NOT NULL)),
                    CHECK ((state = 'running' AND claim IS NOT NULL)
                        OR (state != 'running' AND claim IS NULL))
                )''')
                conn.execute('''CREATE INDEX IF NOT EXISTS
                    automatic_memory_jobs_pending_idx
                    ON automatic_memory_jobs (state, created_at)''')
                columns = {
                    row['name'] for row in conn.execute(
                        'PRAGMA table_info(automatic_memory_jobs)',
                    )
                }
                for phase in ('review', 'extraction'):
                    for field in _USAGE_FIELDS:
                        column = f'{phase}_{field}'
                        if column not in columns:
                            conn.execute(
                                'ALTER TABLE automatic_memory_jobs '
                                f'ADD COLUMN {column} INTEGER '
                                f'CHECK ({column} IS NULL OR {column} >= 0)',
                            )
                if 'extraction_ms' not in columns:
                    conn.execute('ALTER TABLE automatic_memory_jobs '
                                 'ADD COLUMN extraction_ms REAL '
                                 'CHECK (extraction_ms IS NULL '
                                 'OR extraction_ms >= 0)')
                if 'queue_sequence' not in columns:
                    conn.execute('ALTER TABLE automatic_memory_jobs '
                                 'ADD COLUMN queue_sequence INTEGER')
                # Existing jobs keep their insertion order on first migration.
                # A dedicated counter preserves order across tied timestamps,
                # lexical request IDs, cleanup and database maintenance.
                conn.execute('CREATE TABLE IF NOT EXISTS '
                             'automatic_memory_sequence ('
                             'singleton INTEGER PRIMARY KEY '
                             'CHECK (singleton=1), '
                             'value INTEGER NOT NULL CHECK (value>=0))')
                conn.execute('INSERT INTO automatic_memory_sequence '
                             '(singleton,value) SELECT 1, '
                             'COALESCE(MAX(queue_sequence),0) '
                             'FROM automatic_memory_jobs WHERE TRUE '
                             'ON CONFLICT(singleton) DO UPDATE SET '
                             'value=MAX(value,excluded.value)')
                for legacy in conn.execute(
                    'SELECT rowid FROM automatic_memory_jobs '
                    'WHERE queue_sequence IS NULL ORDER BY rowid',
                ).fetchall():
                    conn.execute('UPDATE automatic_memory_sequence '
                                 'SET value=value+1 WHERE singleton=1')
                    conn.execute('UPDATE automatic_memory_jobs '
                                 'SET queue_sequence=(SELECT value FROM '
                                 'automatic_memory_sequence '
                                 'WHERE singleton=1) '
                                 'WHERE rowid=?', (legacy['rowid'],))
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS '
                             'automatic_memory_sequence_idx '
                             'ON automatic_memory_jobs(queue_sequence)')
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _now(self):
        value = self._clock()
        if (type(value) not in {int, float}
                or not math.isfinite(value) or value < 0):
            raise ValueError('job clock must return a finite nonnegative time')
        return float(value)

    @staticmethod
    def _transaction(conn):
        if not conn.in_transaction:
            raise RuntimeError('automatic memory jobs require a transaction')

    @staticmethod
    def _expire(conn, now):
        conn.execute('''UPDATE automatic_memory_jobs
            SET state='discarded', claim=NULL, payload_json=NULL,
                finished_at=?, reason='expired'
            WHERE state IN ('queued', 'running') AND expires_at <= ?''',
                     (now, now))

    def enqueue(self, conn, request, token, snapshot, result,
                *, extract=False):
        """Append a private job atomically with its completed conversation."""
        self._transaction(conn)
        provider = result.provider_result
        provider.validate()
        proposal = provider.memory_proposal
        if type(extract) is not bool:
            raise ValueError('extract must be a boolean')
        if extract:
            if not automatic_deferred(request, snapshot, result):
                return False
            proposal = None
        else:
            if proposal is None or result.decision.type != 'message':
                return False
            proposal = validate_memory_proposal(proposal)
            if (proposal['operation'] != 'remember' or not proposal['facts']
                    or proposal['target_ids'] or proposal['query']):
                return False
        if conn.execute('''SELECT 1 FROM confirmation_intents
            WHERE user_id=? AND state='pending' LIMIT 1''',
                        (request.user_id,)).fetchone() is not None:
            return False
        source = {
            'user_id': request.user_id,
            'conversation_id': token.conversation_id,
            'session_instance_id': token.session_instance_id,
            'generation': token.generation, 'turn_id': token.turn_id,
            'request_id': token.request_id, 'text': request.utterance,
        }
        if (request.user_id != token.user_id
                or request.conversation_id != token.conversation_id
                or request.request_id != token.request_id
                or request.turn_id != token.turn_id
                or any(snapshot.source.get(key) != value
                       for key, value in source.items())):
            raise ValueError('automatic memory source identity does not match')
        state = {key: snapshot.state[key]
                 for key in ('enabled', 'revision', 'legacy_cutoff')}
        if (type(state['enabled']) is not bool
                or type(state['revision']) is not int or state['revision'] < 0
                or type(state['legacy_cutoff']) not in {int, float}
                or not math.isfinite(state['legacy_cutoff'])
                or state['legacy_cutoff'] < 0):
            raise ValueError('automatic memory policy state is invalid')
        if not state['enabled']:
            return False
        private_payload = {
            'source': source, 'proposal': proposal, 'state': state,
        }
        if extract:
            private_payload.update(version=2, mode='extract')
        payload = json.dumps(
            private_payload,
            ensure_ascii=False, allow_nan=False,
        )
        now = self._now()
        self._expire(conn, now)
        count = conn.execute('''SELECT COUNT(*) FROM automatic_memory_jobs
            WHERE state IN ('queued', 'running')''').fetchone()[0]
        if count >= self.max_pending:
            return False
        conn.execute('UPDATE automatic_memory_sequence '
                     'SET value=value+1 WHERE singleton=1')
        sequence = conn.execute('SELECT value FROM automatic_memory_sequence '
                                'WHERE singleton=1').fetchone()[0]
        cursor = conn.execute('''INSERT OR IGNORE INTO automatic_memory_jobs (
            user_id, request_id, conversation_id, session_instance_id,
            generation, source_turn_id, ordinal, expected_session_revision,
            state, payload_json, created_at, expires_at, queue_sequence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)''', (
                request.user_id, request.request_id, token.conversation_id,
                token.session_instance_id, token.generation, token.turn_id,
                token.ordinal, token.revision + 1, payload,
                now, now + self.ttl_seconds, sequence,
            ))
        return cursor.rowcount == 1

    def claim_next(self):
        """Claim one queued job; never take over an unexpired running job."""
        with self.conversations._lock:
            conn = self.conversations._connection
            if conn.in_transaction:
                raise RuntimeError('claim_next requires no transaction')
            conn.execute('BEGIN IMMEDIATE')
            try:
                now = self._now()
                self._expire(conn, now)
                row = conn.execute('''SELECT j.* FROM automatic_memory_jobs j
                    WHERE j.state='queued' AND j.queue_sequence IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM automatic_memory_jobs earlier
                        WHERE earlier.user_id=j.user_id
                        AND earlier.state IN ('queued', 'running')
                        AND (earlier.queue_sequence IS NULL
                             OR earlier.queue_sequence<j.queue_sequence
                             OR earlier.state='running'))
                    ORDER BY j.queue_sequence
                    LIMIT 1''').fetchone()
                if row is None:
                    conn.commit()
                    return None
                claim = secrets.token_hex(24)
                updated = conn.execute('''UPDATE automatic_memory_jobs
                    SET state='running', claim=?, started_at=?
                    WHERE user_id=? AND request_id=? AND state='queued'
                        AND expires_at>?''', (
                            claim, now, row['user_id'], row['request_id'], now,
                        ))
                if updated.rowcount != 1:
                    conn.commit()
                    return None
                job = dict(row)
                job.update(state='running', claim=claim, started_at=now)
                job['payload'] = json.loads(job.pop('payload_json'))
                conn.commit()
                return job
            except Exception:
                conn.rollback()
                raise

    def valid(self, conn, job):
        """Check lease and original turn; caller checks memory policy."""
        self._transaction(conn)
        row = conn.execute('''SELECT * FROM automatic_memory_jobs
            WHERE user_id=? AND request_id=? AND state='running'
                AND claim=?''', (
                    job['user_id'], job['request_id'], job['claim'],
                )).fetchone()
        now = self._now()
        if (row is None or row['expires_at'] <= now
                or any(row[key] != job[key] for key in _IDENTITY)):
            return False
        origin = conn.execute('''SELECT t.user_content
            FROM conversation_sessions s
            JOIN conversation_turns t
              ON t.user_id=s.user_id AND t.conversation_id=s.conversation_id
                AND t.session_instance_id=s.session_instance_id
                AND t.generation=s.generation
            WHERE s.user_id=? AND s.conversation_id=?
                AND s.session_instance_id=? AND s.generation=?
                AND s.status='active' AND s.revision>=? AND s.expires_at>?
                AND t.request_id=? AND t.turn_id=? AND t.ordinal=?
                AND t.status='completed'
                AND NOT EXISTS (SELECT 1 FROM confirmation_intents c
                    WHERE c.user_id=s.user_id AND c.state='pending')''', (
                    row['user_id'], row['conversation_id'],
                    row['session_instance_id'], row['generation'],
                    row['expected_session_revision'], now, row['request_id'],
                    row['source_turn_id'], row['ordinal'],
                )).fetchone()
        if origin is None:
            return False
        source = job.get('payload', {}).get('source', {})
        if not isinstance(source, dict):
            return False
        return all(source.get(key) == value for key, value in (
            ('user_id', row['user_id']),
            ('conversation_id', row['conversation_id']),
            ('request_id', row['request_id']),
            ('session_instance_id', row['session_instance_id']),
            ('generation', row['generation']),
            ('turn_id', row['source_turn_id']),
            ('text', origin['user_content']),
        ))

    def invalidate_user(self, conn, user_id, except_request_id=None,
                        reason='superseded'):
        """Fence work for invalidating controls, including no-op deletes."""
        self._transaction(conn)
        label = reason if isinstance(reason, str) and reason in (
            _SAFE_REASONS
        ) else 'invalidated'
        cursor = conn.execute('''UPDATE automatic_memory_jobs
            SET state='discarded', claim=NULL, payload_json=NULL,
                finished_at=?, reason=?
            WHERE user_id=? AND state IN ('queued', 'running')
                AND (? IS NULL OR request_id != ?)''', (
                    self._now(), label, user_id,
                    except_request_id, except_request_id,
                ))
        return cursor.rowcount

    @staticmethod
    def _metrics(phase, elapsed_ms, counters):
        if elapsed_ms is not None and (
            type(elapsed_ms) not in {int, float}
            or not math.isfinite(elapsed_ms) or elapsed_ms < 0
        ):
            raise ValueError(f'{phase}_ms must be nonnegative and finite')
        if counters is not None and (
            not isinstance(counters, Mapping)
            or not set(counters).issubset(_USAGE_FIELDS)
        ):
            raise ValueError(f'{phase}_usage must contain only token counters')
        usage = {
            key: counters.get(key) if counters is not None else None
            for key in _USAGE_FIELDS
        }
        if any(value is not None and (
            type(value) is not int or not 0 <= value <= 9223372036854775807
        ) for value in usage.values()):
            raise ValueError(
                f'{phase} usage counters must be nonnegative integers',
            )
        return usage

    def finish(self, conn, job, state, review_ms=None, review_usage=None,
               *, extraction_ms=None, extraction_usage=None):
        """Complete the held claim and clear its private payload atomically."""
        self._transaction(conn)
        if type(state) is not str or state not in _TERMINAL:
            raise ValueError('automatic memory terminal state is invalid')
        usage = self._metrics('review', review_ms, review_usage)
        extraction = self._metrics(
            'extraction', extraction_ms, extraction_usage,
        )
        now = self._now()
        self._expire(conn, now)
        cursor = conn.execute('''UPDATE automatic_memory_jobs
            SET state=?, claim=NULL, payload_json=NULL,
                finished_at=?, review_ms=?, review_input_tokens=?,
                review_output_tokens=?, review_total_tokens=?,
                extraction_ms=?, extraction_input_tokens=?,
                extraction_output_tokens=?, extraction_total_tokens=?
            WHERE user_id=? AND request_id=? AND state='running' AND claim=?
                AND expires_at>?''', (
                    state, now, review_ms,
                    usage['input_tokens'], usage['output_tokens'],
                    usage['total_tokens'], extraction_ms,
                    extraction['input_tokens'], extraction['output_tokens'],
                    extraction['total_tokens'], job['user_id'],
                    job['request_id'], job['claim'], now,
                ))
        return cursor.rowcount == 1

    def metadata(self, user_id, request_id):
        """Read content-free status without exposing identities or claims."""
        with self.conversations._lock:
            row = self.conversations._connection.execute('''
                SELECT state,
                    created_at, expires_at, started_at,
                    finished_at, review_ms, reason,
                    review_input_tokens, review_output_tokens,
                    review_total_tokens, extraction_ms,
                    extraction_input_tokens, extraction_output_tokens,
                    extraction_total_tokens
                FROM automatic_memory_jobs
                WHERE user_id=? AND request_id=?''', (
                    user_id, request_id,
                )).fetchone()
            return dict(row) if row is not None else {}
