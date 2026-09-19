"""Private local journal/outbox for incident metadata; never stores RGB or audio.

Restart replays uploads using stable event IDs, not past questions or actuator
commands. Unresolved records remain queryable for an explicit recovery policy.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from malbut_agent_server.domain.fall_monitoring import identifier


class SqliteFallJournal:
    def __init__(self, path: Path, *, device_id: str, wall_clock=time.time):
        identifier(device_id)
        path = Path(path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('absolute non-symlink journal path required')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError('journal directory must be private (0700)')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError('journal file must be private (0600)')
        finally:
            os.close(fd)
        self.device_id, self._clock = device_id, wall_clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=DELETE')
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS identity(device_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS incident_events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                incident_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                closure_evidence TEXT);
            CREATE INDEX IF NOT EXISTS pending_fall_uploads
                ON incident_events(status,next_attempt_at,sequence);
        ''')
        try:
            self._db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in self._db.execute(
                'PRAGMA table_info(incident_events)')}
            if 'closure_evidence' not in columns:
                self._db.execute('ALTER TABLE incident_events ADD COLUMN closure_evidence TEXT')
            identities = self._db.execute('SELECT device_id FROM identity').fetchall()
            if identities and (len(identities) != 1 or identities[0]['device_id'] != device_id):
                raise ValueError('journal belongs to a different device')
            if not identities:
                self._db.execute('INSERT INTO identity VALUES(?)', (device_id,))
            self._db.commit()
        except Exception:
            self._db.rollback()
            self._db.close()
            raise

    def append(self, *, device_id, boot_id, event, incident):
        if device_id != self.device_id or event.incident_id != incident.incident_id:
            raise ValueError('journal identity mismatch')
        occurred = datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(
            timespec='milliseconds').replace('+00:00', 'Z')
        payload = dict(
            schemaVersion=1, eventId=event.event_id, incidentId=incident.incident_id,
            bootId=boot_id, sequence=0, evidenceRevision=incident.revision,
            occurredAt=occurred, eventKind=event.kind, state=incident.state.value,
            fallSeen=incident.fall_seen,
            assessment=incident.video.assessment.value if incident.video else None,
            answer=incident.answer.value if incident.answer else None,
            reason=event.reason,
            notificationLevel=event.notification_level.value if event.notification_level else None)
        # Private proof retained across restart. Keep the existing web wire
        # contract unchanged; never store pixels, transcript or model text.
        proof = None
        if event.kind == 'incident_resolved' and event.reason == 'normal_verified':
            observation = incident.subject_observation
            proof = json.dumps(dict(
                schemaVersion=1, candidateSources=incident.candidate_sources,
                answerQuestionPlayed=incident.answer_question_played,
                normalChecks=[dict(requestId=c.request_id, evidenceRevision=c.evidence_revision,
                                   windowEnd=c.window_end, targetToken=c.target_token)
                              for c in incident.normal_checks],
                subjectObservation=(dict(
                    subjectKey=observation.subject_key, requestId=observation.request_id,
                    evidenceRevision=observation.evidence_revision,
                    observedAt=observation.observed_at, state=observation.state.value,
                    associationVerified=observation.association_verified) if observation else None)
            ), separators=(',', ':'), allow_nan=False)
        with self._lock, self._db:
            cursor = self._db.execute(
                'INSERT INTO incident_events(event_id,incident_id,payload,closure_evidence) '
                'VALUES(?,?,?,?)', (event.event_id, incident.incident_id, '{}', proof))
            payload['sequence'] = cursor.lastrowid
            self._db.execute('UPDATE incident_events SET payload=? WHERE sequence=?',
                             (json.dumps(payload, separators=(',', ':')), cursor.lastrowid))

    def pending(self):
        with self._lock:
            row = self._db.execute('''
                SELECT event_id,payload,attempt_count FROM incident_events
                WHERE status='pending' AND next_attempt_at<=?
                ORDER BY CASE json_extract(payload,'$.notificationLevel')
                  WHEN 'urgent' THEN 0 WHEN 'check' THEN 1 WHEN 'info' THEN 2 ELSE 3 END,
                  sequence LIMIT 1''', (self._clock(),)).fetchone()
            return dict(row) if row else None

    def acknowledge(self, event_id):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE incident_events SET status='stored',last_error=NULL WHERE event_id=?",
                (event_id,))

    def failed(self, event_id, *, code, blocked=False):
        # Codes are bounded caller-generated tokens, never HTTP bodies/secrets.
        if code not in {'upload_failed', 'invalid_ack', 'http_400', 'http_401', 'http_403',
                        'http_409', 'http_413', 'http_429', 'http_503'}:
            code = 'upload_failed'
        with self._lock, self._db:
            self._db.execute(
                '''UPDATE incident_events SET status=?,last_error=?,
                attempt_count=attempt_count+1,
                next_attempt_at=?+MIN(300,5*(1 << MIN(attempt_count,6))) WHERE event_id=?''',
                ('blocked' if blocked else 'pending', code, self._clock(), event_id))

    def unresolved(self):
        """Metadata for recovery review, not automatic resumption across boots."""
        with self._lock:
            rows = self._db.execute('''SELECT payload FROM incident_events WHERE sequence IN
                (SELECT MAX(sequence) FROM incident_events GROUP BY incident_id)''')
            return [p for row in rows if (p := json.loads(row['payload']))['state'] != 'resolved']

    def upload_status(self):
        with self._lock:
            return [dict(row) for row in self._db.execute(
                'SELECT event_id,status,last_error FROM incident_events ORDER BY sequence')]

    def retry_auth_failed(self):
        """Explicit operator action after credentials are repaired; conflicts stay blocked."""
        with self._lock, self._db:
            self._db.execute("""UPDATE incident_events SET status='pending',next_attempt_at=0
                WHERE status='blocked' AND last_error IN ('http_401','http_403')""")

    def close(self):
        with self._lock:
            self._db.close()
