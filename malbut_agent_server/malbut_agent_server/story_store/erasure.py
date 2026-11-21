"""Story and conversation erasure, span redaction, and ref remapping."""

from dataclasses import replace
import hashlib
import json
import re
import secrets
from typing import Any, Iterable

from malbut_agent_server.story_memory import StoryMemoryError, _id, _json
from malbut_agent_server.story_memory_sources import SourceRef, read_source
from malbut_agent_server.story_store.policy import StoryPolicyRepository
from malbut_agent_server.story_store.queries import StoryQueryRepository
from malbut_agent_server.story_store.queue import StoryQueueRepository
from malbut_agent_server.story_store.schema import (
    StoryDatabase,
    _message_key,
    _refs,
    _refs_json,
    raw_refs,
    sync_story_turns,
)


class StoryErasureRepository:
    """Application-level redaction for stories, conversations, and derived lineage."""

    def __init__(
        self,
        db: StoryDatabase,
        memory_store: Any,
        policy_repo: StoryPolicyRepository,
        query_repo: StoryQueryRepository,
        queue_repo: StoryQueueRepository,
    ) -> None:
        self._db = db
        self.memory = memory_store
        self._policy = policy_repo
        self._queries = query_repo
        self._queue = queue_repo

    def invalidate_summary(self, user: str, story_id: str) -> bool:
        user, story_id = _id(user, 'user_id'), _id(story_id, 'story_id')
        with self._db.transaction(True) as conn:
            changed = conn.execute(
                'UPDATE story_runtime_stories SET active=0 WHERE user_id=? AND story_id=?',
                (user, story_id),
            ).rowcount
            if changed:
                conn.execute(
                    'UPDATE story_runtime_policy SET revision=revision+1,data_revision=data_revision+1 '
                    'WHERE user_id=?',
                    (user,),
                )
                self._policy.fence(conn, user)
            return bool(changed)

    @staticmethod
    def merged_ranges(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def remove_ranges(text: str, spans: Iterable[tuple[int, int]]) -> str:
        for start, end in reversed(StoryErasureRepository.merged_ranges(spans)):
            text = text[:start] + text[end:]
        return text

    @staticmethod
    def remap_ref(ref: SourceRef, change: dict[str, Any]) -> SourceRef | None:
        if ref.sha256 != hashlib.sha256(change['old'].encode('utf-8')).hexdigest():
            return None
        ranges = change['ranges']
        if any(start < ref.end and end > ref.start for start, end in ranges):
            return None
        shift = sum(end - start for start, end in ranges if end <= ref.start)
        return replace(
            ref,
            start=ref.start - shift,
            end=ref.end - shift,
            sha256=hashlib.sha256(change['new'].encode('utf-8')).hexdigest(),
        )

    def delete_conversation(self, user: str, conversation_id: str) -> bool:
        """Delete raw dialogue and fence its derived memory in one transaction.

        A story can combine several sessions. Its affected derived record is
        dropped conservatively, but other sessions' user messages are retained.
        Assistant paraphrases with explicit story lineage are cleared as well.
        """
        from malbut_agent_server.conversation import validate_conversation_id

        user = _id(user, 'user_id')
        conversation_id = validate_conversation_id(conversation_id)
        with self._db.transaction(True) as conn:
            if not conn.execute(
                'SELECT 1 FROM conversation_sessions WHERE user_id=? AND conversation_id=?',
                (user, conversation_id),
            ).fetchone():
                return False
            pending_sequences = [
                (row[0], row[1]) for row in conn.execute(
                    "SELECT sequence,state FROM story_runtime_jobs WHERE user_id=? "
                    "AND state IN ('queued','running','failed')",
                    (user,),
                )
            ]
            turns = conn.execute(
                'SELECT * FROM conversation_turns WHERE user_id=?', (user,),
            ).fetchall()
            removed_requests = {
                row['request_id'] for row in turns
                if row['conversation_id'] == conversation_id
            }
            removed_identities = {
                (row['conversation_id'], row['session_instance_id'],
                 row['generation'], row['turn_id'])
                for row in turns if row['request_id'] in removed_requests
            }
            stories = conn.execute(
                'SELECT story_id,refs_json FROM story_runtime_stories WHERE user_id=?',
                (user,),
            ).fetchall()
            replies = conn.execute(
                'SELECT request_id,stories_json FROM story_runtime_replies WHERE user_id=?',
                (user,),
            ).fetchall()
            affected_stories: set[str] = set()
            dependent_replies: set[str] = set()
            # Follow explicit assistant provenance until no additional story is
            # affected. Text equality never authorizes deleting another source.
            while True:
                before = (len(affected_stories), len(dependent_replies))
                reply_identities = {
                    (row['conversation_id'], row['session_instance_id'],
                     row['generation'], row['turn_id'])
                    for row in turns if row['request_id'] in dependent_replies
                }
                for story in stories:
                    if any(
                        (ref.conversation_id, ref.session_instance_id, ref.generation,
                         ref.turn_id) in removed_identities
                        or (
                            ref.role == 'assistant'
                            and (ref.conversation_id, ref.session_instance_id,
                                 ref.generation, ref.turn_id) in reply_identities
                        )
                        for ref in _refs(story['refs_json'])
                    ):
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
                self._policy.mark_reply_stale(conn, user, row['request_id'])
                if row['conversation_id'] == conversation_id:
                    continue
                sessions.add(row['conversation_id'])
                content = row['assistant_content'] or ''
                key = _json([
                    row['conversation_id'], row['session_instance_id'],
                    row['generation'], row['turn_id'], 'assistant',
                ])
                conn.execute(
                    'INSERT OR IGNORE INTO story_runtime_exclusions VALUES (?,?,?)',
                    (user, key, hashlib.sha256(content.encode('utf-8')).hexdigest()),
                )
                conn.execute(
                    "UPDATE conversation_turns SET assistant_content='',response_json='{}',"
                    'request_fingerprint=? WHERE user_id=? AND request_id=?',
                    ('story-revoked-' + secrets.token_hex(16), user, row['request_id']),
                )
            for story_id in affected_stories:
                conn.execute(
                    'DELETE FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                    (user, story_id),
                )
                conn.execute(
                    'DELETE FROM story_runtime_story_turns WHERE user_id=? AND story_id=?',
                    (user, story_id),
                )
            for request_id in affected_requests:
                conn.execute(
                    'DELETE FROM story_runtime_replies WHERE user_id=? AND request_id=?',
                    (user, request_id),
                )
                conn.execute(
                    'DELETE FROM story_runtime_scope WHERE user_id=? AND request_id=?',
                    (user, request_id),
                )
                conn.execute(
                    "UPDATE story_runtime_jobs SET state='cancelled',claim=NULL,"
                    "lease_until=NULL,read_sources_json='[]',result_digest=NULL "
                    'WHERE user_id=? AND request_id=?',
                    (user, request_id),
                )
            fact_ids = [
                fact.id for fact in self.memory.list_for_user(user, connection=conn)
                if fact.metadata.get('source', {}).get('conversation_id') == conversation_id
            ]
            if fact_ids:
                self.memory.remove_facts(user, fact_ids, connection=conn)
            for session in sessions:
                conn.execute(
                    'DELETE FROM conversation_summaries WHERE user_id=? AND conversation_id=?',
                    (user, session),
                )
                conn.execute(
                    'UPDATE conversation_sessions SET revision=revision+1 '
                    'WHERE user_id=? AND conversation_id=?',
                    (user, session),
                )
            conn.execute(
                'DELETE FROM conversation_sessions WHERE user_id=? AND conversation_id=?',
                (user, conversation_id),
            )
            # Also create the epoch for a legacy user who never enabled stories.
            conn.execute('INSERT OR IGNORE INTO story_runtime_policy(user_id) VALUES (?)', (user,))
            conn.execute(
                'UPDATE story_runtime_policy SET revision=revision+1,data_revision=data_revision+1 '
                'WHERE user_id=?',
                (user,),
            )
            self._policy.fence(conn, user)
            revision = self._policy.read_policy(conn, user)['revision']
            for sequence, state in pending_sequences:
                job = conn.execute(
                    'SELECT * FROM story_runtime_jobs WHERE sequence=?', (sequence,),
                ).fetchone()
                if job['request_id'] not in affected_requests and all(
                        self._queries.resolve(conn, user, ref) is not None
                        for ref in _refs(job['sources_json'])):
                    conn.execute(
                        'UPDATE story_runtime_jobs SET state=?,policy_revision=?,available_at=? '
                        'WHERE sequence=?',
                        ('failed' if state == 'failed' else 'queued',
                         revision, self._db.now(), sequence),
                    )
            return True

    def delete_story(
        self, user: str, story_id: str, require_settled: bool = False,
    ) -> dict[str, Any]:
        user, story_id = _id(user, 'user_id'), _id(story_id, 'story_id')
        if type(require_settled) is not bool:
            raise StoryMemoryError('require_settled must be boolean')
        with self._db.transaction(True) as conn:
            story = conn.execute(
                'SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                (user, story_id),
            ).fetchone()
            if story is None:
                return {'deleted': False, 'stories': 0, 'source_messages': 0, 'facts': 0}
            if require_settled:
                policy = self._policy.read_policy(conn, user)
                completed = conn.execute(
                    "SELECT * FROM conversation_turns WHERE user_id=? "
                    "AND status='completed'",
                    (user,),
                ).fetchall()
                if policy['enabled']:
                    for turn in completed:
                        self._queue.enqueue(conn, user, turn['request_id'])
                if conn.execute(
                    "SELECT 1 FROM story_runtime_jobs WHERE user_id=? "
                    "AND state IN ('queued','running','failed') LIMIT 1",
                    (user,),
                ).fetchone():
                    raise StoryMemoryError('story_delete_requires_settled_sources')
                # Off cancels extraction, not its unresolved source scope. A
                # successful checkpoint is durable even after deletion fences
                # its replay. Never silently send cancelled input to a model.
                for turn in completed:
                    if not self._queue.grant(conn, user, turn, policy):
                        continue
                    source_refs = [
                        ref for ref in raw_refs(conn, user, turn)
                        if self._queries.resolve(conn, user, ref) is not None
                    ]
                    if not any(ref.role == 'user' for ref in source_refs):
                        continue
                    jobs = conn.execute(
                        'SELECT processed,sources_json FROM story_runtime_jobs '
                        'WHERE user_id=? AND request_id=?',
                        (user, turn['request_id']),
                    ).fetchall()
                    processed = any(
                        job['processed'] and any(
                            ref.conversation_id == turn['conversation_id']
                            and ref.session_instance_id == turn['session_instance_id']
                            and ref.generation == turn['generation']
                            and ref.turn_id == turn['turn_id']
                            for ref in _refs(job['sources_json'])
                        )
                        for job in jobs
                    )
                    if not processed:
                        raise StoryMemoryError('story_delete_requires_history_review')
            refs = _refs(story['refs_json'])
            pending_sequences = [
                (row[0], row[1]) for row in conn.execute(
                    "SELECT sequence,state FROM story_runtime_jobs WHERE user_id=? "
                    "AND state IN ('queued','running','failed')",
                    (user,),
                )
            ]
            spans: dict[str, list[tuple[int, int]]] = {}
            for ref in refs:
                source = read_source(conn, user, ref)
                if source is not None:
                    spans.setdefault(_message_key(ref), []).append((ref.start, ref.end))
            dependent_replies: set[str] = set()
            for row in conn.execute('SELECT * FROM story_runtime_replies WHERE user_id=?', (user,)):
                if story_id in json.loads(row['stories_json']):
                    dependent_replies.add(row['request_id'])
            changed_turns: list[Any] = []
            changed_messages: set[str] = set()
            sessions: set[str] = set()
            changes: dict[str, dict[str, Any]] = {}
            rows = conn.execute('SELECT * FROM conversation_turns WHERE user_id=?', (user,)).fetchall()
            for row in rows:
                replacement: dict[str, str] = {}
                for role, column in (('user', 'user_content'), ('assistant', 'assistant_content')):
                    content = row[column] or ''
                    key = _json([
                        row['conversation_id'], row['session_instance_id'],
                        row['generation'], row['turn_id'], role,
                    ])
                    ranges = list(spans.get(key, []))
                    # Identical generic responses can belong to unrelated stories.
                    # Only explicit source spans or propagated reply dependencies
                    # authorize redaction; text equality alone is not provenance.
                    if role == 'assistant' and row['request_id'] in dependent_replies:
                        ranges = [(0, len(content))] if content else []
                    if ranges:
                        new = self.remove_ranges(content, ranges)
                        replacement[column] = new
                        changed_messages.add(key)
                        changes[key] = {
                            'old': content,
                            'new': new,
                            'ranges': self.merged_ranges(ranges),
                        }
                        conn.execute(
                            'INSERT OR IGNORE INTO story_runtime_exclusions VALUES (?,?,?)',
                            (user, key, hashlib.sha256(content.encode('utf-8')).hexdigest()),
                        )
                if replacement or row['request_id'] in dependent_replies:
                    changed_turns.append(row)
                    sessions.add(row['conversation_id'])
                    conn.execute(
                        'UPDATE conversation_turns SET user_content=?,assistant_content=?, '
                        'response_json=?,request_fingerprint=? WHERE user_id=? AND request_id=?',
                        (replacement.get('user_content', row['user_content']),
                         replacement.get('assistant_content', row['assistant_content']), '{}',
                         'story-revoked-' + secrets.token_hex(16), user, row['request_id']),
                    )
                    self._policy.mark_reply_stale(conn, user, row['request_id'])
            # Invalidate every derived story whose whole-message digest changed;
            # remaining raw text is retained and can be extracted again safely.
            invalidated = 0
            for other in conn.execute('SELECT * FROM story_runtime_stories WHERE user_id=?', (user,)).fetchall():
                if other['story_id'] == story_id:
                    continue
                previous_refs = _refs(other['refs_json'])
                if any(_message_key(ref) in changed_messages for ref in previous_refs):
                    mapped: dict[Any, SourceRef | None] = {
                        ref.key: (
                            self.remap_ref(ref, changes[_message_key(ref)])
                            if _message_key(ref) in changes else ref
                        )
                        for ref in previous_refs
                    }

                    def clean_entries(
                        entries: list[dict[str, Any]],
                        ref_map: dict[Any, SourceRef | None],
                    ) -> list[dict[str, Any]]:
                        result: list[dict[str, Any]] = []
                        for entry in entries:
                            remapped_keys: list[Any] = []
                            keep = True
                            for key in entry['source_keys']:
                                remapped = ref_map.get(key)
                                if remapped is None:
                                    keep = False
                                    break
                                remapped_keys.append(remapped.key)
                            if keep:
                                entry['source_keys'] = remapped_keys
                                result.append(entry)
                        return result

                    old_current = json.loads(other['current_json'])
                    current = clean_entries(old_current, mapped)
                    episodes = json.loads(other['episodes_json'])
                    for episode in episodes:
                        episode['entries'] = clean_entries(episode['entries'], mapped)
                    episodes = [episode for episode in episodes if episode['entries']]
                    if not current:
                        # Do not leave unreachable sensitive titles/aliases behind.
                        conn.execute(
                            'DELETE FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                            (user, other['story_id']),
                        )
                        conn.execute(
                            'DELETE FROM story_runtime_story_turns WHERE user_id=? AND story_id=?',
                            (user, other['story_id']),
                        )
                        invalidated += 1
                    else:
                        removed_current = len(current) != len(old_current)
                        title = current[0]['text'][:128] if removed_current else other['title']
                        aliases = '[]' if removed_current else other['aliases_json']
                        remaining_refs = [ref for ref in mapped.values() if ref is not None]
                        conn.execute(
                            'UPDATE story_runtime_stories SET refs_json=?,current_json=?,episodes_json=?, '
                            'title=?,aliases_json=?,version=version+1 WHERE user_id=? AND story_id=?',
                            (_refs_json(remaining_refs),
                             _json(current), _json(episodes), title, aliases, user, other['story_id']),
                        )
                        sync_story_turns(conn, user, other['story_id'], remaining_refs)
            conn.execute(
                'DELETE FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                (user, story_id),
            )
            conn.execute(
                'DELETE FROM story_runtime_story_turns WHERE user_id=? AND story_id=?',
                (user, story_id),
            )
            for conversation_id in sessions:
                conn.execute(
                    'DELETE FROM conversation_summaries WHERE user_id=? AND conversation_id=?',
                    (user, conversation_id),
                )
            affected_requests = {row['request_id'] for row in changed_turns}
            fact_ids: list[str] = []
            for fact in self.memory.list_for_user(user, connection=conn):
                origin = fact.metadata.get('source', {})
                key = _json([
                    origin.get('conversation_id'), origin.get('session_instance_id'),
                    origin.get('generation'), origin.get('turn_id'), 'user',
                ])
                evidence = fact.metadata.get('fact', {}).get('evidence', '')
                if key in changes:
                    change = changes[key]
                    starts = (
                        [match.start() for match in re.finditer(re.escape(evidence), change['old'])]
                        if evidence else []
                    )
                    if not starts or any(
                        start < offset + len(evidence) and end > offset
                        for offset in starts for start, end in change['ranges']
                    ):
                        fact_ids.append(fact.id)
                    else:
                        # A preserved fact's source metadata is another raw copy.
                        metadata = dict(fact.metadata)
                        metadata['source'] = dict(origin, text=change['new'])
                        conn.execute(
                            'UPDATE memories SET metadata_json=? WHERE user_id=? AND id=?',
                            (_json(metadata), user, fact.id),
                        )
            if fact_ids:
                self.memory.remove_facts(user, fact_ids, connection=conn)
            for reply in conn.execute('SELECT * FROM story_runtime_replies WHERE user_id=?', (user,)).fetchall():
                identifiers = [value for value in json.loads(reply['stories_json']) if value != story_id]
                if reply['request_id'] in affected_requests or not identifiers:
                    conn.execute(
                        'DELETE FROM story_runtime_replies WHERE user_id=? AND request_id=?',
                        (user, reply['request_id']),
                    )
                else:
                    conn.execute(
                        'UPDATE story_runtime_replies SET stories_json=? WHERE user_id=? AND request_id=?',
                        (_json(identifiers), user, reply['request_id']),
                    )
            conn.execute(
                'UPDATE story_runtime_policy SET revision=revision+1,data_revision=data_revision+1 '
                'WHERE user_id=?',
                (user,),
            )
            self._policy.fence(conn, user)
            revision = self._policy.read_policy(conn, user)['revision']
            for sequence, old_state in pending_sequences:
                job = conn.execute(
                    'SELECT * FROM story_runtime_jobs WHERE sequence=?', (sequence,),
                ).fetchone()
                if job['request_id'] not in affected_requests and all(
                        self._queries.resolve(conn, user, ref) is not None
                        for ref in _refs(job['sources_json'])):
                    conn.execute(
                        'UPDATE story_runtime_jobs SET state=?,policy_revision=?,available_at=? '
                        'WHERE sequence=?',
                        ('failed' if old_state == 'failed' else 'queued',
                         revision, self._db.now(), sequence),
                    )
            # Cancel completed jobs too: a replay must not return or recreate old output.
            for request_id in affected_requests:
                conn.execute(
                    "UPDATE story_runtime_jobs SET state='cancelled',read_sources_json='[]',result_digest=NULL "
                    'WHERE user_id=? AND request_id=?',
                    (user, request_id),
                )
            return {
                'deleted': True,
                'stories': 1,
                'source_messages': len(changed_messages),
                'facts': len(fact_ids),
                'invalidated_stories': invalidated,
                'physical_erasure': False,
            }
