"""Story memory consent policy, fencing, traces, and reply metadata."""

import json
import logging
import sqlite3
from typing import Any

from malbut_agent_server.memory_trace import record_trace
from malbut_agent_server.story_memory import StoryMemoryError, _id, _json
from malbut_agent_server.story_store.schema import (
    StoryDatabase,
    _digest,
    _identity,
    raw_refs,
)


class StoryPolicyRepository:
    """Consent state, cross-worker fencing, traces, and reply receipts."""

    def __init__(self, db: StoryDatabase, memory_store: Any) -> None:
        self._db = db
        self.memory = memory_store

    def trace(self, request: Any, event: str, data: dict[str, Any]) -> None:
        """Diagnostic writes have no effect on consent or memory revisions."""
        try:
            with self._db.transaction(True) as conn:
                record_trace(
                    conn, event, request.user_id,
                    request_id=request.request_id,
                    conversation_id=request.conversation_id,
                    turn_id=request.turn_id,
                    data=data,
                )
        except Exception:
            logging.getLogger('malbut_agent_server.story_runtime_store').warning(
                'Memory trace transaction unavailable'
            )

    @staticmethod
    def job_trace(
        conn: sqlite3.Connection,
        event: str,
        user: str,
        request_id: str,
        data: dict[str, Any],
    ) -> None:
        turn = conn.execute(
            'SELECT conversation_id,turn_id FROM conversation_turns '
            'WHERE user_id=? AND request_id=?',
            (user, request_id),
        ).fetchone()
        record_trace(
            conn, event, user, request_id=request_id,
            conversation_id=turn['conversation_id'] if turn else '',
            turn_id=turn['turn_id'] if turn else '',
            data=data,
        )

    def reply_record(self, user: str, request_id: str) -> dict[str, Any] | None:
        """Read a completed owner's reply and legacy story provenance together."""
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        with self._db.transaction() as conn:
            row = conn.execute(
                'SELECT status,response_json FROM conversation_turns '
                'WHERE user_id=? AND request_id=?',
                (user, request_id),
            ).fetchone()
            if row is None or row['status'] != 'completed' or row['response_json'] is None:
                return None
            receipt = conn.execute(
                'SELECT stories_json FROM story_runtime_replies '
                'WHERE user_id=? AND request_id=?',
                (user, request_id),
            ).fetchone()
            return {
                'response': json.loads(row['response_json']),
                'has_story_dependencies': bool(receipt and json.loads(receipt[0])),
            }

    def reply_dependency_revision(self, user: str, request_id: str) -> int | None:
        """Return identifier-only short-context lineage while a turn is pending."""
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        with self._db.transaction() as conn:
            row = conn.execute(
                'SELECT revision,stories_json FROM story_runtime_replies '
                'WHERE user_id=? AND request_id=?',
                (user, request_id),
            ).fetchone()
            return int(row['revision']) if row and json.loads(row['stories_json']) else None

    @staticmethod
    def read_policy(conn: sqlite3.Connection, user: str) -> dict[str, Any]:
        row = conn.execute(
            'SELECT * FROM story_runtime_policy WHERE user_id=?',
            (user,),
        ).fetchone()
        return dict(row) if row else {
            'user_id': user, 'enabled': 0, 'external_consent': 0,
            'revision': 0, 'data_revision': 0, 'windows_json': '[]',
        }

    @staticmethod
    def public_policy(policy: dict[str, Any]) -> dict[str, Any]:
        return {
            'enabled': bool(policy['enabled']),
            'revision': policy['revision'],
            'external_consent': bool(policy['external_consent']),
            'data_revision': policy['data_revision'],
        }

    def policy(self, user: str) -> dict[str, Any]:
        user = _id(user, 'user_id')
        with self._db.transaction() as conn:
            return self.public_policy(self.read_policy(conn, user))

    @staticmethod
    def has_table(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone() is not None

    def fence(self, conn: sqlite3.Connection, user: str) -> None:
        """Fence cached answers, compaction and both memory workers atomically."""
        self.memory.invalidate_answers(user, connection=conn)
        conn.execute(
            "UPDATE story_runtime_jobs SET state='cancelled', claim=NULL, "
            "lease_until=NULL, read_sources_json='[]' "
            "WHERE user_id=? AND state IN ('queued','running','failed')",
            (user,),
        )
        if self.has_table(conn, 'automatic_memory_jobs'):
            conn.execute(
                "UPDATE automatic_memory_jobs SET state='discarded', claim=NULL, "
                "payload_json=NULL, finished_at=?, reason='memory_control' "
                "WHERE user_id=? AND state IN ('queued','running')",
                (self._db.now(), user),
            )
        if self.has_table(conn, 'memory_questions'):
            conn.execute('DELETE FROM memory_questions WHERE user_id=?', (user,))

    def mark_reply_stale(
        self, conn: sqlite3.Connection, user: str, request_id: str,
    ) -> None:
        if not self.has_table(conn, 'memory_turn_state'):
            return
        row = conn.execute(
            'SELECT dependencies_json FROM memory_turn_state '
            'WHERE user_id=? AND request_id=?',
            (user, request_id),
        ).fetchone()
        if row:
            marker = 'story-reply-' + _digest([user, request_id])
            deps = set(json.loads(row[0])) | {marker}
            conn.execute(
                'UPDATE memory_turn_state SET dependencies_json=? '
                'WHERE user_id=? AND request_id=?',
                (_json(sorted(deps)), user, request_id),
            )
            conn.execute(
                'INSERT OR IGNORE INTO memory_tombstones '
                '(user_id,memory_id,source_key,invalidated_at) VALUES (?,?,?,?)',
                (user, marker, '{}', self._db.now()),
            )

    def set_enabled(
        self,
        user: str,
        enabled: bool,
        external_consent: bool = False,
        include_history: bool = False,
        history_scope: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        from malbut_agent_server.story_memory import StoryConflictError

        user = _id(user, 'user_id')
        if expected_revision is not None and (
                type(expected_revision) is not int or expected_revision < 0):
            raise StoryMemoryError('invalid expected policy revision')
        if any(type(v) is not bool for v in (enabled, external_consent, include_history)):
            raise StoryMemoryError('consent values must be boolean')
        if include_history and not enabled:
            raise StoryMemoryError('history consent requires enabled memory')
        if history_scope is not None and (
                not isinstance(history_scope, dict)
                or history_scope.get('user_id') != user
                or not isinstance(history_scope.get('turns'), list)):
            raise StoryMemoryError('invalid history consent snapshot')
        with self._db.transaction(True) as conn:
            old = self.read_policy(conn, user)
            if expected_revision is not None and old['revision'] != expected_revision:
                raise StoryConflictError('story policy changed')
            changed = (
                bool(old['enabled']) != enabled
                or bool(old['external_consent']) != external_consent
                or include_history
            )
            if not changed:
                return self.public_policy(old)
            now = self._db.now()
            ceiling = conn.execute(
                'SELECT COALESCE(MAX(rowid),0) FROM conversation_turns'
            ).fetchone()[0]
            stored = conn.execute(
                'SELECT * FROM conversation_turns WHERE user_id=?', (user,),
            ).fetchall()
            windows = json.loads(old['windows_json'])
            if bool(old['enabled']) and not enabled and windows and windows[-1]['end'] is None:
                windows[-1].update(
                    end=ceiling,
                    closed_at=now,
                    completed=[_identity(row) for row in stored if row['status'] == 'completed'],
                )
            if enabled and not old['enabled']:
                windows.append({
                    'start': ceiling,
                    'end': None,
                    'opened_at': now,
                    'closed_at': None,
                    'existing': [_identity(row) for row in stored],
                })
            revision = old['revision'] + 1
            conn.execute(
                '''INSERT INTO story_runtime_policy
                (user_id,enabled,external_consent,revision,data_revision,windows_json)
                VALUES (?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled,external_consent=excluded.external_consent,
                revision=excluded.revision,windows_json=excluded.windows_json''',
                (user, int(enabled), int(external_consent), revision,
                 old['data_revision'], _json(windows)),
            )
            if include_history:
                preview = (
                    {item['request_id']: item for item in history_scope['turns']}
                    if history_scope is not None else None
                )
                rows = conn.execute(
                    "SELECT * FROM conversation_turns WHERE user_id=? "
                    "AND status='completed'",
                    (user,),
                ).fetchall()
                for row in rows:
                    if preview is not None:
                        item = preview.get(row['request_id'])
                        if (item is None or item.get('identity') != _identity(row)
                                or item.get('source_digest') != _digest([
                                    ref.key for ref in raw_refs(conn, user, row)
                                ])):
                            continue
                    conn.execute(
                        '''INSERT INTO story_runtime_scope VALUES (?,?,?,?)
                        ON CONFLICT(user_id,request_id) DO UPDATE SET
                        identity_json=excluded.identity_json,
                        grant_revision=excluded.grant_revision''',
                        (user, row['request_id'], _identity(row), revision),
                    )
            self.fence(conn, user)
            if not enabled or not external_consent:
                for row in conn.execute(
                    'SELECT request_id FROM story_runtime_replies WHERE user_id=?',
                    (user,),
                ).fetchall():
                    self.mark_reply_stale(conn, user, row[0])
                conn.execute('DELETE FROM conversation_summaries WHERE user_id=?', (user,))
            return self.public_policy(self.read_policy(conn, user))

    def validate(
        self, user: str, revision: Any, data_revision: int | None = None,
    ) -> bool:
        user = _id(user, 'user_id')
        with self._db.transaction() as conn:
            p = self.read_policy(conn, user)
            return bool(
                p['enabled']
                and type(revision) is int
                and p['revision'] == revision
                and (data_revision is None or p['data_revision'] == data_revision)
            )

    def db_version(self) -> tuple[int, int]:
        """Return a connection-local + cross-connection change token."""
        with self._db.transaction() as conn:
            return (conn.total_changes, int(conn.execute('PRAGMA data_version').fetchone()[0]))
