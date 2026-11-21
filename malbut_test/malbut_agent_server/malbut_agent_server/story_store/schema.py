"""Story runtime schema, constants, codecs, and shared transaction adapter."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from typing import Any, Callable, Iterator, Sequence

from malbut_agent_server.conversation_store.database import ConversationDatabase
from malbut_agent_server.story_memory import StoryMemoryError, _json
from malbut_agent_server.story_memory_sources import SourceRef, SourceText, make_source_ref

LEASE_SECONDS = 300
MAX_ATTEMPTS = 3
MAX_SOURCE_CHARS = 48000
MAX_UPDATES = 8
MAX_PENDING = 256
FAILURE_CODES = frozenset({
    'invalid_extraction', 'actor_source_mismatch', 'invalid_previous_evidence',
    'invalid_recall_coverage', 'evidence_mismatch', 'provider_error',
    'story_processing_error', 'source_or_policy_changed', 'checkpoint_changed',
})

# Category words alone cannot identify a story (e.g. two unrelated classes).
# These are used only to scope failed extraction, not to broaden recall.
_TOPIC_CATEGORIES = frozenset({
    '이야기', '기억', '대화', '경험', '상황', '계획', '일정', '예약', '신청',
    '시점', '시간', '장소', '방문', '참석', '관람', '모임', '수업', '결정',
})
_DEICTIC_REFERENCE = re.compile(r'그(?:거|것|때|곳|날|쪽|\s)|이어서|지난번|아까')
_CORRECTION = re.compile(r'정정|수정|바(?:꾸|꿔|꿨|꾼|뀌|뀐|뀔)|변경|취소|아니[야었라]|잘못|사실')

# These standalone requests have a fixed subject and no slot for a new value.
# Do not use the looser foreground intent matcher: speech can contain a
# correction and a question in the same unpunctuated sentence.
_FACT_QUESTION = re.compile(
    r'(?:저장해둔|전에알려준|평소)?(?:내|제|우리|저희)?'
    r'(?:이름|본명|성함|호칭|별명|애칭|닉네임|(?:강아지|고양이|반려견|반려묘)이름|'
    r'가?좋아하는(?:음료|음식|색)|취향)'
    r'(?:이|가|은|는)?(?:뭐(?:야|예요|에요|였지|였죠|였더라|였나요)|무엇(?:인가요|이었지)|기억나(?:요)?)'
    r'[?.!。]?'
)

# A standalone patrol can refer only to the house or the patrol itself.
# A generic "둘러봐" may refer to a topic supplied by another turn.
_PATROL_SCOPE = r'(?:구석구석)?(?:가볍게|간단히|꼼꼼히)?(?:한바퀴|한번)?'
_PATROL_REQUEST = re.compile(
    r'(?:지금은?|이제)?(?:별일없는지)?(?:간단히)?'
    rf'(?:(?:집(?:안)?(?:을)?)?{_PATROL_SCOPE}순찰(?:을)?(?:시작)?해'
    rf'|집(?:안)?(?:을)?{_PATROL_SCOPE}둘러봐)(?:줘|주세요|줄래(?:요)?)?[.!?。]?'
)
# The complement after "말고" must also be a standalone request to stay put.
# Extra words can change a different memory, even if the reply acknowledges a stop.
_PATROL_STOP = re.compile(
    r'(?:(?:지금|오늘)은?|이제는?)?(?:집(?:안)?(?:을)?)?순찰(?:은|을)?'
    r'(?:하지(?:마(?:라|세요|요)?|말아(?:줘요?|주세요|줄래요?)?|말자'
    r'|말고,?(?:지금은?)?(?:그냥)?(?:가만히있어|기다려|대기해)(?:줘요?|주세요|줄래요?)?)'
    r'|(?:그만|취소|중지)해(?:줘요?|주세요|줄래요?)?)[.!?。]?'
)


def _normalized(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold()


def _refs_json(refs: Sequence[SourceRef]) -> str:
    return _json([ref.to_dict() for ref in refs])


def _refs(value: str) -> list[SourceRef]:
    return [SourceRef.from_dict(item) for item in json.loads(value)]


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _identity(row: Any) -> str:
    return _json([row['conversation_id'], row['session_instance_id'],
                  row['generation'], row['turn_id']])


def _message_key(ref: SourceRef) -> str:
    return _json([ref.conversation_id, ref.session_instance_id,
                  ref.generation, ref.turn_id, ref.role])


def _source_object(ref: SourceRef, source: SourceText, identifier: str) -> dict[str, Any]:
    return {'id': identifier, 'ref': ref.to_dict(), 'text': source.text,
            'role': ref.role, 'created_at': source.created_at,
            'completed_at': source.completed_at}


def sync_story_turns(
    conn: sqlite3.Connection,
    user: str,
    story_id: str,
    refs: Sequence[SourceRef],
) -> None:
    """Keep the turn-to-story reverse index synchronized with refs_json."""
    conn.execute(
        'DELETE FROM story_runtime_story_turns WHERE user_id=? AND story_id=?',
        (user, story_id),
    )
    identities = {
        (ref.conversation_id, ref.session_instance_id, ref.generation, ref.turn_id)
        for ref in refs
    }
    if identities:
        conn.executemany(
            'INSERT OR IGNORE INTO story_runtime_story_turns '
            '(user_id,conversation_id,session_instance_id,generation,turn_id,story_id) '
            'VALUES (?,?,?,?,?,?)',
            [(user, cid, sid, gen, tid, story_id) for cid, sid, gen, tid in identities],
        )


def raw_refs(conn: sqlite3.Connection, user: str, row: Any) -> list[SourceRef]:
    """Return non-excluded source references for both speakers of one turn."""
    result: list[SourceRef] = []
    for role, column in (('user', 'user_content'), ('assistant', 'assistant_content')):
        if row[column]:
            ref = make_source_ref(
                conn, user, row['conversation_id'], row['session_instance_id'],
                row['generation'], row['turn_id'], role,
            )
            if conn.execute(
                'SELECT 1 FROM story_runtime_exclusions WHERE user_id=? '
                'AND message_key=? AND sha256=?',
                (user, _message_key(ref), ref.sha256),
            ).fetchone():
                continue
            result.append(ref)
    return result


@dataclass
class _ValidationSnapshot:
    changes: int
    sources: dict[tuple[str, SourceRef], SourceText | None] = field(default_factory=dict)
    fact_guards: dict[str, tuple[bool, list[tuple[str, Any]]]] = field(default_factory=dict)


class StoryDatabase:
    """Shared transaction, clock, and validation-snapshot coordinator."""

    def __init__(
        self,
        db: ConversationDatabase,
        clock: Callable[[], float] = time.time,
        *,
        max_pending_provider: Callable[[], int] = lambda: MAX_PENDING,
    ) -> None:
        self.db = db
        self.clock = clock
        self._max_pending_provider = max_pending_provider
        self.source_cache: _ValidationSnapshot | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        return self.db.connection

    def locked(self) -> Any:
        return self.db.locked()

    def max_pending(self) -> int:
        return int(self._max_pending_provider())

    def now(self) -> float:
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise StoryMemoryError('invalid clock')
        return float(value)

    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self.db.locked():
            conn = self.db.connection
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

    @contextmanager
    def validation_snapshot(self, *, read_only_scope: bool = False) -> Iterator[None]:
        """Reuse source checks only within one locked SQLite snapshot.

        Never retain successful validation across transactions: another process
        can correct or erase source text without touching this Python object.
        Writes on this connection also invalidate the snapshot's memo.
        """
        with self.db.locked():
            conn = self.db.connection
            if conn.in_transaction:
                if read_only_scope and self.source_cache is None:
                    # Pure-read validation inside an outer commit transaction
                    # has no savepoints of its own and still checks total_changes.
                    self.source_cache = _ValidationSnapshot(conn.total_changes)
                    try:
                        yield
                    finally:
                        self.source_cache = None
                    return
                # A caller-owned transaction can roll back a savepoint without
                # changing total_changes. Only reuse a snapshot we opened.
                yield
                return
            with self.transaction() as snapshot_conn:
                self.source_cache = _ValidationSnapshot(snapshot_conn.total_changes)
                try:
                    yield
                finally:
                    self.source_cache = None

    def initialize_schema(self) -> None:
        """Create story runtime tables, indexes, and idempotent migrations."""
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
            '''CREATE TABLE IF NOT EXISTS story_runtime_story_turns (
                user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                session_instance_id TEXT NOT NULL, generation INTEGER NOT NULL,
                turn_id TEXT NOT NULL, story_id TEXT NOT NULL,
                PRIMARY KEY(user_id, conversation_id, session_instance_id,
                            generation, turn_id, story_id))''',
            '''CREATE TABLE IF NOT EXISTS story_runtime_replies (
                user_id TEXT NOT NULL, request_id TEXT NOT NULL,
                stories_json TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY(user_id, request_id))''',
            '''CREATE TABLE IF NOT EXISTS story_runtime_exclusions (
                user_id TEXT NOT NULL, message_key TEXT NOT NULL, sha256 TEXT NOT NULL,
                PRIMARY KEY(user_id, message_key, sha256))''',
            '''CREATE INDEX IF NOT EXISTS story_runtime_jobs_claim_idx
                ON story_runtime_jobs(user_id,state,available_at,sequence)''',
            '''CREATE INDEX IF NOT EXISTS story_runtime_jobs_req_idx
                ON story_runtime_jobs(user_id,request_id,state)''',
            '''CREATE INDEX IF NOT EXISTS story_runtime_story_turns_story_idx
                ON story_runtime_story_turns(user_id,story_id)''',
        )
        with self.transaction(True) as conn:
            for sql in definitions:
                conn.execute(sql)
            if 'batch_json' not in {
                row[1] for row in conn.execute('PRAGMA table_info(story_runtime_jobs)')
            }:
                conn.execute(
                    "ALTER TABLE story_runtime_jobs ADD COLUMN batch_json TEXT NOT NULL DEFAULT '[]'"
                )
            if 'processed' not in {
                row[1] for row in conn.execute('PRAGMA table_info(story_runtime_jobs)')
            }:
                conn.execute(
                    'ALTER TABLE story_runtime_jobs ADD COLUMN processed INTEGER NOT NULL DEFAULT 0'
                )
                conn.execute("UPDATE story_runtime_jobs SET processed=1 WHERE state='done'")
            missing = conn.execute(
                '''SELECT s.user_id, s.story_id, s.refs_json FROM story_runtime_stories s
                   WHERE NOT EXISTS (
                       SELECT 1 FROM story_runtime_story_turns t
                       WHERE t.user_id=s.user_id AND t.story_id=s.story_id
                   )'''
            ).fetchall()
            for row in missing:
                sync_story_turns(conn, row['user_id'], row['story_id'], _refs(row['refs_json']))
