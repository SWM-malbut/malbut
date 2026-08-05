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
        self._revision = 0
        self._initialize()
        self._secure_file_permissions()

    def _initialize(self) -> None:
        with self._lock:
            if self.database_path != ':memory:':
                self._connection.execute('PRAGMA journal_mode=WAL')
            self._connection.execute('PRAGMA foreign_keys=ON')
            self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    metadata_json TEXT NOT NULL
                )
                '''
            )
            self._connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS memories_user_created_idx
                ON memories (user_id, created_at DESC)
                '''
            )
            self._connection.commit()

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
            self._connection.close()

    @property
    def revision(self) -> int:
        """Return a monotonic in-process mutation revision."""
        with self._lock:
            return self._revision

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
        with self._lock:
            self._connection.execute(
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
            self._connection.commit()
            self._revision += 1
            self._secure_file_permissions()
        return record

    def search(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        now: Optional[float] = None,
    ) -> List[MemoryRecord]:
        """Rank active memories by lexical overlap, confidence, and recency."""
        records, _revision = self.search_with_revision(
            user_id,
            query,
            limit=limit,
            now=now,
        )
        return records

    def search_with_revision(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        now: Optional[float] = None,
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
        with self._lock:
            rows = self._connection.execute(
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
            revision = self._revision

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
        with self._lock:
            cursor = self._connection.execute(
                '''
                DELETE FROM memories
                WHERE expires_at IS NOT NULL
                  AND expires_at <= ?
                ''',
                (current_time,),
            )
            self._connection.commit()
            affected = int(cursor.rowcount)
            if affected:
                self._revision += 1
            return affected

    def delete(
        self,
        user_id: str,
        memory_id: str,
    ) -> bool:
        """Delete one memory within its owning user scope."""
        normalized_user = validate_user_id(user_id)
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValidationError('memory_id must not be empty')
        with self._lock:
            cursor = self._connection.execute(
                '''
                DELETE FROM memories
                WHERE id = ? AND user_id = ?
                ''',
                (memory_id.strip(), normalized_user),
            )
            self._connection.commit()
            if cursor.rowcount == 1:
                self._revision += 1
            self._secure_file_permissions()
            return cursor.rowcount == 1

    def list_for_user(
        self,
        user_id: str,
        now: Optional[float] = None,
    ) -> Sequence[MemoryRecord]:
        """List active memories for diagnostics without cross-user access."""
        normalized_user = validate_user_id(user_id)
        current_time = float(now if now is not None else time.time())
        with self._lock:
            rows = self._connection.execute(
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
