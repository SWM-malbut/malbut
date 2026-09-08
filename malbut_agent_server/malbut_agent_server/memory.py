"""SQLite-backed, user-isolated long-term memory retrieval."""

import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from malbut_agent_server.schemas import ValidationError, validate_user_id


MAX_MEMORY_LENGTH = 4000
MAX_RETRIEVAL_CANDIDATES = 5000
TOKEN_PATTERN = re.compile(r'[0-9A-Za-z가-힣_]+')
KOREAN_SUFFIXES = (
    '으로',
    '에서',
    '에게',
    '였다',
    '였지',
    '인지',
    '처럼',
    '부터',
    '까지',
    '은',
    '는',
    '이',
    '가',
    '을',
    '를',
    '에',
    '로',
    '와',
    '과',
    '도',
    '의',
)
STOP_TOKENS = {
    '뭐',
    '뭐였지',
    '어디',
    '어디야',
    '알려줘',
    '우리',
    '기억',
    '해줘',
}


def initialize_memory_schema(connection: sqlite3.Connection) -> None:
    """Initialize additive memory tables without owning the transaction."""
    connection.row_factory = sqlite3.Row
    statements = (
        '''CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            kind TEXT NOT NULL, content TEXT NOT NULL,
            source TEXT NOT NULL, confidence REAL NOT NULL,
            created_at REAL NOT NULL, expires_at REAL,
            metadata_json TEXT NOT NULL
        )''',
        '''CREATE INDEX IF NOT EXISTS memories_user_created_idx
            ON memories (user_id, created_at DESC)''',
        '''CREATE TABLE IF NOT EXISTS memory_policy_state (
            user_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            revision INTEGER NOT NULL DEFAULT 0,
            legacy_cutoff REAL NOT NULL DEFAULT 0,
            consent_source_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL
        )''',
        '''CREATE TABLE IF NOT EXISTS memory_state_counter (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            revision INTEGER NOT NULL DEFAULT 0
        )''',
        '''INSERT OR IGNORE INTO memory_state_counter (singleton, revision)
            VALUES (1, 0)''',
        '''CREATE TABLE IF NOT EXISTS memory_fact_slots (
            user_id TEXT NOT NULL, memory_id TEXT PRIMARY KEY,
            slot_key TEXT NOT NULL, value_key TEXT NOT NULL,
            UNIQUE (user_id, slot_key, value_key),
            FOREIGN KEY (memory_id) REFERENCES memories (id)
                ON DELETE CASCADE
        )''',
        '''CREATE TABLE IF NOT EXISTS memory_tombstones (
            user_id TEXT NOT NULL, memory_id TEXT NOT NULL,
            source_key TEXT NOT NULL, invalidated_at REAL NOT NULL,
            PRIMARY KEY (user_id, memory_id)
        )''',
    )
    for statement in statements:
        connection.execute(statement)


