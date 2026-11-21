"""Story resolution, candidate ranking, quarantine checks, and reply lineage."""

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from typing import Any, Callable, Iterable, Sequence

from malbut_agent_server.story_memory import StoryMemoryError, _id, _json, _text
from malbut_agent_server.story_memory_sources import SourceRef, SourceText, read_source
from malbut_agent_server.story_store.policy import StoryPolicyRepository
from malbut_agent_server.story_store.schema import (
    StoryDatabase,
    _CORRECTION,
    _DEICTIC_REFERENCE,
    _FACT_QUESTION,
    _PATROL_REQUEST,
    _PATROL_STOP,
    _TOPIC_CATEGORIES,
    _message_key,
    _normalized,
    _refs,
    _source_object,
)


class StoryQueryRepository:
    """Source-checked story queries, quarantine analysis, and reply recording."""

    def __init__(
        self,
        db: StoryDatabase,
        policy_repo: StoryPolicyRepository,
        *,
        resolve_uncached_hook: Callable[
            [sqlite3.Connection, str, SourceRef], SourceText | None
        ] | None = None,
    ) -> None:
        self._db = db
        self._policy = policy_repo
        self._resolve_uncached_hook = resolve_uncached_hook

    def resolve(
        self, conn: sqlite3.Connection, user: str, ref: SourceRef,
    ) -> SourceText | None:
        resolver = self._resolve_uncached_hook or self.resolve_uncached
        cache = self._db.source_cache
        if cache is None or cache.changes != conn.total_changes:
            # A later rollback does not decrement total_changes. Leave the
            # memo disabled for the rest of a snapshot that observed a write.
            return resolver(conn, user, ref)
        key = (user, ref)
        if key not in cache.sources:
            cache.sources[key] = resolver(conn, user, ref)
        return cache.sources[key]

    def fact_guards(
        self, conn: sqlite3.Connection, user: str,
    ) -> tuple[bool, list[tuple[str, Any]]]:
        cache = self._db.source_cache
        if cache is not None and cache.changes == conn.total_changes and user in cache.fact_guards:
            return cache.fact_guards[user]
        has_stamps = self._policy.has_table(conn, 'memory_turn_state')
        tombstones = [
            (row['memory_id'], json.loads(row['source_key']))
            for row in conn.execute(
                'SELECT memory_id,source_key FROM memory_tombstones WHERE user_id=?',
                (user,),
            )
        ]
        guards = (has_stamps, tombstones)
        if cache is not None and cache.changes == conn.total_changes:
            cache.fact_guards[user] = guards
        return guards

    def resolve_uncached(
        self, conn: sqlite3.Connection, user: str, ref: SourceRef,
    ) -> SourceText | None:
        if not isinstance(ref, SourceRef):
            raise ValueError('ref must be a SourceRef')
        if ref.role not in ('user', 'assistant'):
            raise ValueError('role must be user or assistant')
        identity = (ref.conversation_id, ref.session_instance_id, ref.generation, ref.turn_id)
        has_stamps, tombstones = self.fact_guards(conn, user)
        if has_stamps:
            row = conn.execute(
                '''SELECT t.request_id, t.user_content, t.assistant_content,
                          t.created_at, t.completed_at, m.dependencies_json
                   FROM conversation_turns t
                   JOIN story_runtime_scope s
                     ON s.user_id=t.user_id AND s.request_id=t.request_id
                   LEFT JOIN memory_turn_state m
                     ON m.user_id=t.user_id AND m.request_id=t.request_id
                   WHERE t.user_id=? AND t.conversation_id=? AND t.session_instance_id=?
                     AND t.generation=? AND t.turn_id=? AND t.status='completed'
                     AND s.identity_json=?
                     AND NOT EXISTS (SELECT 1 FROM story_runtime_exclusions
                                     WHERE user_id=t.user_id AND message_key=? AND sha256=?)''',
                (user, *identity, _json(identity), _message_key(ref), ref.sha256),
            ).fetchone()
        else:
            row = conn.execute(
                '''SELECT t.request_id, t.user_content, t.assistant_content,
                          t.created_at, t.completed_at, NULL AS dependencies_json
                   FROM conversation_turns t
                   JOIN story_runtime_scope s
                     ON s.user_id=t.user_id AND s.request_id=t.request_id
                   WHERE t.user_id=? AND t.conversation_id=? AND t.session_instance_id=?
                     AND t.generation=? AND t.turn_id=? AND t.status='completed'
                     AND s.identity_json=?
                     AND NOT EXISTS (SELECT 1 FROM story_runtime_exclusions
                                     WHERE user_id=t.user_id AND message_key=? AND sha256=?)''',
                (user, *identity, _json(identity), _message_key(ref), ref.sha256),
            ).fetchone()
        if row is None:
            return None
        content = row['user_content'] if ref.role == 'user' else row['assistant_content']
        if not isinstance(content, str) or not content:
            return None
        created_at, completed_at = row['created_at'], row['completed_at']
        if any(not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in (created_at, completed_at)):
            return None
        if (ref.end > len(content)
                or hashlib.sha256(content.encode('utf-8')).hexdigest() != ref.sha256):
            return None
        # Fact deletion can retain raw text. Revoke both its exact source and
        # turns stamped as depending on it. Story-only markers have no source.
        source_key = dict(
            conversation_id=ref.conversation_id,
            session_instance_id=ref.session_instance_id,
            generation=ref.generation,
            turn_id=ref.turn_id,
            request_id=row['request_id'],
        )
        dependencies = (
            set(json.loads(row['dependencies_json']))
            if row['dependencies_json'] is not None else set()
        )
        if any(revoked and (revoked == source_key or memory_id in dependencies)
               for memory_id, revoked in tombstones):
            return None
        return SourceText(ref, content[ref.start:ref.end], float(created_at), float(completed_at))

    def story_valid(self, conn: sqlite3.Connection, user: str, row: Any) -> bool:
        return bool(
            row['active']
            and all(self.resolve(conn, user, ref) is not None for ref in _refs(row['refs_json']))
        )

    @staticmethod
    def story_dict(row: Any) -> dict[str, Any]:
        return {
            'story_id': row['story_id'],
            'title': row['title'],
            'aliases': json.loads(row['aliases_json']),
            'current': json.loads(row['current_json']),
            'episodes': json.loads(row['episodes_json']),
            'version': row['version'],
            'updated_at': row['updated_at'],
            'source_time': row['source_time'],
            'untrusted': True,
            'execution_authorized': False,
        }

    def candidate_rows(
        self,
        conn: sqlite3.Connection,
        user: str,
        query: str,
        limit: int | None,
        exclude_ids: Sequence[str] = (),
        *,
        extraction: bool = False,
    ) -> list[Any]:
        normalized_query = unicodedata.normalize('NFKC', query).casefold()
        terms = set(re.findall(r'\w+', normalized_query))
        if extraction:
            # First-person pronouns identify the speaker, never a shared topic.
            terms.difference_update({'내', '제', '내가', '제가', '나는', '저는', '저희', '우리'})
        ranked: list[tuple[int, Any]] = []
        for row in conn.execute(
            'SELECT * FROM story_runtime_stories WHERE user_id=? AND active=1', (user,),
        ):
            if row['story_id'] in exclude_ids:
                continue
            text = unicodedata.normalize('NFKC', ' '.join([
                row['title'], *json.loads(row['aliases_json']),
                *(entry['text'] for entry in json.loads(row['current_json'])),
            ])).casefold()
            score = sum(term in text for term in terms)
            title_terms = set(re.findall(r'\w+', unicodedata.normalize(
                'NFKC', ' '.join([row['title'], *json.loads(row['aliases_json'])]),
            ).casefold()))
            score += 2 * sum(
                len(term) >= 2 and term in normalized_query
                and term not in {'이야기', '기억', '대화', '지난번'}
                for term in title_terms
            )
            if score and self.story_valid(conn, user, row):
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], -item[1]['updated_at'], item[1]['story_id']))
        return [row for _, row in ranked[:limit]]

    @staticmethod
    def standalone_patrol_receipt(
        conn: sqlite3.Connection, user: str, request_id: str, utterance: str,
    ) -> bool:
        if _PATROL_STOP.fullmatch(utterance):
            tool = 'cancel_voice_mission'
        elif _PATROL_REQUEST.fullmatch(utterance):
            tool = 'request_patrol'
        else:
            return False
        # Check the final, approved decision; older receipts cannot establish it.
        receipt = conn.execute(
            """SELECT json_extract(response_json, '$.public.decision.type'),
                      json_extract(response_json, '$.public.decision.tool_name'),
                      json_extract(response_json, '$.public.safety.code')
               FROM conversation_turns
               WHERE user_id=? AND request_id=? AND status='completed'
               AND CASE WHEN json_valid(response_json) THEN
                   json_extract(response_json, '$.schema_version') IN (2, 3)
                   AND json_type(response_json, '$.public.safety.allowed')='true'
                   AND json_type(response_json, '$.public.decision.tool_name') IS NOT NULL
               END""",
            (user, request_id),
        ).fetchone()
        if receipt is None:
            return False
        decision = tuple(receipt)
        return (
            decision == ('tool_call', tool, 'manager_request')
            or (tool == 'cancel_voice_mission' and decision == ('message', None, 'not_an_action'))
        )

    def pending_story_ids(
        self, user: str, request_id: str | None = None,
    ) -> set[str] | None:
        """Return possible extraction targets, or None when their scope is unknown."""
        with self._db.validation_snapshot(read_only_scope=True), self._db.transaction() as conn:
            jobs = conn.execute(
                "SELECT request_id,sources_json FROM story_runtime_jobs "
                "WHERE user_id=? AND state IN ('queued','running')",
                (user,),
            ).fetchall()
            if len(jobs) >= self._db.max_pending():
                # Recovery can still owe a job for a turn that did not fit.
                return None
            texts: list[str] = []
            for job in jobs:
                if request_id is not None and job['request_id'] == request_id:
                    continue
                refs = _refs(job['sources_json'])
                sources = {ref.role: self.resolve(conn, user, ref) for ref in refs}
                user_src = sources.get('user')
                assistant_src = sources.get('assistant')
                if set(sources) != {'user', 'assistant'} or user_src is None or assistant_src is None:
                    return None
                utterance = ''.join(_normalized(user_src.text).split())
                if not (_FACT_QUESTION.fullmatch(utterance)
                        or self.standalone_patrol_receipt(conn, user, job['request_id'], utterance)):
                    return None
                texts.extend((user_src.text, assistant_src.text))
            if not texts:
                return set()
            # claim() matches both sides of every turn in a batch. All positive
            # matches across pending turns cover every possible top-five batch.
            return {
                row['story_id'] for row in self.candidate_rows(
                    conn, user, ' '.join(texts), limit=None, extraction=True,
                )
            }

    def failed_story_ids(
        self,
        user: str,
        request_id: str | None = None,
        *,
        candidate_ids: Iterable[str] | None = None,
    ) -> set[str]:
        """Quarantine affected stories, not every story owned by a failed job's user.

        Use the failed turn's user text, not its assistant reply or the broad
        extraction candidate list. A generic overlap such as '수업' does not make
        a yoga question a correction of a pottery booking. Ambiguous corrections
        use preceding conversation lineage; without a resolvable target they
        conservatively hold existing stories until explicit retry succeeds.
        No failed extraction output is trusted, and this works on existing DBs.
        """
        user = _id(user, 'user_id')
        with self._db.validation_snapshot(read_only_scope=True), self._db.transaction() as conn:
            jobs = [
                job for job in conn.execute(
                    "SELECT * FROM story_runtime_jobs WHERE user_id=? AND state='failed'",
                    (user,),
                )
                if request_id is None or job['request_id'] != request_id
            ]
            if not jobs:
                return set()
            stories = {
                row['story_id']: row for row in conn.execute(
                    'SELECT story_id, title, aliases_json, active, refs_json '
                    'FROM story_runtime_stories WHERE user_id=? AND active=1',
                    (user,),
                )
            }
            fallback_targets: Iterable[str] = (
                stories if candidate_ids is None
                else [sid for sid in candidate_ids if sid in stories]
            )
            validity: dict[str, bool] = {}

            def valid_ids(identifiers: Iterable[str]) -> set[str]:
                # All matches and fallback decisions use the same source-valid
                # stories. Resolve only the topics a failed correction can affect.
                result: set[str] = set()
                for identifier in identifiers:
                    if identifier not in stories:
                        continue
                    if identifier not in validity:
                        validity[identifier] = self.story_valid(conn, user, stories[identifier])
                    if validity[identifier]:
                        result.add(identifier)
                return result

            topics: dict[str, set[str]] = {}
            for row in stories.values():
                labels = [row['title'], *json.loads(row['aliases_json'])]
                words = set(re.findall(r'\w+', _normalized(' '.join(labels))))
                specific = {word for word in words if len(word) >= 2} - _TOPIC_CATEGORIES
                # A story called only '모임' has no more specific identity.
                topics[row['story_id']] = specific or words

            def matching(text: str) -> set[str]:
                normalized = _normalized(text)
                return valid_ids(
                    identifier for identifier, words in topics.items()
                    if any(word in normalized for word in words)
                )

            blocked: set[str] = set()
            # A reply's own extraction cannot retroactively cancel delivery.
            for job in jobs:
                refs = [ref for ref in _refs(job['sources_json']) if ref.role == 'user']
                sources = [self.resolve(conn, user, ref) for ref in refs]
                if not sources or any(source is None for source in sources):
                    blocked.update(valid_ids(fallback_targets))
                    continue
                text = ' '.join(source.text for source in sources if source is not None)
                direct = matching(text)
                blocked.update(direct)
                deictic = _DEICTIC_REFERENCE.search(text)
                if not deictic and (direct or not _CORRECTION.search(text)):
                    continue
                # A turn may correct another topic as well as naming one topic.
                # Resolve pronouns against the same session/generation only.
                contextual: set[str] = set()
                for ref in refs:
                    previous = conn.execute(
                        "SELECT request_id,user_content,turn_id FROM conversation_turns WHERE user_id=? "
                        "AND conversation_id=? AND session_instance_id=? AND generation=? "
                        "AND status='completed' AND ordinal < (SELECT ordinal FROM conversation_turns "
                        "WHERE user_id=? AND request_id=?) ORDER BY ordinal DESC LIMIT 2",
                        (user, ref.conversation_id, ref.session_instance_id, ref.generation,
                         user, job['request_id']),
                    ).fetchall()
                    for turn in previous:
                        contextual.update(matching(turn['user_content']))
                        reply = conn.execute(
                            'SELECT stories_json FROM story_runtime_replies '
                            'WHERE user_id=? AND request_id=?',
                            (user, turn['request_id']),
                        ).fetchone()
                        if reply:
                            contextual.update(valid_ids(json.loads(reply[0])))
                blocked.update(contextual)
                if not direct and not contextual:
                    blocked.update(valid_ids(fallback_targets))
            return blocked

    def candidates(
        self,
        user: str,
        query: str,
        limit: int = 5,
        *,
        exclude_ids: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        user = _id(user, 'user_id')
        query = _text(query, 'query', 20000)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise StoryMemoryError('invalid candidate limit')
        with self._db.validation_snapshot(read_only_scope=True), self._db.transaction() as conn:
            if not self._policy.read_policy(conn, user)['enabled']:
                return []
            result: list[dict[str, Any]] = []
            for row in self.candidate_rows(conn, user, query, limit, exclude_ids):
                story = self.story_dict(row)
                story.pop('episodes')
                result.append(story)
            return result

    def list_stories(
        self, user: str, *, story_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        user = _id(user, 'user_id')
        identifiers = None if story_ids is None else sorted({
            _id(value, 'story_id') for value in story_ids
        })
        if identifiers == []:
            return []
        if identifiers is not None and len(identifiers) > 20:
            raise StoryMemoryError('too many story identifiers')
        query = 'SELECT * FROM story_runtime_stories WHERE user_id=?'
        parameters: list[Any] = [user]
        if identifiers is not None:
            query += ' AND story_id IN (' + ','.join('?' for _ in identifiers) + ')'
            parameters.extend(identifiers)
        query += ' ORDER BY updated_at DESC,story_id'
        with self._db.transaction() as conn:
            return [
                self.story_dict(row) for row in conn.execute(query, parameters)
                if self.story_valid(conn, user, row)
            ]

    def evidence(self, user: str, story_id: str) -> list[dict[str, Any]]:
        user, story_id = _id(user, 'user_id'), _id(story_id, 'story_id')
        with self._db.transaction() as conn:
            row = conn.execute(
                'SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                (user, story_id),
            ).fetchone()
            if row is None or not self.story_valid(conn, user, row):
                return []
            result: list[dict[str, Any]] = []
            for index, ref in enumerate(_refs(row['refs_json'])):
                source = self.resolve(conn, user, ref)
                if source is not None:
                    result.append(_source_object(ref, source, 'e' + str(index + 1)))
            return result

    def record_reply(
        self, user: str, request_id: str, story_ids: Sequence[str], revision: int,
    ) -> bool:
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        if not isinstance(story_ids, (list, tuple)) or len(story_ids) > 20:
            raise StoryMemoryError('invalid reply dependencies')
        ids = sorted({_id(value, 'story_id') for value in story_ids})
        with self._db.transaction(True) as conn:
            p = self._policy.read_policy(conn, user)
            if not p['enabled'] or p['revision'] != revision:
                return False
            if not conn.execute(
                'SELECT 1 FROM conversation_turns WHERE user_id=? AND request_id=?',
                (user, request_id),
            ).fetchone():
                return False
            for identifier in ids:
                row = conn.execute(
                    'SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                    (user, identifier),
                ).fetchone()
                if row is None or not self.story_valid(conn, user, row):
                    return False
            existing = conn.execute(
                'SELECT stories_json FROM story_runtime_replies '
                'WHERE user_id=? AND request_id=?',
                (user, request_id),
            ).fetchone()
            if existing:
                ids = sorted(set(ids) | set(json.loads(existing[0])))
            conn.execute(
                'INSERT INTO story_runtime_replies VALUES (?,?,?,?) '
                'ON CONFLICT(user_id,request_id) DO UPDATE SET '
                'stories_json=excluded.stories_json,revision=excluded.revision',
                (user, request_id, _json(ids), revision),
            )
            return True

    def record_context_reply(
        self, user: str, request_id: str, turns: Sequence[Any], summary: Any,
    ) -> bool:
        """Track erasure lineage of actual short-term model input, even when off.

        This stores identifiers only, not a new story or a source-content copy.
        It prevents an unindexed paraphrase of existing dialogue from escaping
        a later story deletion merely because lexical recall returned no hit.
        """
        user, request_id = _id(user, 'user_id'), _id(request_id, 'request_id')
        if not isinstance(turns, (list, tuple)):
            raise StoryMemoryError('invalid conversation context')
        supplied: dict[tuple[Any, Any, Any, Any], Any] = {}
        ancestor_requests: set[str] = set()
        for turn in turns:
            if getattr(turn, 'user_id', None) != user:
                raise StoryMemoryError('context belongs to a different user')
            identity = (
                turn.conversation_id, turn.session_instance_id,
                turn.generation, turn.turn_id,
            )
            supplied[identity] = turn
            if turn.assistant_content:
                ancestor_requests.add(turn.request_id)
        if summary is not None and getattr(summary, 'user_id', None) != user:
            raise StoryMemoryError('summary belongs to a different user')
        with self._db.locked():
            with self._db.transaction(False) as conn:
                if not conn.execute(
                    'SELECT 1 FROM conversation_turns WHERE user_id=? '
                    'AND request_id=?',
                    (user, request_id),
                ).fetchone():
                    return False
                if not supplied and summary is None:
                    return True
                summarized: set[tuple[Any, ...]] = set()
                if summary is not None:
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
                ids: set[str] = set()
                conv_ids = sorted(
                    {identity[0] for identity in supplied}
                    | {identity[0] for identity in summarized}
                )
                if conv_ids:
                    candidate_rows = []
                    for start in range(0, len(conv_ids), 500):
                        batch = conv_ids[start:start + 500]
                        candidate_rows.extend(conn.execute(
                            'SELECT DISTINCT s.story_id, s.refs_json '
                            'FROM story_runtime_story_turns t '
                            'JOIN story_runtime_stories s '
                            '  ON s.user_id=t.user_id AND s.story_id=t.story_id '
                            'WHERE t.user_id=? AND t.conversation_id IN ('
                            + ','.join('?' for _ in batch) + ')',
                            [user, *batch],
                        ).fetchall())
                    seen_stories: set[str] = set()
                    for row in candidate_rows:
                        story_id = row['story_id']
                        if story_id in seen_stories:
                            continue
                        seen_stories.add(story_id)
                        for ref in _refs(row['refs_json']):
                            ref_identity = (
                                ref.conversation_id, ref.session_instance_id,
                                ref.generation, ref.turn_id,
                            )
                            if ref_identity in summarized:
                                ids.add(story_id)
                                break
                            turn = supplied.get(ref_identity)
                            if turn is None:
                                continue
                            source = read_source(conn, user, ref)
                            displayed = (
                                turn.user_content if ref.role == 'user'
                                else turn.assistant_content
                            ) or ''
                            if source is not None and source.text in displayed:
                                ids.add(story_id)
                                break
                requests = sorted(ancestor_requests | {request_id})
                for start in range(0, len(requests), 500):
                    batch = requests[start:start + 500]
                    for row in conn.execute(
                        'SELECT stories_json FROM story_runtime_replies WHERE user_id=? '
                        'AND request_id IN (' + ','.join('?' for _ in batch) + ')',
                        [user, *batch],
                    ):
                        ids.update(json.loads(row['stories_json']))
                if not ids:
                    return True
                sorted_ids_json = _json(sorted(ids))
                revision = self._policy.read_policy(conn, user)['revision']
                existing = conn.execute(
                    'SELECT stories_json,revision FROM story_runtime_replies '
                    'WHERE user_id=? AND request_id=?',
                    (user, request_id),
                ).fetchone()
                if (existing and existing['stories_json'] == sorted_ids_json
                        and existing['revision'] == revision):
                    return True
            with self._db.transaction(True) as conn:
                if not conn.execute(
                    'SELECT 1 FROM conversation_turns WHERE user_id=? '
                    'AND request_id=?',
                    (user, request_id),
                ).fetchone():
                    return False
                revision = self._policy.read_policy(conn, user)['revision']
                existing = conn.execute(
                    'SELECT stories_json FROM story_runtime_replies '
                    'WHERE user_id=? AND request_id=?',
                    (user, request_id),
                ).fetchone()
                if existing:
                    ids.update(json.loads(existing['stories_json']))
                conn.execute(
                    'INSERT INTO story_runtime_replies VALUES (?,?,?,?) '
                    'ON CONFLICT(user_id,request_id) DO UPDATE SET '
                    'stories_json=excluded.stories_json,revision=excluded.revision',
                    (user, request_id, _json(sorted(ids)), revision),
                )
            return True

    def own_reply_updated_story(
        self, user: str, story_id: str, request_id: str,
    ) -> bool:
        """Recognize only a valid latest checkpoint ending with this reply.

        This is not a general freshness bypass: any later source turn in the
        batch, even with an equal clock timestamp, makes the answer false.
        """
        user, story_id, request_id = (
            _id(user, 'user_id'), _id(story_id, 'story_id'), _id(request_id, 'request_id'),
        )
        with self._db.transaction() as conn:
            if not self._policy.read_policy(conn, user)['enabled']:
                return False
            story = conn.execute(
                'SELECT * FROM story_runtime_stories WHERE user_id=? AND story_id=?',
                (user, story_id),
            ).fetchone()
            own = conn.execute(
                "SELECT rowid,* FROM conversation_turns WHERE user_id=? AND request_id=? "
                "AND status='completed'",
                (user, request_id),
            ).fetchone()
            if story is None or own is None or not self.story_valid(conn, user, story):
                return False
            episodes = json.loads(story['episodes_json'])
            if not episodes or story['source_time'] > own['completed_at']:
                return False
            latest = conn.execute(
                "SELECT * FROM story_runtime_jobs WHERE user_id=? AND job_id=? AND state='done'",
                (user, episodes[-1]['job_id']),
            ).fetchone()
            if latest is None:
                return False
            members = json.loads(latest['batch_json']) or [latest['job_id']]
            found = False
            for identifier in members:
                member = conn.execute(
                    'SELECT t.rowid,t.request_id,t.completed_at FROM story_runtime_jobs j '
                    'JOIN conversation_turns t ON t.user_id=j.user_id AND t.request_id=j.request_id '
                    'WHERE j.user_id=? AND j.job_id=?',
                    (user, identifier),
                ).fetchone()
                if member is None or member['rowid'] > own['rowid'] or member['completed_at'] > own['completed_at']:
                    return False
                found = found or member['request_id'] == request_id
            return found
