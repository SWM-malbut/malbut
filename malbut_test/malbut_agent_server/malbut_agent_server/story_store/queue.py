"""Story extraction job queue, checkpoint commits, recovery, and retries."""

from dataclasses import replace
import json
import secrets
import sqlite3
from typing import Any, Sequence

from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.story_memory import (
    StoryEntry,
    StoryMemoryError,
    _entries,
    _id,
    _json,
    _text,
)
from malbut_agent_server.story_memory_sources import SourceRef, SourceText
from malbut_agent_server.story_store.policy import StoryPolicyRepository
from malbut_agent_server.story_store.queries import StoryQueryRepository
from malbut_agent_server.story_store.schema import (
    FAILURE_CODES,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    MAX_SOURCE_CHARS,
    MAX_UPDATES,
    StoryDatabase,
    _digest,
    _identity,
    _message_key,
    _refs,
    _refs_json,
    _source_object,
    raw_refs,
    sync_story_turns,
)


class StoryQueueRepository:
    """Extraction job lifecycle: enqueue, claim, commit, release, fail, recover."""

    def __init__(
        self,
        db: StoryDatabase,
        policy_repo: StoryPolicyRepository,
        query_repo: StoryQueryRepository,
    ) -> None:
        self._db = db
        self._policy = policy_repo
        self._queries = query_repo
        self._recovery_versions: dict[str, tuple[int, int]] = {}

    def grant(
        self,
        conn: sqlite3.Connection,
        user: str,
        row: Any,
        policy: dict[str, Any],
    ) -> bool:
        existing = conn.execute(
            'SELECT * FROM story_runtime_scope WHERE user_id=? AND request_id=?',
            (user, row['request_id']),
        ).fetchone()
        if existing:
            return bool(existing['identity_json'] == _identity(row))
        # rowid may be reused after a session deletion. Capture identities at
        # window boundaries rather than treating rowid as a durable sequence.
        identity = _identity(row)
        eligible = any(
            identity not in window.get('existing', [])
            and row['created_at'] >= window['opened_at']
            and (window['closed_at'] is None
                 or identity in window.get('completed', []))
            for window in json.loads(policy['windows_json'])
        )
        if eligible:
            conn.execute(
                'INSERT INTO story_runtime_scope VALUES (?,?,?,?)',
                (user, row['request_id'], _identity(row), policy['revision']),
            )
        return eligible

    def enqueue(
        self,
        conn: sqlite3.Connection,
        user: str,
        request_id: str,
        *,
        policy: dict[str, Any] | None = None,
    ) -> bool:
        if policy is None:
            policy = self._policy.read_policy(conn, user)
        if not policy['enabled'] or not policy['external_consent']:
            return False
        row = conn.execute(
            "SELECT rowid,* FROM conversation_turns WHERE user_id=? "
            "AND request_id=? AND status='completed'",
            (user, request_id),
        ).fetchone()
        if row is None or not self.grant(conn, user, row, policy):
            return False
        refs = raw_refs(conn, user, row)
        if not refs:
            return False
        digest = _digest([ref.key for ref in refs])
        existing = conn.execute(
            'SELECT * FROM story_runtime_jobs WHERE user_id=? '
            'AND request_id=? AND source_digest=?',
            (user, request_id, digest),
        ).fetchone()
        # A finished job cannot be queued or cancelled again. Check for other
        # source versions that would still need cancellation on revocation.
        if (existing and existing['state'] == 'done' and not conn.execute(
                "SELECT 1 FROM story_runtime_jobs WHERE user_id=? AND request_id=? "
                "AND state IN ('queued','running','failed') LIMIT 1",
                (user, request_id),
        ).fetchone()):
            return False
        if any(self._queries.resolve(conn, user, ref) is None for ref in refs):
            # A fact tombstone revokes retained raw text without erasing it.
            # Do not recreate a cancelled extraction on each recovery pass.
            # Release the rest of an in-flight batch so valid members can be
            # claimed again without waiting for the old lease to expire.
            claims = [
                item[0] for item in conn.execute(
                    "SELECT claim FROM story_runtime_jobs WHERE user_id=? "
                    "AND request_id=? AND state='running' AND claim IS NOT NULL",
                    (user, request_id),
                )
            ]
            for claim in claims:
                conn.execute(
                    "UPDATE story_runtime_jobs SET state='cancelled',claim=NULL, "
                    "lease_until=NULL,read_sources_json='[]',error_code='source_revoked' "
                    "WHERE user_id=? AND state='running' AND claim=?",
                    (user, claim),
                )
            conn.execute(
                "UPDATE story_runtime_jobs SET state='cancelled',claim=NULL, "
                "lease_until=NULL,read_sources_json='[]',error_code='source_revoked' "
                "WHERE user_id=? AND request_id=? AND state IN ('queued','running','failed')",
                (user, request_id),
            )
            return False
        if existing:
            scope = conn.execute(
                'SELECT grant_revision FROM story_runtime_scope WHERE user_id=? '
                'AND request_id=?',
                (user, request_id),
            ).fetchone()
            if existing['state'] == 'cancelled' and scope[0] == policy['revision']:
                conn.execute(
                    "UPDATE story_runtime_jobs SET state='queued',policy_revision=?, "
                    "attempts=0,available_at=? WHERE sequence=?",
                    (policy['revision'], self._db.now(), existing['sequence']),
                )
                return True
            return False
        count = conn.execute(
            "SELECT COUNT(*) FROM story_runtime_jobs WHERE user_id=? "
            "AND state IN ('queued','running')",
            (user,),
        ).fetchone()[0]
        if count >= self._db.max_pending():
            return False
        now = self._db.now()
        job_id = _digest([user, request_id, digest])
        conn.execute(
            '''INSERT INTO story_runtime_jobs
            (user_id,job_id,request_id,source_digest,sources_json,state,available_at,
             policy_revision,created_at) VALUES (?,?,?,?,?,'queued',?,?,?)''',
            (user, job_id, request_id, digest, _refs_json(refs), now, policy['revision'], now),
        )
        self._policy.job_trace(
            conn, 'story_queued', user, request_id,
            {'job_id': job_id, 'state': 'queued'},
        )
        return True

    def checkpoint_source_read(
        self,
        conn: sqlite3.Connection,
        user: str,
        request_id: str,
        revision: int,
        readset: Sequence[tuple[str, Sequence[str]]],
        version: int,
    ) -> None:
        """Finish one server-certified read in the same transaction as its reply."""
        if conn is not self._db.connection or not conn.in_transaction or len(readset) != 1:
            raise StoryMemoryError('invalid source-read transaction')
        identifier, hashes = readset[0]
        policy = self._policy.read_policy(conn, user)
        turn = conn.execute(
            "SELECT rowid,* FROM conversation_turns WHERE user_id=? AND request_id=? "
            "AND status='completed'",
            (user, request_id),
        ).fetchone()
        story = conn.execute(
            'SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?',
            (user, identifier),
        ).fetchone()
        lineage = conn.execute(
            'SELECT stories_json,revision FROM story_runtime_replies WHERE user_id=? AND request_id=?',
            (user, request_id),
        ).fetchone()
        if (not turn or not story or not story['active'] or not lineage or lineage['revision'] != revision
                or identifier not in json.loads(lineage['stories_json'])
                or story['version'] != version or not policy['enabled']
                or not policy['external_consent'] or policy['revision'] != revision
                or not self.grant(conn, user, turn, policy)
                or not self._queries.story_valid(conn, user, story)):
            raise ValidationError('memory_changed')
        current = json.loads(story['current_json'])
        entry_hashes = [
            _digest({key: entry[key] for key in ('text', 'kind', 'actor', 'status')})
            for entry in current
        ]
        if len(current) != 1 or tuple(hashes) != tuple(entry_hashes):
            raise ValidationError('memory_changed')
        source_keys = set(current[0]['source_keys'])
        original = [ref for ref in _refs(story['refs_json']) if ref.key in source_keys]
        refs = raw_refs(conn, user, turn)
        if not original or len(original) != len(source_keys) or any(ref.role != 'user' for ref in original):
            # Legacy entries may cite both speakers. They keep ordinary extraction.
            return
        if ({ref.role for ref in refs} != {'user', 'assistant'}
                or any(self._queries.resolve(conn, user, ref) is None for ref in refs)):
            raise ValidationError('memory_changed')
        digest = _digest([ref.key for ref in refs])
        job_id = _digest([user, request_id, digest])
        # Link the pure question to the story's deletion scope. Its answer is
        # already erased via story_runtime_replies and must never become evidence.
        coverage = {ref.key: ref for ref in _refs(story['refs_json'])}
        coverage.update((ref.key, ref) for ref in refs if ref.role == 'user')
        updated_refs = list(coverage.values())
        conn.execute(
            'UPDATE story_runtime_stories SET refs_json=? WHERE user_id=? AND story_id=?',
            (_refs_json(updated_refs), user, identifier),
        )
        sync_story_turns(conn, user, identifier, updated_refs)
        read_sources = [{'id': 'p' + str(i), 'ref': ref.to_dict()} for i, ref in enumerate(original, 1)]
        now = self._db.now()
        conn.execute(
            """INSERT INTO story_runtime_jobs
            (user_id,job_id,request_id,source_digest,sources_json,read_sources_json,state,processed,
             available_at,policy_revision,result_digest,created_at) VALUES (?,?,?,?,?,?,'done',1,?,?,?,?)""",
            (user, job_id, request_id, digest, _refs_json(refs), _json(read_sources),
             now, revision, _digest([]), now),
        )
        self._policy.job_trace(
            conn, 'story_readback_covered', user, request_id,
            {'job_id': job_id, 'story_id': identifier, 'entry_hashes': list(hashes),
             'source_refs': [ref.to_dict() for ref in original], 'policy_revision': revision,
             'story_version': version},
        )

    def enqueue_completed(self, user: str, request_id: str) -> bool:
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        with self._db.transaction(True) as conn:
            return self.enqueue(conn, user, request_id)

    @staticmethod
    def recovery_candidates(
        conn: sqlite3.Connection, user: str, earliest: float | None,
    ) -> list[Any]:
        return conn.execute(
            """SELECT t.request_id FROM conversation_turns t WHERE t.user_id=?
               AND t.status='completed' AND (t.created_at>=? OR EXISTS
               (SELECT 1 FROM story_runtime_scope s WHERE s.user_id=t.user_id
               AND s.request_id=t.request_id))
               AND (NOT EXISTS (SELECT 1 FROM story_runtime_jobs j
                                WHERE j.user_id=t.user_id AND j.request_id=t.request_id
                                AND j.state='done')
                    OR EXISTS (SELECT 1 FROM story_runtime_jobs j
                               WHERE j.user_id=t.user_id AND j.request_id=t.request_id
                               AND j.state IN ('queued','running','failed')))
               ORDER BY t.completed_at,t.rowid""",
            (user, earliest),
        ).fetchall()

    def recover(self, user: str) -> int:
        user = _id(user, 'user_id')
        with self._db.locked():
            now = self._db.now()
            with self._db.transaction(False) as conn:
                p = self._policy.read_policy(conn, user)
                if not p['enabled'] or not p['external_consent']:
                    return 0
                has_expired = conn.execute(
                    "SELECT 1 FROM story_runtime_jobs WHERE user_id=? "
                    "AND state='running' AND lease_until<=? LIMIT 1",
                    (user, now),
                ).fetchone() is not None
                # This is the persistent shared connection: total_changes covers
                # our own writers and data_version covers other connections. Keep
                # checking timed leases above, but do not acquire a write lock or
                # revisit historical sources when nothing changed.
                version = (conn.total_changes, int(conn.execute('PRAGMA data_version').fetchone()[0]))
                if not has_expired and self._recovery_versions.get(user) == version:
                    return 0
                earliest = min((w['opened_at'] for w in json.loads(p['windows_json'])), default=None)
                candidates = self.recovery_candidates(conn, user, earliest)
                if not has_expired and not candidates:
                    self._recovery_versions[user] = version
                    return 0
            with self._db.transaction(True) as conn:
                p = self._policy.read_policy(conn, user)
                if not p['enabled'] or not p['external_consent']:
                    return 0
                now = self._db.now()
                conn.execute(
                    "UPDATE story_runtime_jobs SET state=CASE WHEN attempts>=? THEN 'failed' "
                    "ELSE 'queued' END,claim=NULL,lease_until=NULL,available_at=? "
                    "WHERE user_id=? AND state='running' AND lease_until<=?",
                    (MAX_ATTEMPTS, now, user, now),
                )
                earliest = min((w['opened_at'] for w in json.loads(p['windows_json'])), default=None)
                rows = self.recovery_candidates(conn, user, earliest)
                queued = sum(self.enqueue(conn, user, row[0], policy=p) for row in rows)
                version = (conn.total_changes, int(conn.execute('PRAGMA data_version').fetchone()[0]))
            # Publish the checkpoint only after the transaction commits. A failed
            # commit must not hide a completion from the next recovery attempt.
            self._recovery_versions[user] = version
            return queued

    def pending_users(self) -> list[str]:
        with self._db.transaction() as conn:
            return [
                row[0] for row in conn.execute(
                    'SELECT user_id FROM story_runtime_policy '
                    'WHERE enabled=1 AND external_consent=1'
                )
            ]

    def claim(self, user: str, batch_size: int = 1) -> dict[str, Any] | None:
        user = _id(user, 'user_id')
        if type(batch_size) is not int or not 1 <= batch_size <= 3:
            raise StoryMemoryError('batch_size must be between 1 and 3')
        with self._db.transaction(True) as conn:
            p = self._policy.read_policy(conn, user)
            if not p['enabled'] or not p['external_consent']:
                return None
            conn.execute(
                "UPDATE story_runtime_jobs SET state=CASE WHEN attempts>=? THEN 'failed' "
                "ELSE 'queued' END,claim=NULL,lease_until=NULL,available_at=? "
                "WHERE user_id=? AND state='running' AND lease_until<=?",
                (MAX_ATTEMPTS, self._db.now(), user, self._db.now()),
            )
            if conn.execute(
                "SELECT 1 FROM story_runtime_jobs WHERE user_id=? AND state='running' "
                "AND lease_until>?",
                (user, self._db.now()),
            ).fetchone():
                return None
            queued = conn.execute(
                "SELECT * FROM story_runtime_jobs WHERE user_id=? AND state='queued' "
                "AND available_at<=? ORDER BY sequence LIMIT ?",
                (user, self._db.now(), batch_size),
            ).fetchall()
            if not queued:
                return None
            row = queued[0]
            if row['policy_revision'] != p['revision']:
                conn.execute(
                    "UPDATE story_runtime_jobs SET state='cancelled' WHERE sequence=?",
                    (row['sequence'],),
                )
                return None
            sources: list[dict[str, Any]] = []
            batch: list[Any] = []
            for candidate in queued:
                if candidate['policy_revision'] != p['revision']:
                    break
                additions: list[tuple[SourceRef, SourceText]] | None = []
                for ref in _refs(candidate['sources_json']):
                    source = self._queries.resolve(conn, user, ref)
                    if source is None:
                        conn.execute(
                            "UPDATE story_runtime_jobs SET state='cancelled',error_code='source_changed' "
                            "WHERE sequence=?",
                            (candidate['sequence'],),
                        )
                        additions = None
                        break
                    assert additions is not None
                    additions.append((ref, source))
                if additions is None:
                    if not batch:
                        return None
                    break
                if batch and (
                    sum(len(item['text']) for item in sources)
                    + sum(len(src.text) for _, src in additions) > 32000
                ):
                    break
                for ref, source in additions:
                    sources.append(_source_object(ref, source, 's' + str(len(sources) + 1)))
                batch.append(candidate)
            if sum(len(item['text']) for item in sources) > MAX_SOURCE_CHARS:
                conn.execute(
                    "UPDATE story_runtime_jobs SET state='failed',error_code='source_too_large' "
                    "WHERE sequence=?",
                    (row['sequence'],),
                )
                self._policy.job_trace(
                    conn, 'story_extraction_failed', user, row['request_id'],
                    {'job_id': row['job_id'], 'state': 'failed',
                     'error_code': 'source_too_large', 'retry_scheduled': False,
                     'attempt': row['attempts']},
                )
                return None
            previous = self._queries.candidate_rows(
                conn, user, ' '.join(item['text'] for item in sources),
                5, extraction=True,
            )
            stories: list[dict[str, Any]] = []
            source_ids = {SourceRef.from_dict(item['ref']).key: item['id'] for item in sources}
            total = sum(len(item['text']) for item in sources)
            for prior in previous:
                story = self._queries.story_dict(prior)
                keys = {key for entry in story['current'] for key in entry['source_keys']}
                prior_additions: list[tuple[SourceRef, SourceText]] = []
                for ref in _refs(prior['refs_json']):
                    if ref.key in keys and ref.key not in source_ids:
                        resolved = self._queries.resolve(conn, user, ref)
                        assert resolved is not None
                        prior_additions.append((ref, resolved))
                if total + sum(len(src.text) for _, src in prior_additions) > MAX_SOURCE_CHARS:
                    continue
                for ref, resolved in prior_additions:
                    identifier = 'p' + str(len(source_ids) + 1)
                    sources.append(_source_object(ref, resolved, identifier))
                    source_ids[ref.key] = identifier
                    total += len(resolved.text)
                for entry in story['current']:
                    entry['evidence'] = [
                        {
                            'source_id': source_ids[key],
                            'quote': next(s['text'] for s in sources if s['id'] == source_ids[key]),
                        }
                        for key in entry.pop('source_keys')
                    ]
                story.pop('episodes')
                stories.append(story)
            claim = secrets.token_hex(20)
            batch_ids = [member['job_id'] for member in batch]
            for member in batch:
                conn.execute(
                    "UPDATE story_runtime_jobs SET state='running',attempts=attempts+1,claim=?, "
                    "lease_until=?,expected_version=?,read_sources_json=?,batch_json=? WHERE sequence=?",
                    (claim, self._db.now() + LEASE_SECONDS, p['data_revision'],
                     _json([{'id': item['id'], 'ref': item['ref']} for item in sources]),
                     _json(batch_ids), member['sequence']),
                )
                self._policy.job_trace(
                    conn, 'story_extraction_started', user, member['request_id'],
                    {'job_id': member['job_id'], 'state': 'running',
                     'attempt': member['attempts'] + 1,
                     'batch_job_ids': batch_ids},
                )
            return {
                'user_id': user,
                'job_id': row['job_id'],
                'claim': claim,
                'request_id': row['request_id'],
                'policy_revision': p['revision'],
                'expected_version': p['data_revision'],
                'sources': sources,
                'stories': stories,
                'batch_job_ids': batch_ids,
            }

    def job_row(
        self, conn: sqlite3.Connection, job: dict[str, Any], allow_done: bool = False,
    ) -> Any:
        user_id = job.get('user_id')
        if not isinstance(user_id, str):
            return None
        row = conn.execute(
            'SELECT * FROM story_runtime_jobs WHERE user_id=? AND job_id=?',
            (user_id, job.get('job_id')),
        ).fetchone()
        p = self._policy.read_policy(conn, user_id)
        if (row is None or not p['enabled'] or not p['external_consent']
                or row['policy_revision'] != p['revision']
                or job.get('policy_revision') != row['policy_revision']):
            return None
        if allow_done and row['state'] == 'done':
            for identifier in json.loads(row['batch_json']):
                member = conn.execute(
                    'SELECT state,result_digest FROM story_runtime_jobs WHERE user_id=? AND job_id=?',
                    (row['user_id'], identifier),
                ).fetchone()
                if member is None or member['state'] != 'done' or member['result_digest'] != row['result_digest']:
                    return None
            return row
        if (row['state'] != 'running' or row['claim'] != job.get('claim')
                or row['lease_until'] <= self._db.now() or p['data_revision'] != row['expected_version']
                or job.get('expected_version') != row['expected_version']):
            return None
        for identifier in json.loads(row['batch_json']):
            member = conn.execute(
                'SELECT * FROM story_runtime_jobs WHERE user_id=? AND job_id=?',
                (row['user_id'], identifier),
            ).fetchone()
            if (member is None or member['state'] != 'running' or member['claim'] != row['claim']
                    or member['policy_revision'] != row['policy_revision']
                    or member['expected_version'] != row['expected_version']):
                return None
        return row

    def job_sources(self, conn: sqlite3.Connection, row: Any) -> list[dict[str, Any]] | None:
        result: list[dict[str, Any]] = []
        for item in json.loads(row['read_sources_json']):
            ref = SourceRef.from_dict(item['ref'])
            source = self._queries.resolve(conn, row['user_id'], ref)
            if source is None:
                return None
            result.append(_source_object(ref, source, item['id']))
        return result

    def job_source(self, job: dict[str, Any]) -> list[dict[str, Any]] | None:
        with self._db.transaction() as conn:
            row = self.job_row(conn, job)
            return self.job_sources(conn, row) if row else None

    @staticmethod
    def quote_ref(evidence: Any, sources: dict[str, dict[str, Any]]) -> SourceRef:
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

    def prepare_update(
        self, update: Any, sources: dict[str, dict[str, Any]],
    ) -> tuple[str | None, str, list[str], list[dict[str, Any]], list[dict[str, Any]], list[SourceRef]]:
        if not isinstance(update, dict):
            raise StoryMemoryError('invalid story update')
        story_id = update.get('story_id')
        if story_id is not None:
            story_id = _id(story_id, 'story_id')
        title = _text(update.get('title'), 'title', 512)
        raw_aliases = update.get('aliases', [])
        if not isinstance(raw_aliases, list) or len(raw_aliases) > 32:
            raise StoryMemoryError('invalid aliases')
        aliases = [_text(alias, 'alias', 256) for alias in raw_aliases]
        used: dict[Any, SourceRef] = {}

        def entries(values: Any) -> list[dict[str, Any]]:
            if not isinstance(values, list) or len(values) > 64:
                raise StoryMemoryError('invalid entries')
            result: list[dict[str, Any]] = []
            for item in values:
                if not isinstance(item, dict):
                    raise StoryMemoryError('invalid entry')
                evidence = item.get('evidence')
                if evidence is None and isinstance(item.get('source_ids'), list):
                    if any(key not in sources for key in item['source_ids']):
                        raise StoryMemoryError('unknown evidence source')
                    evidence = [
                        {'source_id': key, 'quote': sources[key]['text']}
                        for key in item['source_ids']
                    ]
                if not isinstance(evidence, list) or not 1 <= len(evidence) <= 64:
                    raise StoryMemoryError('entry requires evidence')
                refs = [self.quote_ref(part, sources) for part in evidence]
                used.update((ref.key, ref) for ref in refs)
                entry = StoryEntry(
                    item.get('text'),  # type: ignore[arg-type]
                    item.get('kind'),  # type: ignore[arg-type]
                    item.get('actor'),  # type: ignore[arg-type]
                    item.get('status'),  # type: ignore[arg-type]
                    tuple(ref.key for ref in refs),
                )
                checked = _entries([entry], refs)[0]
                result.append({
                    'text': checked.text,
                    'kind': checked.kind,
                    'actor': checked.actor,
                    'status': checked.status,
                    'source_keys': list(checked.source_keys),
                })
            return result

        current = entries(update.get('current'))
        episode = entries(update.get('episode', []))
        if not current:
            raise StoryMemoryError('current state must have evidence')
        spans = update.get('source_spans', [])
        if not isinstance(spans, list) or len(spans) > 128:
            raise StoryMemoryError('invalid source spans')
        for span in spans:
            ref = self.quote_ref(span, sources)
            used[ref.key] = ref
        return story_id, title, aliases, current, episode, list(used.values())

    def commit(self, job: dict[str, Any], updates: Any) -> bool:
        if not isinstance(updates, list) or len(updates) > MAX_UPDATES or len(_json(updates)) > 200000:
            raise StoryMemoryError('invalid update batch')
        result_digest = _digest(updates)
        with self._db.transaction(True) as conn:
            row = self.job_row(conn, job, allow_done=True)
            if row is None:
                return False
            source_list = self.job_sources(conn, row)
            if source_list is None:
                return False
            if row['state'] == 'done':
                if row['result_digest'] != result_digest:
                    raise StoryMemoryError('completed checkpoint payload differs')
                return True
            sources = {item['id']: item for item in source_list}
            prepared = [self.prepare_update(update, sources) for update in updates]
            requested = [item[0] for item in prepared if item[0] is not None]
            if len(requested) != len(set(requested)):
                raise StoryMemoryError('one update per story per checkpoint')
            now = self._db.now()
            source_time = max(
                (item['completed_at'] for item in source_list if item['id'].startswith('s')),
                default=now,
            )
            saved_stories: list[dict[str, Any]] = []
            for update, (story_id, title, aliases, current, episode, refs) in zip(updates, prepared):
                prior = None
                if story_id:
                    prior = conn.execute(
                        'SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                        (row['user_id'], story_id),
                    ).fetchone()
                    if prior is None or not self._queries.story_valid(conn, row['user_id'], prior):
                        raise StoryMemoryError('unknown or invalid existing story')
                previous_keys = {ref.key for ref in _refs(prior['refs_json'])} if prior else set()
                used_ids = {
                    part['source_id']
                    for entry in [*update.get('current', []), *update.get('episode', [])]
                    for part in entry.get('evidence', [])
                }
                used_ids.update(
                    key
                    for entry in [*update.get('current', []), *update.get('episode', [])]
                    for key in entry.get('source_ids', [])
                )
                used_ids.update(part['source_id'] for part in update.get('source_spans', []))
                for identifier in used_ids:
                    if (identifier.startswith('p')
                            and SourceRef.from_dict(sources[identifier]['ref']).key not in previous_keys):
                        raise StoryMemoryError('evidence belongs to a different story')
                if not any(identifier.startswith('s') for identifier in used_ids) and used_ids:
                    raise StoryMemoryError('update must cite this checkpoint source')
                story_id = story_id or 'story-' + secrets.token_hex(12)
                prior_refs = _refs(prior['refs_json']) if prior else []
                combined = {ref.key: ref for ref in [*prior_refs, *refs]}
                episodes = json.loads(prior['episodes_json']) if prior else []
                # Existing p* evidence may be newer than a historical batch. It
                # must not make that old batch appear to be a new decision.
                cited_new_sources = [
                    source for identifier, source in sources.items()
                    if identifier.startswith('s') and any(
                        _message_key(ref) == _message_key(SourceRef.from_dict(source['ref']))
                        and ref.sha256 == source['ref']['sha256']
                        and source['ref']['start'] <= ref.start < ref.end <= source['ref']['end']
                        for ref in refs
                    )
                ]
                story_source_time = max(
                    (source['completed_at'] for source in cited_new_sources),
                    default=source_time,
                )
                if (prior and not episode and title == prior['title']
                        and aliases == json.loads(prior['aliases_json'])
                        and current == json.loads(prior['current_json'])):
                    # A recall-only exchange adds deletion provenance without
                    # making an old fact newer than a later historical update.
                    story_source_time = prior['source_time']
                episodes.append({
                    'job_id': row['job_id'],
                    'source_time': story_source_time,
                    'created_at': now,
                    'entries': episode,
                })
                if prior and story_source_time < prior['source_time']:
                    title, aliases, current = (
                        prior['title'],
                        json.loads(prior['aliases_json']),
                        json.loads(prior['current_json']),
                    )
                version = prior['version'] + 1 if prior else 1
                combined_refs = list(combined.values())
                conn.execute(
                    '''INSERT INTO story_runtime_stories
                    (user_id,story_id,title,aliases_json,current_json,episodes_json,refs_json,
                     version,updated_at,source_time,active) VALUES (?,?,?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(user_id,story_id) DO UPDATE SET title=excluded.title,
                    aliases_json=excluded.aliases_json,current_json=excluded.current_json,
                    episodes_json=excluded.episodes_json,refs_json=excluded.refs_json,
                    version=excluded.version,updated_at=excluded.updated_at,source_time=excluded.source_time,active=1''',
                    (row['user_id'], story_id, title, _json(aliases), _json(current),
                     _json(episodes), _refs_json(combined_refs), version, now,
                     max(story_source_time, prior['source_time'] if prior else story_source_time)),
                )
                sync_story_turns(conn, row['user_id'], story_id, combined_refs)
                saved_stories.append({'story_id': story_id, 'version': version})
            if updates:
                conn.execute(
                    'UPDATE story_runtime_policy SET data_revision=data_revision+1 WHERE user_id=?',
                    (row['user_id'],),
                )
            members = conn.execute(
                "SELECT request_id FROM story_runtime_jobs WHERE user_id=? AND state='running' AND claim=?",
                (row['user_id'], row['claim']),
            ).fetchall()
            conn.execute(
                "UPDATE story_runtime_jobs SET state='done',processed=1,claim=NULL,lease_until=NULL,"
                "error_code=NULL,result_digest=? WHERE user_id=? AND state='running' AND claim=?",
                (result_digest, row['user_id'], row['claim']),
            )
            # One checkpoint can include several requests. Keep the exact source
            # request IDs instead of attributing every update to the first turn.
            source_refs = [item['ref'] for item in source_list if item['id'].startswith('s')]
            for member in members:
                self._policy.job_trace(
                    conn, 'story_committed' if saved_stories else 'story_no_change',
                    row['user_id'], member['request_id'],
                    {'stories': saved_stories, 'job_id': row['job_id'],
                     'source_refs': source_refs,
                     'source_request_ids': [item['request_id'] for item in members]},
                )
            return True

    def release_claim(self, job: dict[str, Any]) -> int:
        """Return only a stopped worker's matching claim, without spending a retry."""
        with self._db.transaction(True) as conn:
            return conn.execute(
                "UPDATE story_runtime_jobs SET state='queued',attempts=MAX(attempts-1,0), "
                "claim=NULL,lease_until=NULL,available_at=?,expected_version=NULL, "
                "read_sources_json='[]',batch_json='[]' "
                "WHERE user_id=? AND state='running' AND claim=?",
                (self._db.now(), job['user_id'], job['claim']),
            ).rowcount

    def fail(self, job: dict[str, Any], error: Any) -> None:
        with self._db.transaction(True) as conn:
            row = conn.execute(
                'SELECT * FROM story_runtime_jobs WHERE user_id=? AND job_id=? '
                "AND state='running' AND claim=?",
                (job.get('user_id'), job.get('job_id'), job.get('claim')),
            ).fetchone()
            if row is None:
                return
            code = (
                type(error).__name__ if isinstance(error, Exception)
                else error if isinstance(error, str) and error in FAILURE_CODES
                else 'processing_failed'
            )
            for member in conn.execute(
                "SELECT * FROM story_runtime_jobs WHERE user_id=? AND state='running' AND claim=?",
                (row['user_id'], row['claim']),
            ).fetchall():
                retry = member['attempts'] < MAX_ATTEMPTS
                conn.execute(
                    'UPDATE story_runtime_jobs SET state=?,claim=NULL,lease_until=NULL, '
                    'available_at=?,error_code=? WHERE sequence=?',
                    ('queued' if retry else 'failed',
                     self._db.now() + 2 ** member['attempts'], code, member['sequence']),
                )
                self._policy.job_trace(
                    conn, 'story_extraction_failed', row['user_id'], member['request_id'],
                    {'job_id': member['job_id'], 'state': 'queued' if retry else 'failed',
                     'attempt': member['attempts'], 'error_code': code,
                     'retry_scheduled': retry,
                     'retry_after_seconds': 2 ** member['attempts'] if retry else None},
                )

    def stats(self, user: str) -> dict[str, Any]:
        user = _id(user, 'user_id')
        with self._db.transaction() as conn:
            counts = {
                row[0]: row[1] for row in conn.execute(
                    'SELECT state,COUNT(*) FROM story_runtime_jobs '
                    'WHERE user_id=? GROUP BY state',
                    (user,),
                )
            }
            result: dict[str, Any] = {
                state: counts.get(state, 0)
                for state in ('queued', 'running', 'done', 'failed', 'cancelled')
            }
            result['stories'] = conn.execute(
                'SELECT COUNT(*) FROM story_runtime_stories WHERE user_id=? AND active=1',
                (user,),
            ).fetchone()[0]
            result['policy'] = self._policy.public_policy(self._policy.read_policy(conn, user))
            result['last_error'] = (conn.execute(
                'SELECT error_code FROM story_runtime_jobs WHERE user_id=? '
                "AND state IN ('queued','running','failed') AND error_code IS NOT NULL "
                'ORDER BY sequence DESC LIMIT 1',
                (user,),
            ).fetchone() or [None])[0]
            return result

    def history_preview(self, user: str) -> dict[str, Any]:
        user = _id(user, 'user_id')
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*),MIN(created_at),MAX(completed_at) FROM conversation_turns "
                "WHERE user_id=? AND status='completed'",
                (user,),
            ).fetchone()
            turns = []
            for turn in conn.execute(
                "SELECT * FROM conversation_turns WHERE user_id=? AND status='completed'",
                (user,),
            ):
                turns.append({
                    'request_id': turn['request_id'],
                    'identity': _identity(turn),
                    'source_digest': _digest([ref.key for ref in raw_refs(conn, user, turn)]),
                })
            return {
                'user_id': user,
                'turn_count': row[0],
                'first_at': row[1],
                'last_at': row[2],
                'turns': turns,
            }

    def retry_failed(self, user: str) -> int:
        """Explicit operator retry of technical failures, never cancelled work."""
        user = _id(user, 'user_id')
        with self._db.transaction(True) as conn:
            p = self._policy.read_policy(conn, user)
            if not p['enabled'] or not p['external_consent']:
                return 0
            count = 0
            for row in conn.execute(
                "SELECT * FROM story_runtime_jobs WHERE user_id=? AND state='failed'",
                (user,),
            ).fetchall():
                if (row['policy_revision'] != p['revision']
                        or row['error_code'] == 'source_too_large'
                        or any(self._queries.resolve(conn, user, ref) is None
                               for ref in _refs(row['sources_json']))):
                    continue
                conn.execute(
                    "UPDATE story_runtime_jobs SET state='queued',attempts=0,available_at=?, "
                    "claim=NULL,lease_until=NULL,error_code=NULL WHERE sequence=?",
                    (self._db.now(), row['sequence']),
                )
                self._policy.job_trace(
                    conn, 'story_retry_requested', user, row['request_id'],
                    {'job_id': row['job_id'], 'state': 'queued',
                     'previous_attempts': row['attempts'], 'error_code': row['error_code']},
                )
                count += 1
            return count
