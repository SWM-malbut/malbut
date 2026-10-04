"""Offline SQLite prototype for consented stories with original-turn evidence.

Only this module's ``story_memory_*`` tables are written. Original conversation
messages stay in the existing conversation tables and are read through
``story_memory_sources``. Callers supply trusted user identities and manually
prepared summaries: identifier validation is not authentication, and validating
citations does not establish semantic entailment. Returned text is untrusted
data and never execution authority.

There is no model call, automatic extraction, worker, runtime integration, or
deletion API. Source grants are exact SourceRefs, not whole-session grants.
Each checkpoint explicitly includes every source its new entries cite, including
old sources reused in a new current summary. A story's historical read sets are
also revalidated, so one missing or unapproved dependency hides the whole story.
Search scans this user's stories lexically; this is a small-data prototype.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
from typing import List, Optional, Sequence, Tuple

from .story_memory_sources import SourceRef, SourceText, read_source


MAX_ID_CHARS = 128
MAX_ENTRY_CHARS = 4000
MAX_ENTRIES = 64
MAX_JOB_SOURCES = 256
MAX_SCOPE_SOURCES = 4096
MAX_PAYLOAD_CHARS = 65536
KINDS = frozenset({'context', 'goal', 'experience', 'emotion', 'decision', 'question'})
ACTORS = frozenset({'user', 'assistant', 'inference'})
STATUSES = frozenset({'stated', 'proposed', 'confirmed', 'open', 'superseded'})


class StoryMemoryError(ValueError):
    """Invalid prototype input or operation."""


class StoryConsentError(StoryMemoryError):
    """Disabled, changed, or insufficient exact-source consent."""


class StoryConflictError(StoryMemoryError):
    """A checkpoint identity or expected story version no longer matches."""


class StorySourceError(StoryMemoryError):
    """Evidence is malformed, missing, changed, or not owned by the user."""


@dataclass(frozen=True)
class StoryEntry:
    text: str
    kind: str
    actor: str
    status: str
    source_keys: Tuple[str, ...]


@dataclass(frozen=True)
class StorySnapshot:
    """Episodes are per-checkpoint entry tuples in commit/version order."""

    story_id: str
    title: str
    aliases: Tuple[str, ...]
    current: Tuple[StoryEntry, ...]
    episodes: Tuple[Tuple[StoryEntry, ...], ...]
    version: int
    updated_at: float
    untrusted: bool = True
    execution_authorized: bool = False


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _text(value, label, maximum, *, identifier=False):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any((ord(c) < 32 and (identifier or c not in '\n\t'))
                   or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or (identifier and value != value.strip())):
        raise StoryMemoryError('invalid ' + label)
    return value


def _id(value, label='identifier'):
    return _text(value, label, MAX_ID_CHARS, identifier=True)


def _sequence(value, label, maximum, *, nonempty=False):
    if (not isinstance(value, (list, tuple)) or len(value) > maximum
            or (nonempty and not value)):
        raise StoryMemoryError('invalid ' + label)
    return value


def _refs(values, maximum=MAX_JOB_SOURCES, *, nonempty=True):
    try:
        _sequence(values, 'source references', maximum, nonempty=nonempty)
        by_key = {}
        for ref in values:
            if not isinstance(ref, SourceRef):
                raise ValueError('expected SourceRef')
            normalized = SourceRef.from_dict(ref.to_dict())
            if normalized != ref:
                raise ValueError('noncanonical SourceRef')
            by_key[ref.key] = ref
        return tuple(by_key[key] for key in sorted(by_key))
    except (TypeError, ValueError, AttributeError) as error:
        raise StorySourceError('invalid source references') from error


def _decode_refs(value):
    try:
        return tuple(SourceRef.from_dict(item) for item in json.loads(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise StorySourceError('stored source references are invalid') from error


def _encoded_refs(refs):
    return _json([ref.to_dict() for ref in refs])


def _entries(values, refs):
    _sequence(values, 'story entries', MAX_ENTRIES)
    available = {ref.key: ref for ref in refs}
    result = []
    for entry in values:
        if not isinstance(entry, StoryEntry):
            raise StoryMemoryError('expected StoryEntry')
        _text(entry.text, 'entry text', MAX_ENTRY_CHARS)
        if (type(entry.kind) is not str or entry.kind not in KINDS
                or type(entry.actor) is not str or entry.actor not in ACTORS
                or type(entry.status) is not str or entry.status not in STATUSES):
            raise StoryMemoryError('invalid entry kind, actor, or status')
        keys = _sequence(entry.source_keys, 'entry sources', MAX_JOB_SOURCES,
                         nonempty=True)
        if any(type(key) is not str or key not in available for key in keys):
            raise StorySourceError('entry cites a source outside the checkpoint')
        roles = {available[key].role for key in keys}
        if entry.actor == 'user' and roles != {'user'}:
            raise StorySourceError('user entries require only user evidence')
        if entry.actor == 'assistant' and 'assistant' not in roles:
            raise StorySourceError('assistant entries require assistant evidence')
        if ((entry.actor == 'assistant' and entry.status == 'confirmed')
                or (entry.actor == 'inference'
                    and entry.status not in {'proposed', 'open'})):
            raise StoryMemoryError('entry status overstates its actor authority')
        result.append(StoryEntry(entry.text, entry.kind, entry.actor,
                                 entry.status, tuple(sorted(set(keys)))))
    return tuple(result)


def _decode_entries(value):
    return tuple(StoryEntry(item['text'], item['kind'], item['actor'],
                            item['status'], tuple(item['source_keys']))
                 for item in json.loads(value))


def _normalized(text):
    return unicodedata.normalize('NFKC', text).casefold()


class SQLiteStoryMemoryStore:
    """Own a connection; durable jobs need no live session or runtime worker."""

    def __init__(self, database_path, clock=time.time):
        if not isinstance(database_path, (str, Path)) or not str(database_path):
            raise StoryMemoryError('database_path must not be empty')
        if not callable(clock):
            raise StoryMemoryError('clock must be callable')
        self.database_path = str(database_path)
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        path = (':memory:' if self.database_path == ':memory:'
                else str(Path(self.database_path).expanduser()))
        self._connection = sqlite3.connect(path, isolation_level=None,
                                           check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute('PRAGMA foreign_keys=ON')
            self._connection.execute('PRAGMA busy_timeout=5000')
            self._initialize()
        except Exception:
            self._connection.close()
            raise

    def _initialize(self):
        statements = (
            '''CREATE TABLE IF NOT EXISTS story_memory_consent (
                user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                revision INTEGER NOT NULL, sources_json TEXT NOT NULL,
                updated_at REAL NOT NULL, CHECK(enabled IN (0, 1)),
                CHECK(revision >= 0))''',
            '''CREATE TABLE IF NOT EXISTS story_memory_current (
                user_id TEXT NOT NULL, story_id TEXT NOT NULL,
                title TEXT NOT NULL, aliases_json TEXT NOT NULL,
                current_json TEXT NOT NULL, version INTEGER NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(user_id, story_id),
                CHECK(version >= 1))''',
            '''CREATE TABLE IF NOT EXISTS story_memory_checkpoints (
                user_id TEXT NOT NULL, job_id TEXT NOT NULL,
                story_id TEXT NOT NULL, policy_revision INTEGER NOT NULL,
                expected_version INTEGER NOT NULL, sources_json TEXT NOT NULL,
                state TEXT NOT NULL, payload_json TEXT, created_at REAL NOT NULL,
                completed_at REAL, PRIMARY KEY(user_id, job_id),
                CHECK(state IN ('pending', 'completed', 'invalidated')),
                CHECK(expected_version >= 0), CHECK(policy_revision >= 0))''',
            '''CREATE TABLE IF NOT EXISTS story_memory_episodes (
                user_id TEXT NOT NULL, story_id TEXT NOT NULL,
                version INTEGER NOT NULL, job_id TEXT NOT NULL,
                entries_json TEXT NOT NULL, sources_json TEXT NOT NULL,
                created_at REAL NOT NULL, PRIMARY KEY(user_id, story_id, version),
                UNIQUE(user_id, job_id),
                FOREIGN KEY(user_id, story_id)
                    REFERENCES story_memory_current(user_id, story_id))''',
        )
        with self._transaction(write=True) as conn:
            for statement in statements:
                conn.execute(statement)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    @contextmanager
    def _transaction(self, *, write=False):
        with self._lock:
            if self._closed:
                raise StoryMemoryError('store is closed')
            conn = self._connection
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _now(self):
        value = self._clock()
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value < 0):
            raise StoryMemoryError('clock must return a finite nonnegative time')
        return float(value)

    @staticmethod
    def _policy(conn, user_id):
        row = conn.execute('SELECT * FROM story_memory_consent WHERE user_id=?',
                           (user_id,)).fetchone()
        return (dict(row) if row else {
            'enabled': 0, 'revision': 0, 'sources_json': '[]',
        })

    @staticmethod
    def _require_policy(policy, expected_revision=None):
        if not policy['enabled'] or (expected_revision is not None
                                     and policy['revision'] != expected_revision):
            raise StoryConsentError('story consent is disabled or has changed')

    @staticmethod
    def _read_sources(conn, user_id, refs, policy=None):
        allowed = ({ref.key for ref in _decode_refs(policy['sources_json'])}
                   if policy is not None else None)
        result = []
        for ref in refs:
            if allowed is not None and ref.key not in allowed:
                raise StoryConsentError('source is outside the consent scope')
            try:
                source = read_source(conn, user_id, ref)
            except (ValueError, TypeError) as error:
                raise StorySourceError('invalid source reference') from error
            if source is None:
                raise StorySourceError('source is unavailable or has changed')
            result.append(source)
        return result

    def set_consent(self, user_id, enabled, allowed_sources=None) -> int:
        """Grant exact sources; None preserves scope, including when disabling."""
        user_id = _id(user_id, 'user_id')
        if type(enabled) is not bool:
            raise StoryMemoryError('enabled must be boolean')
        supplied = (None if allowed_sources is None else
                    _refs(allowed_sources, MAX_SCOPE_SOURCES, nonempty=False))
        with self._transaction(write=True) as conn:
            old = self._policy(conn, user_id)
            refs = supplied if supplied is not None else _decode_refs(old['sources_json'])
            # Turning off must still work when an old source has been deleted.
            if supplied is not None or enabled:
                self._read_sources(conn, user_id, refs)
            encoded = _encoded_refs(refs)
            changed = bool(old['enabled']) != enabled or old['sources_json'] != encoded
            revision = old['revision'] + int(changed)
            conn.execute('''INSERT INTO story_memory_consent
                (user_id, enabled, revision, sources_json, updated_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled, revision=excluded.revision,
                sources_json=excluded.sources_json, updated_at=excluded.updated_at''',
                         (user_id, int(enabled), revision, encoded, self._now()))
            if changed:
                conn.execute('''UPDATE story_memory_checkpoints SET state='invalidated'
                    WHERE user_id=? AND state='pending' ''', (user_id,))
            return revision

    @staticmethod
    def _story_row(conn, user_id, story_id):
        return conn.execute('''SELECT * FROM story_memory_current
            WHERE user_id=? AND story_id=?''', (user_id, story_id)).fetchone()

    @staticmethod
    def _story_refs(conn, user_id, story_id):
        rows = conn.execute('''SELECT sources_json FROM story_memory_episodes
            WHERE user_id=? AND story_id=? ORDER BY version''',
                            (user_id, story_id)).fetchall()
        refs = {}
        for row in rows:
            for ref in _decode_refs(row['sources_json']):
                refs.setdefault(ref.key, ref)
        return tuple(refs.values())

    def begin_checkpoint(self, user_id, story_id, job_id,
                         sources: Sequence[SourceRef]) -> str:
        """Persist the read set and expected revisions; return the durable job ID."""
        user_id, story_id, job_id = (_id(user_id, 'user_id'),
                                    _id(story_id, 'story_id'), _id(job_id, 'job_id'))
        refs = _refs(sources)
        encoded = _encoded_refs(refs)
        with self._transaction(write=True) as conn:
            policy = self._policy(conn, user_id)
            self._require_policy(policy)
            self._read_sources(conn, user_id, refs, policy)
            self._read_sources(conn, user_id, self._story_refs(conn, user_id, story_id), policy)
            job = conn.execute('''SELECT * FROM story_memory_checkpoints
                WHERE user_id=? AND job_id=?''', (user_id, job_id)).fetchone()
            story = self._story_row(conn, user_id, story_id)
            version = story['version'] if story else 0
            if job:
                if job['story_id'] != story_id or job['sources_json'] != encoded:
                    raise StoryConflictError('job_id was used for a different checkpoint')
                self._require_policy(policy, job['policy_revision'])
                if job['state'] == 'invalidated' or (job['state'] == 'pending'
                                                    and job['expected_version'] != version):
                    raise StoryConflictError('checkpoint was invalidated or superseded')
                return job_id
            conn.execute('''INSERT INTO story_memory_checkpoints
                (user_id, job_id, story_id, policy_revision, expected_version,
                 sources_json, state, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)''',
                         (user_id, job_id, story_id, policy['revision'], version,
                          encoded, self._now()))
            return job_id

    def commit_checkpoint(self, user_id, job_id, title, aliases,
                          current: Sequence[StoryEntry],
                          episode: Sequence[StoryEntry]) -> StorySnapshot:
        """Atomically save an episode/current state and finish a still-valid job."""
        user_id, job_id = _id(user_id, 'user_id'), _id(job_id, 'job_id')
        title = _text(title, 'story title', 512)
        aliases = tuple(_text(alias, 'story alias', 256) for alias in
                        _sequence(aliases, 'aliases', 32))
        with self._transaction(write=True) as conn:
            job = conn.execute('''SELECT * FROM story_memory_checkpoints
                WHERE user_id=? AND job_id=?''', (user_id, job_id)).fetchone()
            if job is None:
                raise StoryConflictError('checkpoint does not exist')
            policy = self._policy(conn, user_id)
            self._require_policy(policy, job['policy_revision'])
            if job['state'] == 'invalidated':
                raise StoryConflictError('checkpoint was invalidated')
            refs = _decode_refs(job['sources_json'])
            self._read_sources(conn, user_id, refs, policy)
            self._read_sources(conn, user_id,
                               self._story_refs(conn, user_id, job['story_id']), policy)
            current, episode = _entries(current, refs), _entries(episode, refs)
            if not current:
                raise StoryMemoryError('current entries must not be empty')
            payload = _json({'title': title, 'aliases': aliases,
                             'current': [asdict(item) for item in current],
                             'episode': [asdict(item) for item in episode]})
            if len(payload) > MAX_PAYLOAD_CHARS:
                raise StoryMemoryError('checkpoint payload is too large')
            if job['state'] == 'completed':
                if payload != job['payload_json']:
                    raise StoryConflictError('completed job payload differs')
                return self._snapshot(conn, user_id, job['story_id'],
                                      job['expected_version'] + 1, job['completed_at'],
                                      json.loads(job['payload_json']))
            row = self._story_row(conn, user_id, job['story_id'])
            if (row['version'] if row else 0) != job['expected_version']:
                raise StoryConflictError('story version has changed')
            version, now = job['expected_version'] + 1, self._now()
            conn.execute('''INSERT INTO story_memory_current
                (user_id, story_id, title, aliases_json, current_json, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, story_id) DO UPDATE SET
                title=excluded.title, aliases_json=excluded.aliases_json,
                current_json=excluded.current_json, version=excluded.version,
                updated_at=excluded.updated_at''',
                         (user_id, job['story_id'], title, _json(aliases),
                          _json([asdict(item) for item in current]), version, now))
            conn.execute('''INSERT INTO story_memory_episodes
                (user_id, story_id, version, job_id, entries_json, sources_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                         (user_id, job['story_id'], version, job_id,
                          _json([asdict(item) for item in episode]), job['sources_json'], now))
            conn.execute('''UPDATE story_memory_checkpoints SET state='completed',
                payload_json=?, completed_at=? WHERE user_id=? AND job_id=?''',
                         (payload, now, user_id, job_id))
            return self._snapshot(conn, user_id, job['story_id'], version, now,
                                  json.loads(payload))

    @staticmethod
    def _snapshot(conn, user_id, story_id, version, updated_at, payload):
        episodes = conn.execute('''SELECT entries_json FROM story_memory_episodes
            WHERE user_id=? AND story_id=? AND version<=? ORDER BY version''',
                                (user_id, story_id, version)).fetchall()
        return StorySnapshot(story_id, payload['title'], tuple(payload['aliases']),
                             _decode_entries(_json(payload['current'])),
                             tuple(_decode_entries(row['entries_json']) for row in episodes),
                             version, updated_at)

    def _visible(self, conn, user_id, row, policy):
        if row is None or not policy['enabled']:
            return False
        try:
            self._read_sources(conn, user_id,
                               self._story_refs(conn, user_id, row['story_id']), policy)
            return True
        except (StoryConsentError, StorySourceError):
            return False

    def _row_snapshot(self, conn, user_id, row):
        return self._snapshot(conn, user_id, row['story_id'], row['version'], row['updated_at'],
                              {'title': row['title'], 'aliases': json.loads(row['aliases_json']),
                               'current': json.loads(row['current_json'])})

    def get_story(self, user_id, story_id) -> Optional[StorySnapshot]:
        user_id, story_id = _id(user_id, 'user_id'), _id(story_id, 'story_id')
        with self._transaction() as conn:
            row = self._story_row(conn, user_id, story_id)
            if not self._visible(conn, user_id, row, self._policy(conn, user_id)):
                return None
            return self._row_snapshot(conn, user_id, row)

    def search(self, user_id, query, limit=5) -> List[StorySnapshot]:
        """Search all eligible stories lexically, without a recency candidate cap."""
        user_id = _id(user_id, 'user_id')
        query = _normalized(_text(query, 'query', 2048))
        if type(limit) is not int or not 1 <= limit <= 50:
            raise StoryMemoryError('limit must be an integer between 1 and 50')
        terms = set(re.findall(r'\w+', query))
        with self._transaction() as conn:
            policy = self._policy(conn, user_id)
            if not policy['enabled'] or not terms:
                return []
            rows = conn.execute('''SELECT * FROM story_memory_current WHERE user_id=?''',
                                (user_id,)).fetchall()
            ranked = []
            for row in rows:
                text = _normalized(' '.join([
                    row['title'], *json.loads(row['aliases_json']),
                    *(entry['text'] for entry in json.loads(row['current_json'])),
                ]))
                score = sum(term in text for term in terms) + int(query in text)
                if score and self._visible(conn, user_id, row, policy):
                    ranked.append((score, row))
            ranked.sort(key=lambda item: (-item[0], -item[1]['updated_at'],
                                          item[1]['story_id']))
            return [self._row_snapshot(conn, user_id, row) for _, row in ranked[:limit]]

    def get_evidence(self, user_id, story_id) -> List[SourceText]:
        """Return exact granted slices; never widen scope to neighboring turns."""
        user_id, story_id = _id(user_id, 'user_id'), _id(story_id, 'story_id')
        with self._transaction() as conn:
            policy = self._policy(conn, user_id)
            row = self._story_row(conn, user_id, story_id)
            if row is None or not policy['enabled']:
                return []
            try:
                return self._read_sources(conn, user_id,
                                          self._story_refs(conn, user_id, story_id), policy)
            except (StoryConsentError, StorySourceError):
                return []
