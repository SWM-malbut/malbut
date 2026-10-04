"""Durable runtime story memory on the conversation connection.

Consent windows grant completed source turns, independently of personal-fact
consent. Jobs keep references, not raw copies. All provider work happens outside
this class's transactions. Quotes establish provenance, not semantic truth.
Deletion is application-level redaction; it does not promise media/backup erasure.
"""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import math
import re
import secrets
import time
import unicodedata

from .story_memory import StoryEntry, StoryMemoryError, _entries, _id, _json, _text
from .story_memory_sources import SourceRef, make_source_ref, read_source


LEASE_SECONDS = 300
MAX_ATTEMPTS = 3
MAX_SOURCE_CHARS = 48000
MAX_UPDATES = 8
MAX_PENDING = 256


def _refs_json(refs):
    return _json([ref.to_dict() for ref in refs])


def _refs(value):
    return [SourceRef.from_dict(item) for item in json.loads(value)]


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _identity(row):
    return _json([row['conversation_id'], row['session_instance_id'],
                  row['generation'], row['turn_id']])


def _message_key(ref):
    return _json([ref.conversation_id, ref.session_instance_id,
                  ref.generation, ref.turn_id, ref.role])


def _source_object(ref, source, identifier):
    return {'id': identifier, 'ref': ref.to_dict(), 'text': source.text,
            'role': ref.role, 'created_at': source.created_at,
            'completed_at': source.completed_at}