@dataclass(frozen=True)
class MemoryRecord:
    """One persisted memory safe for JSON serialization."""

    id: str
    user_id: str
    kind: str
    content: str
    source: str
    confidence: float
    created_at: float
    expires_at: Optional[float]
    metadata: Dict[str, Any]
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Return the record without internal database details."""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'kind': self.kind,
            'content': self.content,
            'source': self.source,
            'confidence': self.confidence,
            'created_at': self.created_at,
            'expires_at': self.expires_at,
            'metadata': dict(self.metadata),
            'score': round(self.score, 6),
        }


def _normalize_text(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold()


def _token_variants(value: str) -> Set[str]:
    result: Set[str] = set()
    for raw_token in TOKEN_PATTERN.findall(_normalize_text(value)):
        if raw_token in STOP_TOKENS:
            continue
        result.add(raw_token)
        for suffix in KOREAN_SUFFIXES:
            if raw_token.endswith(suffix) and len(raw_token) > len(suffix) + 1:
                result.add(raw_token[:-len(suffix)])
                break
    return result


class SQLiteMemoryStore:
    """Thread-safe SQLite source of truth for verified memories."""

    def __init__(self, database_path: str) -> None:
        """Open a database and create the version-one memory schema."""
        if not database_path:
            raise ValueError('database_path must not be empty')
        self.database_path = database_path
        if database_path != ':memory:':
            Path(database_path).expanduser().parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
        self._connection = sqlite3.connect(
            str(Path(database_path).expanduser())
            if database_path != ':memory:'
            else database_path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._owns_connection = True
        self._initialize()
        self._secure_file_permissions()

    def _initialize(self) -> None:
        with self._lock:
            if self.database_path != ':memory:':
                self._connection.execute('PRAGMA journal_mode=WAL')
            self._connection.execute('PRAGMA foreign_keys=ON')
            self._connection.execute('PRAGMA busy_timeout=5000')
            initialize_memory_schema(self._connection)
            self._connection.commit()

    @contextmanager
    def _transaction(self, connection=None, *, write=False):
        """Use caller transactions unchanged, or own one local transaction."""
        if connection is not None:
            yield connection
            return
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                if write:
                    self._secure_file_permissions()

    def _secure_file_permissions(self) -> None:
        if self.database_path == ':memory:':
            return
        expanded = str(Path(self.database_path).expanduser())
        for suffix in ('', '-wal', '-shm'):
            candidate = expanded + suffix
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            if self._owns_connection:
                self._connection.close()

    def bind_connection(self, connection, lock) -> None:
        """Share a conversation connection, retaining seeded test memories."""
        with self._lock, lock:
            if connection is self._connection:
                self._lock = lock
                return
            target_path = connection.execute(
                'PRAGMA database_list',
            ).fetchone()[2]
            if self.database_path != ':memory:':
                expected = str(
                    Path(self.database_path).expanduser().resolve(),
                )
                if (not target_path
                        or str(Path(target_path).resolve()) != expected):
                    raise ValueError(
                        'memory and conversation databases differ',
                    )
            initialize_memory_schema(connection)
            if self.database_path == ':memory:':
                tables = (
                    'memories', 'memory_policy_state', 'memory_fact_slots',
                    'memory_tombstones',
                )
                for table in tables:
                    rows = self._connection.execute(
                        f'SELECT * FROM {table}',
                    ).fetchall()
                    for row in rows:
                        columns = ', '.join(row.keys())
                        placeholders = ', '.join('?' for _ in row)
                        connection.execute(
                            f'INSERT INTO {table} ({columns}) '
                            f'VALUES ({placeholders})',
                            tuple(row),
                        )
                connection.execute(
                    '''UPDATE memory_state_counter
                        SET revision = revision + ? WHERE singleton = 1''',
                    (self.revision,),
                )
            connection.commit()
            if self._owns_connection:
                self._connection.close()
            self._connection = connection
            self._lock = lock
            self._owns_connection = False

    @property
    def revision(self) -> int:
        """Return a persistent global mutation revision for legacy callers."""
        with self._lock:
            row = self._connection.execute(
                'SELECT revision FROM memory_state_counter WHERE singleton=1',
            ).fetchone()
            return int(row['revision'])

    @staticmethod
    def _bump_revision(connection, user_id, *, cutoff=0.0):
        now = time.time()
        connection.execute(
            '''INSERT INTO memory_policy_state (user_id, updated_at)
                VALUES (?, ?) ON CONFLICT (user_id) DO NOTHING''',
            (user_id, now),
        )
        connection.execute(
            '''UPDATE memory_policy_state
                SET revision = revision + 1,
                    legacy_cutoff = MAX(legacy_cutoff, ?), updated_at = ?
                WHERE user_id = ?''',
            (cutoff, now, user_id),
        )
        connection.execute(
            '''UPDATE memory_state_counter SET revision = revision + 1
                WHERE singleton = 1''',
        )

    def add(
        self,
        user_id: str,
        content: str,
        kind: str = 'fact',
        source: str = 'user_verified',
        confidence: float = 1.0,
        expires_at: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        memory_id: Optional[str] = None,
        created_at: Optional[float] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> MemoryRecord:
        """Persist an explicit memory.

        Arbitrary model output is intentionally not accepted.
        """
        normalized_user = validate_user_id(user_id)
        if not isinstance(content, str) or not content.strip():
            raise ValidationError('memory content must not be empty')
        normalized_content = content.strip()
        if len(normalized_content) > MAX_MEMORY_LENGTH:
            raise ValidationError('memory content is too long')
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 64:
            raise ValidationError('memory kind is invalid')
        if (
            not isinstance(source, str)
            or not source.strip()
            or len(source) > 64
        ):
            raise ValidationError('memory source is invalid')
        if isinstance(confidence, bool) or not isinstance(
            confidence, (int, float)
        ):
            raise ValidationError('memory confidence must be a number')
        confidence = float(confidence)
        if (
            not math.isfinite(confidence)
            or confidence < 0
            or confidence > 1
        ):
            raise ValidationError(
                'memory confidence must be between 0 and 1'
            )
        if expires_at is not None:
            if isinstance(expires_at, bool) or not isinstance(
                expires_at, (int, float)
            ):
                raise ValidationError(
                    'memory expires_at must be a number or null'
                )
            expires_at = float(expires_at)
            if not math.isfinite(expires_at):
                raise ValidationError(
                    'memory expires_at must be finite'
                )
        safe_metadata = {} if metadata is None else metadata
        if not isinstance(safe_metadata, dict):
            raise ValidationError('memory metadata must be an object')
        metadata_json = json.dumps(
            safe_metadata,
            ensure_ascii=False,
            separators=(',', ':'),
        )
        if len(metadata_json) > 8000:
            raise ValidationError('memory metadata is too large')

        normalized_created_at = float(
            created_at if created_at is not None else time.time()
        )
        if not math.isfinite(normalized_created_at):
            raise ValidationError('memory created_at must be finite')
        record = MemoryRecord(
            id=memory_id or str(uuid.uuid4()),
            user_id=normalized_user,
            kind=kind.strip(),
            content=normalized_content,
            source=source.strip(),
            confidence=confidence,
            created_at=normalized_created_at,
            expires_at=expires_at,
            metadata=dict(safe_metadata),
        )
        with self._transaction(connection, write=True) as active:
            active.execute(
                '''
                INSERT INTO memories (
                    id,
                    user_id,
                    kind,
                    content,
                    source,
                    confidence,
                    created_at,
                    expires_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    record.id,
                    record.user_id,
                    record.kind,
                    record.content,
                    record.source,
                    record.confidence,
                    record.created_at,
                    record.expires_at,
                    metadata_json,
                ),
            )
            self._bump_revision(active, normalized_user)
        return record

    def search(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        now: Optional[float] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> List[MemoryRecord]:
        """Rank active memories by lexical overlap, confidence, and recency."""
        records, _revision = self.search_with_revision(
            user_id,
            query,
            limit=limit,
            now=now,
            connection=connection,
        )
        return records

    def search_with_revision(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        now: Optional[float] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> Tuple[List[MemoryRecord], int]:
        """Return ranked active memories and their atomic store revision."""
        normalized_user = validate_user_id(user_id)
        if not isinstance(query, str) or not query.strip():
            raise ValidationError('memory query must not be empty')
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError('memory search limit must be an integer')
        if limit < 1 or limit > 10:
            raise ValidationError(
                'memory search limit must be between 1 and 10'
            )
        current_time = float(now if now is not None else time.time())
        with self._transaction(connection) as active:
            rows = active.execute(
                '''
                SELECT *
                FROM memories
                WHERE user_id = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC
                LIMIT ?
                ''',
                (
                    normalized_user,
                    current_time,
                    MAX_RETRIEVAL_CANDIDATES,
                ),
            ).fetchall()
            revision = int(active.execute(
                'SELECT revision FROM memory_state_counter WHERE singleton=1',
            ).fetchone()['revision'])

        query_normalized = _normalize_text(query)
        query_tokens = _token_variants(query)
        scored: List[MemoryRecord] = []
        for row in rows:
            content = str(row['content'])
            content_normalized = _normalize_text(content)
            content_tokens = _token_variants(content)
            overlap = query_tokens & content_tokens
            score = float(len(overlap) * 4)
            if query_normalized in content_normalized:
                score += 8
            for query_token in query_tokens:
                if len(query_token) < 2:
                    continue
                if any(
                    query_token in content_token
                    or content_token in query_token
                    for content_token in content_tokens
                    if len(content_token) >= 2
                ):
                    score += 1
            if score <= 0:
                continue
            age_days = max(
                0.0,
                (current_time - float(row['created_at'])) / 86400.0,
            )
            recency = math.exp(-age_days / 90.0)
            confidence = float(row['confidence'])
            score += confidence + recency
            metadata = json.loads(str(row['metadata_json']))
            scored.append(
                MemoryRecord(
                    id=str(row['id']),
                    user_id=str(row['user_id']),
                    kind=str(row['kind']),
                    content=content,
                    source=str(row['source']),
                    confidence=confidence,
                    created_at=float(row['created_at']),
                    expires_at=(
                        float(row['expires_at'])
                        if row['expires_at'] is not None
                        else None
                    ),
                    metadata=metadata,
                    score=score,
                )
            )
        scored.sort(
            key=lambda item: (item.score, item.created_at),
            reverse=True,
        )
        return scored[:limit], revision

    def purge_expired(self, now: Optional[float] = None) -> int:
        """Delete expired records and return the affected row count."""
        current_time = float(now if now is not None else time.time())
        with self._transaction(write=True) as active:
            rows = active.execute(
                '''SELECT * FROM memories WHERE expires_at IS NOT NULL
                    AND expires_at <= ?''',
                (current_time,),
            ).fetchall()
            for user_id in {row['user_id'] for row in rows}:
                owned = [row for row in rows if row['user_id'] == user_id]
                cutoff = self._invalidate_rows(active, owned)
                self._bump_revision(active, user_id, cutoff=cutoff)
            return len(rows)

    def delete(
        self,
        user_id: str,
        memory_id: str,
        connection: Optional[sqlite3.Connection] = None,
    ) -> bool:
        """Delete one memory within its owning user scope."""
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValidationError('memory_id must not be empty')
        result = self.remove_facts(
            user_id, [memory_id.strip()], connection=connection,
        )
        return result['status'] == 'deleted'

    def list_for_user(
        self,
        user_id: str,
        now: Optional[float] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> Sequence[MemoryRecord]:
        """List active memories for diagnostics without cross-user access."""
        normalized_user = validate_user_id(user_id)
        current_time = float(now if now is not None else time.time())
        with self._transaction(connection) as active:
            rows = active.execute(
                '''
                SELECT *
                FROM memories
                WHERE user_id = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC
                ''',
                (normalized_user, current_time),
            ).fetchall()
        return [
            MemoryRecord(
                id=str(row['id']),
                user_id=str(row['user_id']),
                kind=str(row['kind']),
                content=str(row['content']),
                source=str(row['source']),
                confidence=float(row['confidence']),
                created_at=float(row['created_at']),
                expires_at=(
                    float(row['expires_at'])
                    if row['expires_at'] is not None
                    else None
                ),
                metadata=json.loads(str(row['metadata_json'])),
            )
            for row in rows
        ]

    def policy_state(self, user_id, connection=None) -> Dict[str, Any]:
        """Read persistent consent and freshness without enabling new users."""
        normalized_user = validate_user_id(user_id)
        with self._transaction(connection) as active:
            row = active.execute(
                'SELECT * FROM memory_policy_state WHERE user_id = ?',
                (normalized_user,),
            ).fetchone()
            if row is None:
                return {'enabled': False, 'revision': 0, 'legacy_cutoff': 0.0}
            return {
                'enabled': bool(row['enabled']),
                'revision': int(row['revision']),
                'legacy_cutoff': float(row['legacy_cutoff']),
            }

    def set_personalization(
        self, user_id, enabled, source, connection=None,
    ) -> Dict[str, Any]:
        """Persist an explicit choice without deleting existing memories."""
        normalized_user = validate_user_id(user_id)
        if type(enabled) is not bool:
            raise ValidationError('personalization enabled must be boolean')
        source_value = self._validate_source(source)
        with self._transaction(connection, write=True) as active:
            state = self.policy_state(normalized_user, connection=active)
            row = active.execute(
                'SELECT consent_source_json FROM memory_policy_state '
                'WHERE user_id = ?',
                (normalized_user,),
            ).fetchone()
            same_source = (
                row is not None
                and json.loads(row['consent_source_json']) == source_value
            )
            if state['enabled'] == enabled and same_source:
                return state
            self._bump_revision(
                active, normalized_user,
                cutoff=0.0 if enabled else time.time(),
            )
            active.execute(
                '''UPDATE memory_policy_state
                    SET enabled = ?, consent_source_json = ?
                    WHERE user_id = ?''',
                (
                    int(enabled),
                    json.dumps(source_value, ensure_ascii=False),
                    normalized_user,
                ),
            )
            return self.policy_state(normalized_user, connection=active)

    @staticmethod
    def _validate_source(source):
        if not isinstance(source, dict):
            raise ValidationError('memory source must be an object')
        result = {}
        for key in (
            'conversation_id', 'session_instance_id', 'turn_id', 'request_id',
        ):
            value = source.get(key)
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > 128):
                raise ValidationError(f'memory source {key} is invalid')
            result[key] = value
        generation = source.get('generation')
        if type(generation) is not int or generation < 1:
            raise ValidationError('memory source generation is invalid')
        text = source.get('text')
        if (not isinstance(text, str) or not text.strip()
                or len(text) > MAX_MEMORY_LENGTH):
            raise ValidationError('memory source text is invalid')
        result.update(generation=generation, text=text)
        return result

    @staticmethod
    def _validate_fact(fact):
        keys = {'kind', 'subject', 'attribute', 'value', 'evidence'}
        if not isinstance(fact, dict) or set(fact) != keys:
            raise ValidationError('memory fact fields are invalid')
        if not isinstance(fact['kind'], str) or fact['kind'] not in {
            'name', 'nickname', 'pet', 'preference',
        }:
            raise ValidationError('memory fact kind is unsupported')
        result = {}
        for key in keys:
            value = fact[key]
            limit = {
                'kind': 64, 'subject': 256, 'attribute': 128,
                'value': 2000, 'evidence': 2000,
            }[key]
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > limit):
                raise ValidationError(f'memory fact {key} is invalid')
            result[key] = value.strip()
        return result

    @staticmethod
    def _fact_key(value):
        return ' '.join(_normalize_text(value).split())

    @staticmethod
    def _fact_content(value):
        subject = (
            '사용자' if value['subject'].casefold() == 'user'
            else value['subject']
        )
        attribute = {
            'name': '이름은', 'nickname': '호칭은', 'preference': '취향은',
            'likes': '좋아하는 것은', 'dislikes': '싫어하는 것은',
        }.get(value['attribute'].casefold(), value['attribute'] + ':')
        return f"{subject} {attribute} {value['value']}"

    @staticmethod
    def _ids(ids):
        if not isinstance(ids, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() or len(item) > 128
            for item in ids
        ):
            raise ValidationError('memory ids must be nonblank strings')
        return list(dict.fromkeys(item.strip() for item in ids))

    @staticmethod
    def _rows_for_ids(connection, user_id, ids):
        if not ids:
            return []
        placeholders = ', '.join('?' for _ in ids)
        return connection.execute(
            f'SELECT * FROM memories WHERE user_id = ? '
            f'AND id IN ({placeholders})', (user_id, *ids),
        ).fetchall()

    @staticmethod
    def _source_key(source):
        return {key: source[key] for key in (
            'conversation_id', 'session_instance_id', 'generation',
            'turn_id', 'request_id',
        )}

    @staticmethod
    def _invalidate_rows(connection, rows):
        now = time.time()
        cutoff = 0.0
        for row in rows:
            metadata = json.loads(row['metadata_json'])
            source = metadata.get('source')
            tracked = connection.execute(
                'SELECT 1 FROM memory_fact_slots WHERE memory_id = ?',
                (row['id'],),
            ).fetchone()
            try:
                if tracked is None:
                    raise ValidationError('legacy memory has no source link')
                validated = SQLiteMemoryStore._validate_source(source)
                source_key = SQLiteMemoryStore._source_key(validated)
            except ValidationError:
                source_key = {}
                cutoff = now
            connection.execute(
                '''INSERT OR IGNORE INTO memory_tombstones
                    (user_id, memory_id, source_key, invalidated_at)
                    VALUES (?, ?, ?, ?)''',
                (row['user_id'], row['id'], json.dumps(source_key), now),
            )
            connection.execute(
                'DELETE FROM memory_fact_slots WHERE memory_id = ?',
                (row['id'],),
            )
            connection.execute(
                'DELETE FROM memories WHERE id = ? AND user_id = ?',
                (row['id'], row['user_id']),
            )
        return cutoff

    def remove_facts(self, user_id, ids, connection=None) -> Dict[str, Any]:
        """Delete owned facts and retain content-free invalidation evidence."""
        normalized_user = validate_user_id(user_id)
        target_ids = self._ids(ids)
        with self._transaction(connection, write=True) as active:
            rows = self._rows_for_ids(active, normalized_user, target_ids)
            if not rows:
                return {'status': 'not_found', 'invalidated_ids': []}
            cutoff = self._invalidate_rows(active, rows)
            self._bump_revision(active, normalized_user, cutoff=cutoff)
            return {
                'status': 'deleted',
                'invalidated_ids': [row['id'] for row in rows],
            }

    def invalidated_ids(self, user_id, connection=None) -> Set[str]:
        """Return only this user's invalidated fact identities."""
        normalized_user = validate_user_id(user_id)
        with self._transaction(connection) as active:
            return {row['memory_id'] for row in active.execute(
                'SELECT memory_id FROM memory_tombstones WHERE user_id = ?',
                (normalized_user,),
            ).fetchall()}

    def upsert_fact(
        self, user_id, fact, source, correct_ids=(), connection=None,
    ) -> Dict[str, Any]:
        """Store direct facts or report conflicts without changing the slot."""
        normalized_user = validate_user_id(user_id)
        value = self._validate_fact(fact)
        origin = self._validate_source(source)
        if value['evidence'] not in origin['text']:
            raise ValidationError('memory evidence is absent from source')
        targets = self._ids(correct_ids)
        slot_parts = [
            self._fact_key(value[key])
            for key in ('kind', 'subject', 'attribute')
        ]
        value_key = self._fact_key(value['value'])
        if value['kind'] == 'preference':
            slot_parts.append(value_key)
        slot = json.dumps(slot_parts, ensure_ascii=False)
        with self._transaction(connection, write=True) as active:
            if not targets and not self.policy_state(
                normalized_user, connection=active,
            )['enabled']:
                raise ValidationError('personalization is disabled')
            source_invalidated = active.execute(
                '''SELECT 1 FROM memory_tombstones
                    WHERE user_id = ? AND source_key = ?''',
                (normalized_user, json.dumps(self._source_key(origin))),
            ).fetchone()
            if source_invalidated is not None:
                return {
                    'status': 'conflict', 'records': [], 'invalidated_ids': [],
                }
            corrected = self._rows_for_ids(active, normalized_user, targets)
            if len(corrected) != len(targets):
                return {
                    'status': 'conflict', 'records': [], 'invalidated_ids': [],
                }
            rows = active.execute(
                '''SELECT m.*, s.value_key FROM memories m
                    JOIN memory_fact_slots s ON s.memory_id = m.id
                    WHERE s.user_id = ? AND s.slot_key = ?''',
                (normalized_user, slot),
            ).fetchall()
            conflicts = [row for row in rows if (
                row['value_key'] != value_key and row['id'] not in targets
            )]
            if conflicts:
                return {
                    'status': 'conflict',
                    'records': [self._record(row) for row in conflicts],
                    'invalidated_ids': [],
                }
            identical = next((row for row in rows if (
                row['value_key'] == value_key
            )), None)
            removed = [row for row in corrected if (
                identical is None or row['id'] != identical['id']
            )]
            if identical is not None and not removed:
                return {
                    'status': 'unchanged',
                    'records': [self._record(identical)],
                    'invalidated_ids': [],
                }
            cutoff = self._invalidate_rows(active, removed)
            if identical is not None:
                self._bump_revision(active, normalized_user, cutoff=cutoff)
                record = self._record(identical)
            else:
                version = 1 + max((
                    int(json.loads(row['metadata_json']).get('version', 0))
                    for row in corrected
                ), default=0)
                record = self.add(
                    normalized_user,
                    self._fact_content(value),
                    kind=value['kind'], source='user_statement',
                    metadata={
                        'fact': value, 'source': origin, 'version': version,
                    },
                    connection=active,
                )
                active.execute(
                    '''INSERT INTO memory_fact_slots
                        (user_id, memory_id, slot_key, value_key)
                        VALUES (?, ?, ?, ?)''',
                    (normalized_user, record.id, slot, value_key),
                )
                if cutoff:
                    active.execute(
                        '''UPDATE memory_policy_state
                            SET legacy_cutoff = MAX(legacy_cutoff, ?)
                            WHERE user_id = ?''',
                        (cutoff, normalized_user),
                    )
            return {
                'status': 'stored', 'records': [record],
                'invalidated_ids': [row['id'] for row in removed],
            }

    @staticmethod
    def _record(row) -> MemoryRecord:
        return MemoryRecord(
            id=str(row['id']), user_id=str(row['user_id']),
            kind=str(row['kind']), content=str(row['content']),
            source=str(row['source']), confidence=float(row['confidence']),
            created_at=float(row['created_at']),
            expires_at=(float(row['expires_at'])
                        if row['expires_at'] is not None else None),
            metadata=json.loads(str(row['metadata_json'])),
        )