class StoryRuntimeStore:
    """A shared-transaction backend. user_id must come from a trusted caller."""

    def __init__(self, conversation_store, memory_store, clock=time.time):
        self.conversations = conversation_store
        self.memory = memory_store
        self._clock = clock
        if memory_store._connection is not conversation_store._connection:
            memory_store.bind_connection(conversation_store._connection,
                                         conversation_store._lock)
        self._initialize()

    @contextmanager
    def _transaction(self, write=False):
        with self.conversations._lock:
            conn = self.conversations._connection
            if conn.in_transaction:
                if write:
                    raise StoryMemoryError('story write requires its own transaction')
                # Confirmation commits revalidate a reply inside the owning
                # conversation transaction. Join its snapshot for read guards;
                # never commit or roll back the caller's transaction here.
                yield conn
                return
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise StoryMemoryError('invalid clock')
        return float(value)

    def _initialize(self):
        definitions = (
            '''CREATE TABLE IF NOT EXISTS story_runtime_policy (
                user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                external_consent INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                data_revision INTEGER NOT NULL DEFAULT 0, windows_json TEXT NOT NULL DEFAULT '[]')''',
            '''CREATE TABLE IF NOT EXISTS story_runtime_scope (
                user_id TEXT NOT NULL, request_id TEXT NOT NULL, identity_json TEXT NOT NULL,
                grant_revision INTEGER NOT NULL, PRIMARY KEY(user_id, request_id))''',
            '''CREATE TABLE IF NOT EXISTS story_runtime_jobs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                job_id TEXT NOT NULL, request_id TEXT NOT NULL, source_digest TEXT NOT NULL,
                sources_json TEXT NOT NULL, read_sources_json TEXT NOT NULL DEFAULT '[]',
                batch_json TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL, lease_until REAL, claim TEXT,
                policy_revision INTEGER NOT NULL, expected_version INTEGER,
                result_digest TEXT, error_code TEXT, created_at REAL NOT NULL,
                UNIQUE(user_id, job_id), UNIQUE(user_id, request_id, source_digest))''',
            '''CREATE TABLE IF NOT EXISTS story_runtime_stories (
                user_id TEXT NOT NULL, story_id TEXT NOT NULL, title TEXT NOT NULL,
                aliases_json TEXT NOT NULL, current_json TEXT NOT NULL,
                episodes_json TEXT NOT NULL, refs_json TEXT NOT NULL,
                version INTEGER NOT NULL, updated_at REAL NOT NULL, source_time REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id, story_id))''',
            '''CREATE TABLE IF NOT EXISTS story_runtime_replies (
                user_id TEXT NOT NULL, request_id TEXT NOT NULL,
                stories_json TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY(user_id, request_id))''',
            '''CREATE TABLE IF NOT EXISTS story_runtime_exclusions (
                user_id TEXT NOT NULL, message_key TEXT NOT NULL, sha256 TEXT NOT NULL,
                PRIMARY KEY(user_id, message_key, sha256))''',
            '''CREATE INDEX IF NOT EXISTS story_runtime_jobs_claim_idx
                ON story_runtime_jobs(user_id,state,available_at,sequence)''',
        )
        with self._transaction(True) as conn:
            for sql in definitions:
                conn.execute(sql)
            if 'batch_json' not in {row[1] for row in conn.execute('PRAGMA table_info(story_runtime_jobs)')}:
                conn.execute("ALTER TABLE story_runtime_jobs ADD COLUMN batch_json TEXT NOT NULL DEFAULT '[]'")
            if 'processed' not in {row[1] for row in conn.execute('PRAGMA table_info(story_runtime_jobs)')}:
                conn.execute('ALTER TABLE story_runtime_jobs ADD COLUMN processed INTEGER NOT NULL DEFAULT 0')
                conn.execute("UPDATE story_runtime_jobs SET processed=1 WHERE state='done'")

    def close(self):
        """The runtime owns the shared connection and closes it separately."""

    def reply_record(self, user, request_id):
        """Read a completed owner's reply and legacy story provenance together."""
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        with self._transaction() as conn:
            row = conn.execute('SELECT status,response_json FROM conversation_turns '
                               'WHERE user_id=? AND request_id=?', (user, request_id)).fetchone()
            if row is None or row['status'] != 'completed' or row['response_json'] is None:
                return None
            receipt = conn.execute('SELECT stories_json FROM story_runtime_replies '
                                   'WHERE user_id=? AND request_id=?', (user, request_id)).fetchone()
            return {'response': json.loads(row['response_json']),
                    'has_story_dependencies': bool(receipt and json.loads(receipt[0]))}

    def reply_dependency_revision(self, user, request_id):
        """Return identifier-only short-context lineage while a turn is pending."""
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        with self._transaction() as conn:
            row = conn.execute('SELECT revision,stories_json FROM story_runtime_replies '
                               'WHERE user_id=? AND request_id=?', (user, request_id)).fetchone()
            return row['revision'] if row and json.loads(row['stories_json']) else None

    @staticmethod
    def _policy(conn, user):
        row = conn.execute('SELECT * FROM story_runtime_policy WHERE user_id=?',
                           (user,)).fetchone()
        return dict(row) if row else {
            'user_id': user, 'enabled': 0, 'external_consent': 0,
            'revision': 0, 'data_revision': 0, 'windows_json': '[]',
        }

    @staticmethod
    def _public_policy(policy):
        return {'enabled': bool(policy['enabled']), 'revision': policy['revision'],
                'external_consent': bool(policy['external_consent']),
                'data_revision': policy['data_revision']}

    def policy(self, user):
        user = _id(user, 'user_id')
        with self._transaction() as conn:
            return self._public_policy(self._policy(conn, user))

    @staticmethod
    def _has_table(conn, name):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None

    def _fence(self, conn, user):
        """Fence cached answers, compaction and both memory workers atomically."""
        self.memory.invalidate_answers(user, connection=conn)
        conn.execute("UPDATE story_runtime_jobs SET state='cancelled', claim=NULL, "
                     "lease_until=NULL, read_sources_json='[]' "
                     "WHERE user_id=? AND state IN ('queued','running','failed')", (user,))
        if self._has_table(conn, 'automatic_memory_jobs'):
            conn.execute("UPDATE automatic_memory_jobs SET state='discarded', claim=NULL, "
                         "payload_json=NULL, finished_at=?, reason='memory_control' "
                         "WHERE user_id=? AND state IN ('queued','running')", (self._now(), user))
        if self._has_table(conn, 'memory_questions'):
            conn.execute('DELETE FROM memory_questions WHERE user_id=?', (user,))

    def _mark_reply_stale(self, conn, user, request_id):
        if not self._has_table(conn, 'memory_turn_state'):
            return
        row = conn.execute('SELECT dependencies_json FROM memory_turn_state '
                           'WHERE user_id=? AND request_id=?', (user, request_id)).fetchone()
        if row:
            marker = 'story-reply-' + _digest([user, request_id])
            deps = set(json.loads(row[0])) | {marker}
            conn.execute('UPDATE memory_turn_state SET dependencies_json=? '
                         'WHERE user_id=? AND request_id=?', (_json(sorted(deps)), user, request_id))
            conn.execute('INSERT OR IGNORE INTO memory_tombstones '
                         '(user_id,memory_id,source_key,invalidated_at) VALUES (?,?,?,?)',
                         (user, marker, '{}', self._now()))

    def set_enabled(self, user, enabled, external_consent=False, include_history=False,
                    history_scope=None, expected_revision=None):
        from .story_memory import StoryConflictError

        user = _id(user, 'user_id')
        if expected_revision is not None and (
                type(expected_revision) is not int or expected_revision < 0):
            raise StoryMemoryError('invalid expected policy revision')
        if any(type(v) is not bool for v in (enabled, external_consent, include_history)):
            raise StoryMemoryError('consent values must be boolean')
        if include_history and not enabled:
            raise StoryMemoryError('history consent requires enabled memory')
        if history_scope is not None and (not isinstance(history_scope, dict)
                                          or history_scope.get('user_id') != user
                                          or not isinstance(history_scope.get('turns'), list)):
            raise StoryMemoryError('invalid history consent snapshot')
        with self._transaction(True) as conn:
            old = self._policy(conn, user)
            if expected_revision is not None and old['revision'] != expected_revision:
                raise StoryConflictError('story policy changed')
            changed = (bool(old['enabled']) != enabled
                       or bool(old['external_consent']) != external_consent or include_history)
            if not changed:
                return self._public_policy(old)
            now = self._now()
            ceiling = conn.execute('SELECT COALESCE(MAX(rowid),0) FROM conversation_turns').fetchone()[0]
            stored = conn.execute('SELECT * FROM conversation_turns WHERE user_id=?', (user,)).fetchall()
            windows = json.loads(old['windows_json'])
            if bool(old['enabled']) and not enabled and windows and windows[-1]['end'] is None:
                windows[-1].update(end=ceiling, closed_at=now,
                                   completed=[_identity(row) for row in stored if row['status'] == 'completed'])
            if enabled and not old['enabled']:
                windows.append({'start': ceiling, 'end': None, 'opened_at': now,
                                'closed_at': None, 'existing': [_identity(row) for row in stored]})
            revision = old['revision'] + 1
            conn.execute('''INSERT INTO story_runtime_policy
                (user_id,enabled,external_consent,revision,data_revision,windows_json)
                VALUES (?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled,external_consent=excluded.external_consent,
                revision=excluded.revision,windows_json=excluded.windows_json''',
                         (user, int(enabled), int(external_consent), revision,
                          old['data_revision'], _json(windows)))
            if include_history:
                preview = ({item['request_id']: item for item in history_scope['turns']}
                           if history_scope is not None else None)
                rows = conn.execute("SELECT * FROM conversation_turns WHERE user_id=? "
                                    "AND status='completed'", (user,)).fetchall()
                for row in rows:
                    if preview is not None:
                        item = preview.get(row['request_id'])
                        if (item is None or item.get('identity') != _identity(row)
                                or item.get('source_digest') != _digest([ref.key for ref in self._raw_refs(conn, user, row)])):
                            continue
                    conn.execute('''INSERT INTO story_runtime_scope VALUES (?,?,?,?)
                        ON CONFLICT(user_id,request_id) DO UPDATE SET
                        identity_json=excluded.identity_json,grant_revision=excluded.grant_revision''',
                                 (user, row['request_id'], _identity(row), revision))
            self._fence(conn, user)
            if not enabled or not external_consent:
                for row in conn.execute('SELECT request_id FROM story_runtime_replies WHERE user_id=?',
                                        (user,)).fetchall():
                    self._mark_reply_stale(conn, user, row[0])
                conn.execute('DELETE FROM conversation_summaries WHERE user_id=?', (user,))
            return self._public_policy(self._policy(conn, user))

    def validate(self, user, revision, data_revision=None):
        user = _id(user, 'user_id')
        with self._transaction() as conn:
            p = self._policy(conn, user)
            return bool(p['enabled'] and type(revision) is int and p['revision'] == revision
                        and (data_revision is None or p['data_revision'] == data_revision))

    def _grant(self, conn, user, row, policy):
        existing = conn.execute('SELECT * FROM story_runtime_scope WHERE user_id=? AND request_id=?',
                                (user, row['request_id'])).fetchone()
        if existing:
            return existing['identity_json'] == _identity(row)
        # rowid may be reused after a session deletion. Capture identities at
        # window boundaries rather than treating rowid as a durable sequence.
        identity = _identity(row)
        eligible = any(identity not in window.get('existing', [])
                       and row['created_at'] >= window['opened_at']
                       and (window['closed_at'] is None
                            or identity in window.get('completed', []))
                       for window in json.loads(policy['windows_json']))
        if eligible:
            conn.execute('INSERT INTO story_runtime_scope VALUES (?,?,?,?)',
                         (user, row['request_id'], _identity(row), policy['revision']))
        return eligible

    def _raw_refs(self, conn, user, row):
        result = []
        for role, field in (('user', 'user_content'), ('assistant', 'assistant_content')):
            if row[field]:
                ref = make_source_ref(conn, user, row['conversation_id'], row['session_instance_id'],
                                      row['generation'], row['turn_id'], role)
                if conn.execute('SELECT 1 FROM story_runtime_exclusions WHERE user_id=? '
                                'AND message_key=? AND sha256=?',
                                (user, _message_key(ref), ref.sha256)).fetchone():
                    continue
                result.append(ref)
        return result

    def _enqueue(self, conn, user, request_id):
        policy = self._policy(conn, user)
        if not policy['enabled'] or not policy['external_consent']:
            return False
        row = conn.execute("SELECT rowid,* FROM conversation_turns WHERE user_id=? "
                           "AND request_id=? AND status='completed'", (user, request_id)).fetchone()
        if row is None or not self._grant(conn, user, row, policy):
            return False
        refs = self._raw_refs(conn, user, row)
        if not refs:
            return False
        if any(self._resolve(conn, user, ref) is None for ref in refs):
            # A fact tombstone revokes retained raw text without erasing it.
            # Do not recreate a cancelled extraction on each recovery pass.
            # Release the rest of an in-flight batch so valid members can be
            # claimed again without waiting for the old lease to expire.
            claims = [item[0] for item in conn.execute(
                "SELECT claim FROM story_runtime_jobs WHERE user_id=? "
                "AND request_id=? AND state='running' AND claim IS NOT NULL",
                (user, request_id))]
            for claim in claims:
                conn.execute("UPDATE story_runtime_jobs SET state='cancelled',claim=NULL, "
                             "lease_until=NULL,read_sources_json='[]',error_code='source_revoked' "
                             "WHERE user_id=? AND state='running' AND claim=?", (user, claim))
            conn.execute("UPDATE story_runtime_jobs SET state='cancelled',claim=NULL, "
                         "lease_until=NULL,read_sources_json='[]',error_code='source_revoked' "
                         "WHERE user_id=? AND request_id=? AND state IN ('queued','running','failed')",
                         (user, request_id))
            return False
        digest = _digest([ref.key for ref in refs])
        existing = conn.execute('SELECT * FROM story_runtime_jobs WHERE user_id=? '
                                'AND request_id=? AND source_digest=?', (user, request_id, digest)).fetchone()
        if existing:
            scope = conn.execute('SELECT grant_revision FROM story_runtime_scope WHERE user_id=? '
                                 'AND request_id=?', (user, request_id)).fetchone()
            if existing['state'] == 'cancelled' and scope[0] == policy['revision']:
                conn.execute("UPDATE story_runtime_jobs SET state='queued',policy_revision=?, "
                             "attempts=0,available_at=? WHERE sequence=?",
                             (policy['revision'], self._now(), existing['sequence']))
                return True
            return False
        count = conn.execute("SELECT COUNT(*) FROM story_runtime_jobs WHERE user_id=? "
                             "AND state IN ('queued','running')", (user,)).fetchone()[0]
        if count >= MAX_PENDING:
            return False
        conn.execute('''INSERT INTO story_runtime_jobs
            (user_id,job_id,request_id,source_digest,sources_json,state,available_at,
             policy_revision,created_at) VALUES (?,?,?,?,?,'queued',?,?,?)''',
                     (user, _digest([user, request_id, digest]), request_id, digest,
                      _refs_json(refs), self._now(), policy['revision'], self._now()))
        return True

    def enqueue_completed(self, user, request_id):
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        with self._transaction(True) as conn:
            return self._enqueue(conn, user, request_id)

    def recover(self, user):
        user = _id(user, 'user_id')
        with self._transaction(True) as conn:
            p = self._policy(conn, user)
            if not p['enabled'] or not p['external_consent']:
                return 0
            conn.execute("UPDATE story_runtime_jobs SET state=CASE WHEN attempts>=? THEN 'failed' "
                         "ELSE 'queued' END,claim=NULL,lease_until=NULL,available_at=? "
                         "WHERE user_id=? AND state='running' AND lease_until<=?",
                         (MAX_ATTEMPTS, self._now(), user, self._now()))
            rows = conn.execute("SELECT request_id FROM conversation_turns WHERE user_id=? "
                                "AND status='completed' ORDER BY completed_at,rowid", (user,)).fetchall()
            return sum(self._enqueue(conn, user, row[0]) for row in rows)

    def pending_users(self):
        with self._transaction() as conn:
            return [row[0] for row in conn.execute('SELECT user_id FROM story_runtime_policy '
                                                  'WHERE enabled=1 AND external_consent=1')]

    def _resolve(self, conn, user, ref):
        source = read_source(conn, user, ref)
        if source is None:
            return None
        row = conn.execute('SELECT * FROM conversation_turns WHERE user_id=? AND conversation_id=? '
                           'AND session_instance_id=? AND generation=? AND turn_id=?',
                           (user, ref.conversation_id, ref.session_instance_id,
                            ref.generation, ref.turn_id)).fetchone()
        scope = conn.execute('SELECT identity_json FROM story_runtime_scope WHERE user_id=? '
                             'AND request_id=?', (user, row['request_id'])).fetchone() if row else None
        if scope is None or scope[0] != _identity(row):
            return None
        # Fact deletion/correction retains raw conversation text, but revokes
        # its source and any turns explicitly stamped as depending on it.
        # Story-only reply markers have no fact source: do not treat disabling
        # story recall as a deletion of the underlying retained story.
        source_key = {key: row[key] for key in (
            'conversation_id', 'session_instance_id', 'generation',
            'turn_id', 'request_id',
        )}
        dependencies = set()
        if self._has_table(conn, 'memory_turn_state'):
            stamp = conn.execute('SELECT dependencies_json FROM memory_turn_state '
                                 'WHERE user_id=? AND request_id=?',
                                 (user, row['request_id'])).fetchone()
            if stamp is not None:
                dependencies = set(json.loads(stamp[0]))
        for tombstone in conn.execute(
                'SELECT memory_id,source_key FROM memory_tombstones WHERE user_id=?',
                (user,)):
            revoked = json.loads(tombstone['source_key'])
            if revoked and (revoked == source_key
                            or tombstone['memory_id'] in dependencies):
                return None
        if conn.execute('SELECT 1 FROM story_runtime_exclusions WHERE user_id=? '
                        'AND message_key=? AND sha256=?',
                        (user, _message_key(ref), ref.sha256)).fetchone():
            return None
        return source

    def _story_valid(self, conn, user, row):
        return bool(row['active'] and all(self._resolve(conn, user, ref) is not None
                                          for ref in _refs(row['refs_json'])))

    @staticmethod
    def _story_dict(row):
        return {'story_id': row['story_id'], 'title': row['title'],
                'aliases': json.loads(row['aliases_json']), 'current': json.loads(row['current_json']),
                'episodes': json.loads(row['episodes_json']), 'version': row['version'],
                'updated_at': row['updated_at'], 'source_time': row['source_time'],
                'untrusted': True, 'execution_authorized': False}

    def _candidate_rows(self, conn, user, query, limit):
        normalized_query = unicodedata.normalize('NFKC', query).casefold()
        terms = set(re.findall(r'\w+', normalized_query))
        ranked = []
        for row in conn.execute('SELECT * FROM story_runtime_stories WHERE user_id=? AND active=1', (user,)):
            text = unicodedata.normalize('NFKC', ' '.join([
                row['title'], *json.loads(row['aliases_json']),
                *(entry['text'] for entry in json.loads(row['current_json']))])).casefold()
            score = sum(term in text for term in terms)
            title_terms = set(re.findall(r'\w+', unicodedata.normalize(
                'NFKC', ' '.join([row['title'], *json.loads(row['aliases_json'])])).casefold()))
            score += 2 * sum(len(term) >= 2 and term in normalized_query
                             and term not in {'이야기', '기억', '대화', '지난번'} for term in title_terms)
            if score and self._story_valid(conn, user, row):
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], -item[1]['updated_at'], item[1]['story_id']))
        return [row for _, row in ranked[:limit]]

    def claim(self, user, batch_size=1):
        user = _id(user, 'user_id')
        if type(batch_size) is not int or not 1 <= batch_size <= 3:
            raise StoryMemoryError('batch_size must be between 1 and 3')
        with self._transaction(True) as conn:
            p = self._policy(conn, user)
            if not p['enabled'] or not p['external_consent']:
                return None
            conn.execute("UPDATE story_runtime_jobs SET state=CASE WHEN attempts>=? THEN 'failed' "
                         "ELSE 'queued' END,claim=NULL,lease_until=NULL,available_at=? "
                         "WHERE user_id=? AND state='running' AND lease_until<=?",
                         (MAX_ATTEMPTS, self._now(), user, self._now()))
            if conn.execute("SELECT 1 FROM story_runtime_jobs WHERE user_id=? AND state='running' "
                            "AND lease_until>?", (user, self._now())).fetchone():
                return None
            queued = conn.execute("SELECT * FROM story_runtime_jobs WHERE user_id=? AND state='queued' "
                                  "AND available_at<=? ORDER BY sequence LIMIT ?",
                                  (user, self._now(), batch_size)).fetchall()
            if not queued:
                return None
            row = queued[0]
            if row['policy_revision'] != p['revision']:
                conn.execute("UPDATE story_runtime_jobs SET state='cancelled' WHERE sequence=?", (row['sequence'],))
                return None
            sources, batch = [], []
            for candidate in queued:
                if candidate['policy_revision'] != p['revision']:
                    break
                additions = []
                for ref in _refs(candidate['sources_json']):
                    source = self._resolve(conn, user, ref)
                    if source is None:
                        conn.execute("UPDATE story_runtime_jobs SET state='cancelled',error_code='source_changed' "
                                     "WHERE sequence=?", (candidate['sequence'],))
                        additions = None
                        break
                    additions.append((ref, source))
                if additions is None:
                    if not batch:
                        return None
                    break
                if batch and sum(len(item['text']) for item in sources) + sum(len(src.text) for _, src in additions) > 32000:
                    break
                for ref, source in additions:
                    sources.append(_source_object(ref, source, 's' + str(len(sources) + 1)))
                batch.append(candidate)
            if sum(len(item['text']) for item in sources) > MAX_SOURCE_CHARS:
                conn.execute("UPDATE story_runtime_jobs SET state='failed',error_code='source_too_large' "
                             "WHERE sequence=?", (row['sequence'],))
                return None
            previous = self._candidate_rows(conn, user, ' '.join(item['text'] for item in sources), 5)
            stories = []
            source_ids = {SourceRef.from_dict(item['ref']).key: item['id'] for item in sources}
            total = sum(len(item['text']) for item in sources)
            for prior in previous:
                story = self._story_dict(prior)
                keys = {key for entry in story['current'] for key in entry['source_keys']}
                additions = []
                for ref in _refs(prior['refs_json']):
                    if ref.key in keys and ref.key not in source_ids:
                        source = self._resolve(conn, user, ref)
                        additions.append((ref, source))
                if total + sum(len(src.text) for _, src in additions) > MAX_SOURCE_CHARS:
                    continue
                for ref, source in additions:
                    identifier = 'p' + str(len(source_ids) + 1)
                    sources.append(_source_object(ref, source, identifier))
                    source_ids[ref.key] = identifier
                    total += len(source.text)
                for entry in story['current']:
                    entry['evidence'] = [{'source_id': source_ids[key],
                                          'quote': next(s['text'] for s in sources if s['id'] == source_ids[key])}
                                         for key in entry.pop('source_keys')]
                story.pop('episodes')
                stories.append(story)
            claim = secrets.token_hex(20)
            batch_ids = [member['job_id'] for member in batch]
            for member in batch:
                conn.execute("UPDATE story_runtime_jobs SET state='running',attempts=attempts+1,claim=?, "
                             "lease_until=?,expected_version=?,read_sources_json=?,batch_json=? WHERE sequence=?",
                             (claim, self._now() + LEASE_SECONDS, p['data_revision'],
                              _json([{'id': item['id'], 'ref': item['ref']} for item in sources]),
                              _json(batch_ids), member['sequence']))
            return {'user_id': user, 'job_id': row['job_id'], 'claim': claim,
                    'request_id': row['request_id'], 'policy_revision': p['revision'],
                    'expected_version': p['data_revision'], 'sources': sources, 'stories': stories,
                    'batch_job_ids': batch_ids}

    def _job(self, conn, job, allow_done=False):
        row = conn.execute('SELECT * FROM story_runtime_jobs WHERE user_id=? AND job_id=?',
                           (job.get('user_id'), job.get('job_id'))).fetchone()
        p = self._policy(conn, job.get('user_id'))
        if (row is None or not p['enabled'] or not p['external_consent']
                or row['policy_revision'] != p['revision']
                or job.get('policy_revision') != row['policy_revision']):
            return None
        if allow_done and row['state'] == 'done':
            for identifier in json.loads(row['batch_json']):
                member = conn.execute('SELECT state,result_digest FROM story_runtime_jobs WHERE user_id=? AND job_id=?',
                                      (row['user_id'], identifier)).fetchone()
                if member is None or member['state'] != 'done' or member['result_digest'] != row['result_digest']:
                    return None
            return row
        if (row['state'] != 'running' or row['claim'] != job.get('claim')
                or row['lease_until'] <= self._now() or p['data_revision'] != row['expected_version']
                or job.get('expected_version') != row['expected_version']):
            return None
        for identifier in json.loads(row['batch_json']):
            member = conn.execute('SELECT * FROM story_runtime_jobs WHERE user_id=? AND job_id=?',
                                  (row['user_id'], identifier)).fetchone()
            if (member is None or member['state'] != 'running' or member['claim'] != row['claim']
                    or member['policy_revision'] != row['policy_revision']
                    or member['expected_version'] != row['expected_version']):
                return None
        return row

    def _job_sources(self, conn, row):
        result = []
        for item in json.loads(row['read_sources_json']):
            ref = SourceRef.from_dict(item['ref'])
            source = self._resolve(conn, row['user_id'], ref)
            if source is None:
                return None
            result.append(_source_object(ref, source, item['id']))
        return result

    def job_source(self, job):
        with self._transaction() as conn:
            row = self._job(conn, job)
            return self._job_sources(conn, row) if row else None

    @staticmethod
    def _quote_ref(evidence, sources):
        if not isinstance(evidence, dict) or evidence.get('source_id') not in sources:
            raise StoryMemoryError('unknown evidence source')
        source = sources[evidence['source_id']]
        quote = _text(evidence.get('quote'), 'evidence quote', MAX_SOURCE_CHARS)
        text = source['text']
        if 'start' in evidence or 'end' in evidence:
            start, end = evidence.get('start'), evidence.get('end')
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                raise StoryMemoryError('invalid quote offsets')
            if text[start:end] != quote:
                raise StoryMemoryError('quote differs from source')
        else:
            start = text.find(quote)
            if start < 0 or text.find(quote, start + 1) >= 0:
                raise StoryMemoryError('quote must identify one exact source span')
            end = start + len(quote)
        original = SourceRef.from_dict(source['ref'])
        return replace(original, start=original.start + start, end=original.start + end)

    def _prepare_update(self, update, sources):
        if not isinstance(update, dict):
            raise StoryMemoryError('invalid story update')
        story_id = update.get('story_id')
        if story_id is not None:
            _id(story_id, 'story_id')
        title = _text(update.get('title'), 'title', 512)
        aliases = update.get('aliases', [])
        if not isinstance(aliases, list) or len(aliases) > 32:
            raise StoryMemoryError('invalid aliases')
        aliases = [_text(alias, 'alias', 256) for alias in aliases]
        used = {}
        def entries(values):
            if not isinstance(values, list) or len(values) > 64:
                raise StoryMemoryError('invalid entries')
            result = []
            for item in values:
                if not isinstance(item, dict):
                    raise StoryMemoryError('invalid entry')
                evidence = item.get('evidence')
                if evidence is None and isinstance(item.get('source_ids'), list):
                    if any(key not in sources for key in item['source_ids']):
                        raise StoryMemoryError('unknown evidence source')
                    evidence = [{'source_id': key, 'quote': sources[key]['text']}
                                for key in item['source_ids']]
                if not isinstance(evidence, list) or not 1 <= len(evidence) <= 64:
                    raise StoryMemoryError('entry requires evidence')
                refs = [self._quote_ref(part, sources) for part in evidence]
                used.update((ref.key, ref) for ref in refs)
                entry = StoryEntry(item.get('text'), item.get('kind'), item.get('actor'),
                                   item.get('status'), tuple(ref.key for ref in refs))
                checked = _entries([entry], refs)[0]
                result.append({'text': checked.text, 'kind': checked.kind, 'actor': checked.actor,
                               'status': checked.status, 'source_keys': list(checked.source_keys)})
            return result
        current = entries(update.get('current'))
        episode = entries(update.get('episode', []))
        if not current:
            raise StoryMemoryError('current state must have evidence')
        spans = update.get('source_spans', [])
        if not isinstance(spans, list) or len(spans) > 128:
            raise StoryMemoryError('invalid source spans')
        for span in spans:
            ref = self._quote_ref(span, sources)
            used[ref.key] = ref
        return story_id, title, aliases, current, episode, list(used.values())

    def commit(self, job, updates):
        if not isinstance(updates, list) or len(updates) > MAX_UPDATES or len(_json(updates)) > 200000:
            raise StoryMemoryError('invalid update batch')
        result_digest = _digest(updates)
        with self._transaction(True) as conn:
            row = self._job(conn, job, allow_done=True)
            if row is None:
                return False
            source_list = self._job_sources(conn, row)
            if source_list is None:
                return False
            if row['state'] == 'done':
                if row['result_digest'] != result_digest:
                    raise StoryMemoryError('completed checkpoint payload differs')
                return True
            sources = {item['id']: item for item in source_list}
            prepared = [self._prepare_update(update, sources) for update in updates]
            requested = [item[0] for item in prepared if item[0] is not None]
            if len(requested) != len(set(requested)):
                raise StoryMemoryError('one update per story per checkpoint')
            now = self._now()
            source_time = max((item['completed_at'] for item in source_list if item['id'].startswith('s')), default=now)
            for update, (story_id, title, aliases, current, episode, refs) in zip(updates, prepared):
                prior = None
                if story_id:
                    prior = conn.execute('SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                                         (row['user_id'], story_id)).fetchone()
                    if prior is None or not self._story_valid(conn, row['user_id'], prior):
                        raise StoryMemoryError('unknown or invalid existing story')
                previous_keys = {ref.key for ref in _refs(prior['refs_json'])} if prior else set()
                used_ids = {part['source_id'] for entry in [*update.get('current', []), *update.get('episode', [])]
                            for part in entry.get('evidence', [])}
                used_ids.update(key for entry in [*update.get('current', []), *update.get('episode', [])]
                                for key in entry.get('source_ids', []))
                used_ids.update(part['source_id'] for part in update.get('source_spans', []))
                for identifier in used_ids:
                    if identifier.startswith('p') and SourceRef.from_dict(sources[identifier]['ref']).key not in previous_keys:
                        raise StoryMemoryError('evidence belongs to a different story')
                if not any(identifier.startswith('s') for identifier in used_ids) and used_ids:
                    raise StoryMemoryError('update must cite this checkpoint source')
                story_id = story_id or 'story-' + secrets.token_hex(12)
                prior_refs = _refs(prior['refs_json']) if prior else []
                combined = {ref.key: ref for ref in [*prior_refs, *refs]}
                episodes = json.loads(prior['episodes_json']) if prior else []
                # Existing p* evidence may be newer than a historical batch. It
                # must not make that old batch appear to be a new decision.
                cited_new_sources = [source for identifier, source in sources.items()
                                     if identifier.startswith('s') and any(
                                         _message_key(ref) == _message_key(SourceRef.from_dict(source['ref']))
                                         and ref.sha256 == source['ref']['sha256']
                                         and source['ref']['start'] <= ref.start < ref.end <= source['ref']['end']
                                         for ref in refs)]
                story_source_time = max((source['completed_at'] for source in cited_new_sources), default=source_time)
                if (prior and not episode and title == prior['title']
                        and aliases == json.loads(prior['aliases_json'])
                        and current == json.loads(prior['current_json'])):
                    # A recall-only exchange adds deletion provenance without
                    # making an old fact newer than a later historical update.
                    story_source_time = prior['source_time']
                episodes.append({'job_id': row['job_id'], 'source_time': story_source_time,
                                 'created_at': now, 'entries': episode})
                if prior and story_source_time < prior['source_time']:
                    title, aliases, current = (prior['title'], json.loads(prior['aliases_json']),
                                               json.loads(prior['current_json']))
                version = prior['version'] + 1 if prior else 1
                conn.execute('''INSERT INTO story_runtime_stories
                    (user_id,story_id,title,aliases_json,current_json,episodes_json,refs_json,
                     version,updated_at,source_time,active) VALUES (?,?,?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(user_id,story_id) DO UPDATE SET title=excluded.title,
                    aliases_json=excluded.aliases_json,current_json=excluded.current_json,
                    episodes_json=excluded.episodes_json,refs_json=excluded.refs_json,
                    version=excluded.version,updated_at=excluded.updated_at,source_time=excluded.source_time,active=1''',
                             (row['user_id'], story_id, title, _json(aliases), _json(current),
                              _json(episodes), _refs_json(list(combined.values())), version, now,
                              max(story_source_time, prior['source_time'] if prior else story_source_time)))
            if updates:
                conn.execute('UPDATE story_runtime_policy SET data_revision=data_revision+1 WHERE user_id=?', (row['user_id'],))
            conn.execute("UPDATE story_runtime_jobs SET state='done',processed=1,claim=NULL,lease_until=NULL,error_code=NULL,result_digest=? "
                         "WHERE user_id=? AND state='running' AND claim=?",
                         (result_digest, row['user_id'], row['claim']))
            return True

    def fail(self, job, error):
        with self._transaction(True) as conn:
            row = conn.execute('SELECT * FROM story_runtime_jobs WHERE user_id=? AND job_id=? '
                               "AND state='running' AND claim=?", (job.get('user_id'), job.get('job_id'), job.get('claim'))).fetchone()
            if row is None:
                return
            code = type(error).__name__ if isinstance(error, Exception) else 'processing_failed'
            for member in conn.execute("SELECT * FROM story_runtime_jobs WHERE user_id=? AND state='running' AND claim=?",
                                       (row['user_id'], row['claim'])).fetchall():
                retry = member['attempts'] < MAX_ATTEMPTS
                conn.execute('UPDATE story_runtime_jobs SET state=?,claim=NULL,lease_until=NULL, '
                             'available_at=?,error_code=? WHERE sequence=?',
                             ('queued' if retry else 'failed', self._now() + 2 ** member['attempts'], code, member['sequence']))

    def candidates(self, user, query, limit=5):
        user = _id(user, 'user_id')
        query = _text(query, 'query', 20000)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise StoryMemoryError('invalid candidate limit')
        with self._transaction() as conn:
            if not self._policy(conn, user)['enabled']:
                return []
            result = []
            for row in self._candidate_rows(conn, user, query, limit):
                story = self._story_dict(row)
                story.pop('episodes')
                result.append(story)
            return result

    def list_stories(self, user):
        user = _id(user, 'user_id')
        with self._transaction() as conn:
            return [self._story_dict(row) for row in conn.execute(
                'SELECT * FROM story_runtime_stories WHERE user_id=? ORDER BY updated_at DESC,story_id', (user,))
                    if self._story_valid(conn, user, row)]

    def evidence(self, user, story_id):
        user, story_id = _id(user, 'user_id'), _id(story_id, 'story_id')
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?', (user, story_id)).fetchone()
            if row is None or not self._story_valid(conn, user, row):
                return []
            return [_source_object(ref, self._resolve(conn, user, ref), 'e' + str(index + 1))
                    for index, ref in enumerate(_refs(row['refs_json']))]

    def record_reply(self, user, request_id, story_ids, revision):
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        if not isinstance(story_ids, (list, tuple)) or len(story_ids) > 20:
            raise StoryMemoryError('invalid reply dependencies')
        ids = sorted(set(_id(value, 'story_id') for value in story_ids))
        with self._transaction(True) as conn:
            p = self._policy(conn, user)
            if not p['enabled'] or p['revision'] != revision:
                return False
            if not conn.execute('SELECT 1 FROM conversation_turns WHERE user_id=? AND request_id=?', (user, request_id)).fetchone():
                return False
            for identifier in ids:
                row = conn.execute('SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?', (user, identifier)).fetchone()
                if row is None or not self._story_valid(conn, user, row):
                    return False
            existing = conn.execute('SELECT stories_json FROM story_runtime_replies '
                                    'WHERE user_id=? AND request_id=?', (user, request_id)).fetchone()
            if existing:
                ids = sorted(set(ids) | set(json.loads(existing[0])))
            conn.execute('INSERT INTO story_runtime_replies VALUES (?,?,?,?) '
                         'ON CONFLICT(user_id,request_id) DO UPDATE SET stories_json=excluded.stories_json,revision=excluded.revision',
                         (user, request_id, _json(ids), revision))
            return True

    def record_context_reply(self, user, request_id, turns, summary):
        """Track erasure lineage of actual short-term model input, even when off.

        This stores identifiers only, not a new story or a source-content copy.
        It prevents an unindexed paraphrase of existing dialogue from escaping
        a later story deletion merely because lexical recall returned no hit.
        """
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        if not isinstance(turns, (list, tuple)):
            raise StoryMemoryError('invalid conversation context')
        with self._transaction(True) as conn:
            if not conn.execute('SELECT 1 FROM conversation_turns WHERE user_id=? '
                                'AND request_id=?', (user, request_id)).fetchone():
                return False
            supplied = {}
            ancestor_requests = set()
            for turn in turns:
                if getattr(turn, 'user_id', None) != user:
                    raise StoryMemoryError('context belongs to a different user')
                identity = (turn.conversation_id, turn.session_instance_id,
                            turn.generation, turn.turn_id)
                supplied[identity] = turn
                if turn.assistant_content:
                    ancestor_requests.add(turn.request_id)
            summarized = set()
            if summary is not None:
                if getattr(summary, 'user_id', None) != user:
                    raise StoryMemoryError('summary belongs to a different user')
                for row in conn.execute(
                    '''SELECT conversation_id,session_instance_id,generation,turn_id,request_id
                       FROM conversation_turns WHERE user_id=? AND conversation_id=?
                       AND session_instance_id=? AND generation=? AND status='completed'
                       AND ordinal BETWEEN ? AND ?''',
                    (user, summary.conversation_id, summary.session_instance_id,
                     summary.generation, summary.source_start_ordinal,
                     summary.source_end_ordinal),
                ):
                    summarized.add(tuple(row[:4]))
                    ancestor_requests.add(row['request_id'])
            ids = set()
            for row in conn.execute('SELECT story_id,refs_json FROM story_runtime_stories '
                                    'WHERE user_id=?', (user,)):
                for ref in _refs(row['refs_json']):
                    identity = (ref.conversation_id, ref.session_instance_id,
                                ref.generation, ref.turn_id)
                    if identity in summarized:
                        ids.add(row['story_id'])
                        break
                    turn = supplied.get(identity)
                    if turn is None:
                        continue
                    source = read_source(conn, user, ref)
                    displayed = (turn.user_content if ref.role == 'user'
                                 else turn.assistant_content) or ''
                    if source is not None and source.text in displayed:
                        ids.add(row['story_id'])
                        break
            for row in conn.execute('SELECT request_id,stories_json FROM story_runtime_replies '
                                    'WHERE user_id=?', (user,)):
                if row['request_id'] in ancestor_requests or row['request_id'] == request_id:
                    ids.update(json.loads(row['stories_json']))
            if ids:
                revision = self._policy(conn, user)['revision']
                conn.execute('INSERT INTO story_runtime_replies VALUES (?,?,?,?) '
                             'ON CONFLICT(user_id,request_id) DO UPDATE SET '
                             'stories_json=excluded.stories_json,revision=excluded.revision',
                             (user, request_id, _json(sorted(ids)), revision))
            return True

    def invalidate_summary(self, user, story_id):
        user, story_id = _id(user, 'user_id'), _id(story_id, 'story_id')
        with self._transaction(True) as conn:
            changed = conn.execute('UPDATE story_runtime_stories SET active=0 WHERE user_id=? AND story_id=?', (user, story_id)).rowcount
            if changed:
                conn.execute('UPDATE story_runtime_policy SET revision=revision+1,data_revision=data_revision+1 WHERE user_id=?', (user,))
                self._fence(conn, user)
            return bool(changed)

    @staticmethod
    def _merged_ranges(spans):
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def _remove_ranges(text, spans):
        for start, end in reversed(StoryRuntimeStore._merged_ranges(spans)):
            text = text[:start] + text[end:]
        return text

    @staticmethod
    def _remap_ref(ref, change):
        if ref.sha256 != hashlib.sha256(change['old'].encode('utf-8')).hexdigest():
            return None
        ranges = change['ranges']
        if any(start < ref.end and end > ref.start for start, end in ranges):
            return None
        shift = sum(end - start for start, end in ranges if end <= ref.start)
        return replace(ref, start=ref.start - shift, end=ref.end - shift,
                       sha256=hashlib.sha256(change['new'].encode('utf-8')).hexdigest())

    def delete_conversation(self, user, conversation_id):
        """Delete raw dialogue and fence its derived memory in one transaction.

        A story can combine several sessions. Its affected derived record is
        dropped conservatively, but other sessions' user messages are retained.
        Assistant paraphrases with explicit story lineage are cleared as well.
        """
        from .conversation import validate_conversation_id

        user = _id(user, 'user_id')
        conversation_id = validate_conversation_id(conversation_id)
        with self._transaction(True) as conn:
            if not conn.execute('SELECT 1 FROM conversation_sessions WHERE user_id=? '
                                'AND conversation_id=?', (user, conversation_id)).fetchone():
                return False
            pending_sequences = [(row[0], row[1]) for row in conn.execute(
                "SELECT sequence,state FROM story_runtime_jobs WHERE user_id=? "
                "AND state IN ('queued','running','failed')", (user,))]
            turns = conn.execute('SELECT * FROM conversation_turns WHERE user_id=?',
                                 (user,)).fetchall()
            removed_requests = {row['request_id'] for row in turns
                                if row['conversation_id'] == conversation_id}
            removed_identities = {(row['conversation_id'], row['session_instance_id'],
                                   row['generation'], row['turn_id']) for row in turns
                                  if row['request_id'] in removed_requests}
            stories = conn.execute('SELECT story_id,refs_json FROM story_runtime_stories '
                                   'WHERE user_id=?', (user,)).fetchall()
            replies = conn.execute('SELECT request_id,stories_json FROM story_runtime_replies '
                                   'WHERE user_id=?', (user,)).fetchall()
            affected_stories, dependent_replies = set(), set()
            # Follow explicit assistant provenance until no additional story is
            # affected. Text equality never authorizes deleting another source.
            while True:
                before = (len(affected_stories), len(dependent_replies))
                reply_identities = {(row['conversation_id'], row['session_instance_id'],
                                     row['generation'], row['turn_id']) for row in turns
                                    if row['request_id'] in dependent_replies}
                for story in stories:
                    if any((ref.conversation_id, ref.session_instance_id, ref.generation,
                            ref.turn_id) in removed_identities or (
                                ref.role == 'assistant' and
                                (ref.conversation_id, ref.session_instance_id, ref.generation,
                                 ref.turn_id) in reply_identities)
                           for ref in _refs(story['refs_json'])):
                        affected_stories.add(story['story_id'])
                for reply in replies:
                    if affected_stories.intersection(json.loads(reply['stories_json'])):
                        dependent_replies.add(reply['request_id'])
                if before == (len(affected_stories), len(dependent_replies)):
                    break
            affected_requests = removed_requests | dependent_replies
            sessions = {conversation_id}
            for row in turns:
                if row['request_id'] not in dependent_replies:
                    continue
                self._mark_reply_stale(conn, user, row['request_id'])
                if row['conversation_id'] == conversation_id:
                    continue
                sessions.add(row['conversation_id'])
                content = row['assistant_content'] or ''
                key = _json([row['conversation_id'], row['session_instance_id'],
                             row['generation'], row['turn_id'], 'assistant'])
                conn.execute('INSERT OR IGNORE INTO story_runtime_exclusions VALUES (?,?,?)',
                             (user, key, hashlib.sha256(content.encode('utf-8')).hexdigest()))
                conn.execute("UPDATE conversation_turns SET assistant_content='',response_json='{}',"
                             'request_fingerprint=? WHERE user_id=? AND request_id=?',
                             ('story-revoked-' + secrets.token_hex(16), user, row['request_id']))
            for story_id in affected_stories:
                conn.execute('DELETE FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                             (user, story_id))
            for request_id in affected_requests:
                conn.execute('DELETE FROM story_runtime_replies WHERE user_id=? AND request_id=?',
                             (user, request_id))
                conn.execute('DELETE FROM story_runtime_scope WHERE user_id=? AND request_id=?',
                             (user, request_id))
                conn.execute("UPDATE story_runtime_jobs SET state='cancelled',claim=NULL,"
                             "lease_until=NULL,read_sources_json='[]',result_digest=NULL "
                             'WHERE user_id=? AND request_id=?', (user, request_id))
            fact_ids = [fact.id for fact in self.memory.list_for_user(user, connection=conn)
                        if fact.metadata.get('source', {}).get('conversation_id') == conversation_id]
            if fact_ids:
                self.memory.remove_facts(user, fact_ids, connection=conn)
            for session in sessions:
                conn.execute('DELETE FROM conversation_summaries WHERE user_id=? AND conversation_id=?',
                             (user, session))
                conn.execute('UPDATE conversation_sessions SET revision=revision+1 '
                             'WHERE user_id=? AND conversation_id=?', (user, session))
            conn.execute('DELETE FROM conversation_sessions WHERE user_id=? AND conversation_id=?',
                         (user, conversation_id))
            # Also create the epoch for a legacy user who never enabled stories.
            conn.execute('INSERT OR IGNORE INTO story_runtime_policy(user_id) VALUES (?)', (user,))
            conn.execute('UPDATE story_runtime_policy SET revision=revision+1,data_revision=data_revision+1 '
                         'WHERE user_id=?', (user,))
            self._fence(conn, user)
            revision = self._policy(conn, user)['revision']
            for sequence, state in pending_sequences:
                job = conn.execute('SELECT * FROM story_runtime_jobs WHERE sequence=?', (sequence,)).fetchone()
                if job['request_id'] not in affected_requests and all(
                        self._resolve(conn, user, ref) is not None for ref in _refs(job['sources_json'])):
                    conn.execute('UPDATE story_runtime_jobs SET state=?,policy_revision=?,available_at=? '
                                 'WHERE sequence=?', ('failed' if state == 'failed' else 'queued',
                                                     revision, self._now(), sequence))
            return True

    def delete_story(self, user, story_id, require_settled=False):
        user, story_id = _id(user, 'user_id'), _id(story_id, 'story_id')
        if type(require_settled) is not bool:
            raise StoryMemoryError('require_settled must be boolean')
        with self._transaction(True) as conn:
            story = conn.execute('SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?', (user, story_id)).fetchone()
            if story is None:
                return {'deleted': False, 'stories': 0, 'source_messages': 0, 'facts': 0}
            if require_settled:
                policy = self._policy(conn, user)
                completed = conn.execute("SELECT * FROM conversation_turns WHERE user_id=? "
                                         "AND status='completed'", (user,)).fetchall()
                if policy['enabled']:
                    for turn in completed:
                        self._enqueue(conn, user, turn['request_id'])
                if conn.execute("SELECT 1 FROM story_runtime_jobs WHERE user_id=? "
                                "AND state IN ('queued','running','failed') LIMIT 1", (user,)).fetchone():
                    raise StoryMemoryError('story_delete_requires_settled_sources')
                # Off cancels extraction, not its unresolved source scope. A
                # successful checkpoint is durable even after deletion fences
                # its replay. Never silently send cancelled input to a model.
                for turn in completed:
                    if not self._grant(conn, user, turn, policy):
                        continue
                    source_refs = [ref for ref in self._raw_refs(conn, user, turn)
                                   if self._resolve(conn, user, ref) is not None]
                    if not any(ref.role == 'user' for ref in source_refs):
                        continue
                    jobs = conn.execute('SELECT processed,sources_json FROM story_runtime_jobs '
                                        'WHERE user_id=? AND request_id=?',
                                        (user, turn['request_id'])).fetchall()
                    processed = any(job['processed'] and any(
                        ref.conversation_id == turn['conversation_id']
                        and ref.session_instance_id == turn['session_instance_id']
                        and ref.generation == turn['generation'] and ref.turn_id == turn['turn_id']
                        for ref in _refs(job['sources_json'])) for job in jobs)
                    if not processed:
                        raise StoryMemoryError('story_delete_requires_history_review')
            refs = _refs(story['refs_json'])
            pending_sequences = [(row[0], row[1]) for row in conn.execute(
                "SELECT sequence,state FROM story_runtime_jobs WHERE user_id=? AND state IN ('queued','running','failed')", (user,))]
            spans = {}
            for ref in refs:
                source = read_source(conn, user, ref)
                if source is not None:
                    spans.setdefault(_message_key(ref), []).append((ref.start, ref.end))
            dependent_replies = set()
            for row in conn.execute('SELECT * FROM story_runtime_replies WHERE user_id=?', (user,)):
                if story_id in json.loads(row['stories_json']):
                    dependent_replies.add(row['request_id'])
            changed_turns, changed_messages, sessions, changes = [], set(), set(), {}
            rows = conn.execute('SELECT * FROM conversation_turns WHERE user_id=?', (user,)).fetchall()
            for row in rows:
                replacement = {}
                for role, field in (('user', 'user_content'), ('assistant', 'assistant_content')):
                    content = row[field] or ''
                    key = _json([row['conversation_id'], row['session_instance_id'], row['generation'], row['turn_id'], role])
                    ranges = list(spans.get(key, []))
                    # Identical generic responses can belong to unrelated stories.
                    # Only explicit source spans or propagated reply dependencies
                    # authorize redaction; text equality alone is not provenance.
                    if role == 'assistant' and row['request_id'] in dependent_replies:
                        ranges = [(0, len(content))] if content else []
                    if ranges:
                        new = self._remove_ranges(content, ranges)
                        replacement[field] = new
                        changed_messages.add(key)
                        changes[key] = {'old': content, 'new': new,
                                        'ranges': self._merged_ranges(ranges)}
                        conn.execute('INSERT OR IGNORE INTO story_runtime_exclusions VALUES (?,?,?)',
                                     (user, key, hashlib.sha256(content.encode('utf-8')).hexdigest()))
                if replacement or row['request_id'] in dependent_replies:
                    changed_turns.append(row)
                    sessions.add(row['conversation_id'])
                    conn.execute('UPDATE conversation_turns SET user_content=?,assistant_content=?, '
                                 'response_json=?,request_fingerprint=? WHERE user_id=? AND request_id=?',
                                 (replacement.get('user_content', row['user_content']),
                                  replacement.get('assistant_content', row['assistant_content']), '{}',
                                  'story-revoked-' + secrets.token_hex(16), user, row['request_id']))
                    self._mark_reply_stale(conn, user, row['request_id'])
            # Invalidate every derived story whose whole-message digest changed;
            # remaining raw text is retained and can be extracted again safely.
            invalidated = 0
            for other in conn.execute('SELECT * FROM story_runtime_stories WHERE user_id=?', (user,)).fetchall():
                if other['story_id'] == story_id:
                    continue
                previous_refs = _refs(other['refs_json'])
                if any(_message_key(ref) in changed_messages for ref in previous_refs):
                    mapped = {ref.key: (self._remap_ref(ref, changes[_message_key(ref)])
                                        if _message_key(ref) in changes else ref)
                              for ref in previous_refs}
                    def clean_entries(entries):
                        result = []
                        for entry in entries:
                            if all(mapped.get(key) is not None for key in entry['source_keys']):
                                entry['source_keys'] = [mapped[key].key for key in entry['source_keys']]
                                result.append(entry)
                        return result
                    old_current = json.loads(other['current_json'])
                    current = clean_entries(old_current)
                    episodes = json.loads(other['episodes_json'])
                    for episode in episodes:
                        episode['entries'] = clean_entries(episode['entries'])
                    episodes = [episode for episode in episodes if episode['entries']]
                    if not current:
                        # Do not leave unreachable sensitive titles/aliases behind.
                        conn.execute('DELETE FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                                     (user, other['story_id']))
                        invalidated += 1
                    else:
                        removed_current = len(current) != len(old_current)
                        title = current[0]['text'][:128] if removed_current else other['title']
                        aliases = '[]' if removed_current else other['aliases_json']
                        conn.execute('UPDATE story_runtime_stories SET refs_json=?,current_json=?,episodes_json=?, '
                                     'title=?,aliases_json=?,version=version+1 WHERE user_id=? AND story_id=?',
                                     (_refs_json([ref for ref in mapped.values() if ref is not None]),
                                      _json(current), _json(episodes), title, aliases, user, other['story_id']))
            conn.execute('DELETE FROM story_runtime_stories WHERE user_id=? AND story_id=?', (user, story_id))
            for conversation_id in sessions:
                conn.execute('DELETE FROM conversation_summaries WHERE user_id=? AND conversation_id=?', (user, conversation_id))
            affected_requests = {row['request_id'] for row in changed_turns}
            fact_ids = []
            for fact in self.memory.list_for_user(user, connection=conn):
                origin = fact.metadata.get('source', {})
                key = _json([origin.get('conversation_id'), origin.get('session_instance_id'),
                             origin.get('generation'), origin.get('turn_id'), 'user'])
                evidence = fact.metadata.get('fact', {}).get('evidence', '')
                if key in changes:
                    change = changes[key]
                    starts = [match.start() for match in re.finditer(re.escape(evidence), change['old'])] if evidence else []
                    if not starts or any(start < offset + len(evidence) and end > offset
                                         for offset in starts for start, end in change['ranges']):
                        fact_ids.append(fact.id)
                    else:
                        # A preserved fact's source metadata is another raw copy.
                        metadata = dict(fact.metadata)
                        metadata['source'] = dict(origin, text=change['new'])
                        conn.execute('UPDATE memories SET metadata_json=? WHERE user_id=? AND id=?',
                                     (_json(metadata), user, fact.id))
            if fact_ids:
                self.memory.remove_facts(user, fact_ids, connection=conn)
            for reply in conn.execute('SELECT * FROM story_runtime_replies WHERE user_id=?', (user,)).fetchall():
                identifiers = [value for value in json.loads(reply['stories_json']) if value != story_id]
                if reply['request_id'] in affected_requests or not identifiers:
                    conn.execute('DELETE FROM story_runtime_replies WHERE user_id=? AND request_id=?', (user, reply['request_id']))
                else:
                    conn.execute('UPDATE story_runtime_replies SET stories_json=? WHERE user_id=? AND request_id=?',
                                 (_json(identifiers), user, reply['request_id']))
            conn.execute('UPDATE story_runtime_policy SET revision=revision+1,data_revision=data_revision+1 WHERE user_id=?', (user,))
            self._fence(conn, user)
            revision = self._policy(conn, user)['revision']
            for sequence, old_state in pending_sequences:
                job = conn.execute('SELECT * FROM story_runtime_jobs WHERE sequence=?', (sequence,)).fetchone()
                if job['request_id'] not in affected_requests and all(
                        self._resolve(conn, user, ref) is not None for ref in _refs(job['sources_json'])):
                    conn.execute('UPDATE story_runtime_jobs SET state=?,policy_revision=?,available_at=? '
                                 'WHERE sequence=?', ('failed' if old_state == 'failed' else 'queued',
                                                     revision, self._now(), sequence))
            # Cancel completed jobs too: a replay must not return or recreate old output.
            for request_id in affected_requests:
                conn.execute("UPDATE story_runtime_jobs SET state='cancelled',read_sources_json='[]',result_digest=NULL "
                             'WHERE user_id=? AND request_id=?', (user, request_id))
            return {'deleted': True, 'stories': 1, 'source_messages': len(changed_messages),
                    'facts': len(fact_ids), 'invalidated_stories': invalidated,
                    'physical_erasure': False}

    def own_reply_updated_story(self, user, story_id, request_id):
        """Recognize only a valid latest checkpoint ending with this reply.

        This is not a general freshness bypass: any later source turn in the
        batch, even with an equal clock timestamp, makes the answer false.
        """
        user, story_id, request_id = (_id(user, 'user_id'), _id(story_id, 'story_id'),
                                     _id(request_id, 'request_id'))
        with self._transaction() as conn:
            if not self._policy(conn, user)['enabled']:
                return False
            story = conn.execute('SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                                 (user, story_id)).fetchone()
            own = conn.execute("SELECT rowid,* FROM conversation_turns WHERE user_id=? AND request_id=? "
                               "AND status='completed'", (user, request_id)).fetchone()
            if story is None or own is None or not self._story_valid(conn, user, story):
                return False
            episodes = json.loads(story['episodes_json'])
            if not episodes or story['source_time'] > own['completed_at']:
                return False
            latest = conn.execute("SELECT * FROM story_runtime_jobs WHERE user_id=? AND job_id=? AND state='done'",
                                  (user, episodes[-1]['job_id'])).fetchone()
            if latest is None:
                return False
            members = json.loads(latest['batch_json']) or [latest['job_id']]
            found = False
            for identifier in members:
                member = conn.execute('SELECT t.rowid,t.request_id,t.completed_at FROM story_runtime_jobs j '
                                      'JOIN conversation_turns t ON t.user_id=j.user_id AND t.request_id=j.request_id '
                                      'WHERE j.user_id=? AND j.job_id=?', (user, identifier)).fetchone()
                if member is None or member['rowid'] > own['rowid'] or member['completed_at'] > own['completed_at']:
                    return False
                found = found or member['request_id'] == request_id
            return found

    def stats(self, user):
        user = _id(user, 'user_id')
        with self._transaction() as conn:
            counts = {row[0]: row[1] for row in conn.execute('SELECT state,COUNT(*) FROM story_runtime_jobs '
                                                           'WHERE user_id=? GROUP BY state', (user,))}
            result = {state: counts.get(state, 0) for state in ('queued', 'running', 'done', 'failed', 'cancelled')}
            result['stories'] = conn.execute('SELECT COUNT(*) FROM story_runtime_stories WHERE user_id=? AND active=1', (user,)).fetchone()[0]
            result['policy'] = self._public_policy(self._policy(conn, user))
            result['last_error'] = (conn.execute('SELECT error_code FROM story_runtime_jobs WHERE user_id=? '
                                               "AND state IN ('queued','running','failed') AND error_code IS NOT NULL "
                                               'ORDER BY sequence DESC LIMIT 1', (user,)).fetchone() or [None])[0]
            return result

    def history_preview(self, user):
        user = _id(user, 'user_id')
        with self._transaction() as conn:
            row = conn.execute("SELECT COUNT(*),MIN(created_at),MAX(completed_at) FROM conversation_turns "
                               "WHERE user_id=? AND status='completed'", (user,)).fetchone()
            turns = []
            for turn in conn.execute("SELECT * FROM conversation_turns WHERE user_id=? AND status='completed'", (user,)):
                turns.append({'request_id': turn['request_id'], 'identity': _identity(turn),
                              'source_digest': _digest([ref.key for ref in self._raw_refs(conn, user, turn)])})
            return {'user_id': user, 'turn_count': row[0], 'first_at': row[1], 'last_at': row[2], 'turns': turns}

    def retry_failed(self, user):
        """Explicit operator retry of technical failures, never cancelled work."""
        user = _id(user, 'user_id')
        with self._transaction(True) as conn:
            p = self._policy(conn, user)
            if not p['enabled'] or not p['external_consent']:
                return 0
            count = 0
            for row in conn.execute("SELECT * FROM story_runtime_jobs WHERE user_id=? AND state='failed'", (user,)).fetchall():
                if (row['policy_revision'] != p['revision'] or row['error_code'] == 'source_too_large'
                        or any(self._resolve(conn, user, ref) is None for ref in _refs(row['sources_json']))):
                    continue
                conn.execute("UPDATE story_runtime_jobs SET state='queued',attempts=0,available_at=?, "
                             "claim=NULL,lease_until=NULL,error_code=NULL WHERE sequence=?", (self._now(), row['sequence']))
                count += 1
            return count
