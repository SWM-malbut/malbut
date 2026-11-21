"""Durable runtime story memory on the conversation connection.

Consent windows grant completed source turns, independently of personal-fact
consent. Jobs keep references, not raw copies. All provider work happens outside
this class's transactions. Quotes establish provenance, not semantic truth.
Deletion is application-level redaction; it does not promise media/backup erasure.
"""

import time

from malbut_agent_server.story_store import (
    FAILURE_CODES,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    MAX_PENDING,
    MAX_SOURCE_CHARS,
    MAX_UPDATES,
    StoryDatabase,
    StoryErasureRepository,
    StoryPolicyRepository,
    StoryQueryRepository,
    StoryQueueRepository,
    _CORRECTION,
    _DEICTIC_REFERENCE,
    _FACT_QUESTION,
    _PATROL_REQUEST,
    _PATROL_STOP,
    _TOPIC_CATEGORIES,
    _ValidationSnapshot,
    _digest,
    _identity,
    _message_key,
    _normalized,
    _refs,
    _refs_json,
    _source_object,
    raw_refs,
    sync_story_turns,
)

__all__ = [
    'FAILURE_CODES',
    'LEASE_SECONDS',
    'MAX_ATTEMPTS',
    'MAX_PENDING',
    'MAX_SOURCE_CHARS',
    'MAX_UPDATES',
    'StoryRuntimeStore',
    '_CORRECTION',
    '_DEICTIC_REFERENCE',
    '_FACT_QUESTION',
    '_PATROL_REQUEST',
    '_PATROL_STOP',
    '_TOPIC_CATEGORIES',
    '_ValidationSnapshot',
    '_digest',
    '_identity',
    '_message_key',
    '_normalized',
    '_refs',
    '_refs_json',
    '_source_object',
]


class StoryRuntimeStore:
    """A shared-transaction backend. user_id must come from a trusted caller."""

    def __init__(self, conversation_store, memory_store, clock=time.time):
        self.conversations = conversation_store
        self.memory = memory_store
        db = conversation_store.database
        if memory_store._connection is not db.connection:
            memory_store.bind_connection(db.connection, db.lock)
        self._db = StoryDatabase(
            db,
            clock=clock,
            max_pending_provider=lambda: MAX_PENDING,
        )
        self._policy_repo = StoryPolicyRepository(self._db, memory_store)
        self._queries = StoryQueryRepository(
            self._db,
            self._policy_repo,
            resolve_uncached_hook=lambda conn, user, ref: self._resolve_uncached(
                conn, user, ref,
            ),
        )
        self._queue = StoryQueueRepository(
            self._db,
            self._policy_repo,
            self._queries,
        )
        self._erasure = StoryErasureRepository(
            self._db,
            memory_store,
            self._policy_repo,
            self._queries,
            self._queue,
        )
        self._initialize()

    @property
    def _clock(self):
        return self._db.clock

    @_clock.setter
    def _clock(self, clock):
        self._db.clock = clock

    @property
    def _source_cache(self):
        return self._db.source_cache

    @_source_cache.setter
    def _source_cache(self, value):
        self._db.source_cache = value

    @property
    def _recovery_versions(self):
        return self._queue._recovery_versions

    @_recovery_versions.setter
    def _recovery_versions(self, value):
        self._queue._recovery_versions = value

    def _transaction(self, write=False):
        return self._db.transaction(write)

    def _now(self):
        return self._db.now()

    def validation_snapshot(self, *, read_only_scope=False):
        """Reuse source checks only within one locked SQLite snapshot."""
        return self._db.validation_snapshot(read_only_scope=read_only_scope)

    @staticmethod
    def _sync_story_turns(conn, user, story_id, refs):
        sync_story_turns(conn, user, story_id, refs)

    def _initialize(self):
        self._db.initialize_schema()

    def close(self):
        """The runtime owns the shared connection and closes it separately."""

    def trace(self, request, event, data):
        """Diagnostic writes have no effect on consent or memory revisions."""
        return self._policy_repo.trace(request, event, data)

    @staticmethod
    def _job_trace(conn, event, user, request_id, data):
        StoryPolicyRepository.job_trace(conn, event, user, request_id, data)

    def reply_record(self, user, request_id):
        """Read a completed owner's reply and legacy story provenance together."""
        return self._policy_repo.reply_record(user, request_id)

    def reply_dependency_revision(self, user, request_id):
        """Return identifier-only short-context lineage while a turn is pending."""
        return self._policy_repo.reply_dependency_revision(user, request_id)

    @staticmethod
    def _policy(conn, user):
        return StoryPolicyRepository.read_policy(conn, user)

    @staticmethod
    def _public_policy(policy):
        return StoryPolicyRepository.public_policy(policy)

    def policy(self, user):
        return self._policy_repo.policy(user)

    @staticmethod
    def _has_table(conn, name):
        return StoryPolicyRepository.has_table(conn, name)

    def _fence(self, conn, user):
        """Fence cached answers, compaction and both memory workers atomically."""
        return self._policy_repo.fence(conn, user)

    def _mark_reply_stale(self, conn, user, request_id):
        return self._policy_repo.mark_reply_stale(conn, user, request_id)

    def set_enabled(self, user, enabled, external_consent=False, include_history=False,
                    history_scope=None, expected_revision=None):
        return self._policy_repo.set_enabled(
            user,
            enabled,
            external_consent=external_consent,
            include_history=include_history,
            history_scope=history_scope,
            expected_revision=expected_revision,
        )

    def validate(self, user, revision, data_revision=None):
        return self._policy_repo.validate(user, revision, data_revision=data_revision)

    def _grant(self, conn, user, row, policy):
        return self._queue.grant(conn, user, row, policy)

    def _raw_refs(self, conn, user, row):
        return raw_refs(conn, user, row)

    def _enqueue(self, conn, user, request_id, *, policy=None):
        return self._queue.enqueue(conn, user, request_id, policy=policy)

    def checkpoint_source_read(self, conn, user, request_id, revision, readset, version):
        """Finish one server-certified read in the same transaction as its reply."""
        return self._queue.checkpoint_source_read(
            conn, user, request_id, revision, readset, version,
        )

    def enqueue_completed(self, user, request_id):
        return self._queue.enqueue_completed(user, request_id)

    def _recovery_candidates(self, conn, user, earliest):
        return self._queue.recovery_candidates(conn, user, earliest)

    def recover(self, user):
        return self._queue.recover(user)

    def pending_users(self):
        return self._queue.pending_users()

    def _resolve(self, conn, user, ref):
        return self._queries.resolve(conn, user, ref)

    def _fact_guards(self, conn, user):
        return self._queries.fact_guards(conn, user)

    def _resolve_uncached(self, conn, user, ref):
        return self._queries.resolve_uncached(conn, user, ref)

    def _story_valid(self, conn, user, row):
        return self._queries.story_valid(conn, user, row)

    @staticmethod
    def _story_dict(row):
        return StoryQueryRepository.story_dict(row)

    def _candidate_rows(self, conn, user, query, limit, exclude_ids=(), *, extraction=False):
        return self._queries.candidate_rows(
            conn, user, query, limit, exclude_ids, extraction=extraction,
        )

    def claim(self, user, batch_size=1):
        return self._queue.claim(user, batch_size=batch_size)

    def _job(self, conn, job, allow_done=False):
        return self._queue.job_row(conn, job, allow_done=allow_done)

    def _job_sources(self, conn, row):
        return self._queue.job_sources(conn, row)

    def job_source(self, job):
        return self._queue.job_source(job)

    @staticmethod
    def _quote_ref(evidence, sources):
        return StoryQueueRepository.quote_ref(evidence, sources)

    def _prepare_update(self, update, sources):
        return self._queue.prepare_update(update, sources)

    def commit(self, job, updates):
        return self._queue.commit(job, updates)

    def release_claim(self, job):
        """Return only a stopped worker's matching claim, without spending a retry."""
        return self._queue.release_claim(job)

    def fail(self, job, error):
        return self._queue.fail(job, error)

    @staticmethod
    def _standalone_patrol_receipt(conn, user, request_id, utterance):
        return StoryQueryRepository.standalone_patrol_receipt(
            conn, user, request_id, utterance,
        )

    def db_version(self):
        """Return a connection-local + cross-connection change token."""
        return self._policy_repo.db_version()

    def pending_story_ids(self, user, request_id=None):
        """Return possible extraction targets, or None when their scope is unknown."""
        return self._queries.pending_story_ids(user, request_id=request_id)

    def failed_story_ids(self, user, request_id=None, *, candidate_ids=None):
        """Quarantine affected stories, not every story owned by a failed job's user."""
        return self._queries.failed_story_ids(
            user, request_id=request_id, candidate_ids=candidate_ids,
        )

    def candidates(self, user, query, limit=5, *, exclude_ids=()):
        return self._queries.candidates(
            user, query, limit=limit, exclude_ids=exclude_ids,
        )

    def list_stories(self, user, *, story_ids=None):
        return self._queries.list_stories(user, story_ids=story_ids)

    def evidence(self, user, story_id):
        return self._queries.evidence(user, story_id)

    def record_reply(self, user, request_id, story_ids, revision):
        return self._queries.record_reply(user, request_id, story_ids, revision)

    def record_context_reply(self, user, request_id, turns, summary):
        """Track erasure lineage of actual short-term model input, even when off."""
        return self._queries.record_context_reply(user, request_id, turns, summary)

    def invalidate_summary(self, user, story_id):
        return self._erasure.invalidate_summary(user, story_id)

    @staticmethod
    def _merged_ranges(spans):
        return StoryErasureRepository.merged_ranges(spans)

    @staticmethod
    def _remove_ranges(text, spans):
        return StoryErasureRepository.remove_ranges(text, spans)

    @staticmethod
    def _remap_ref(ref, change):
        return StoryErasureRepository.remap_ref(ref, change)

    def delete_conversation(self, user, conversation_id):
        """Delete raw dialogue and fence its derived memory in one transaction."""
        return self._erasure.delete_conversation(user, conversation_id)

    def delete_story(self, user, story_id, require_settled=False):
        return self._erasure.delete_story(
            user, story_id, require_settled=require_settled,
        )

    def own_reply_updated_story(self, user, story_id, request_id):
        """Recognize only a valid latest checkpoint ending with this reply."""
        return self._queries.own_reply_updated_story(user, story_id, request_id)

    def stats(self, user):
        return self._queue.stats(user)

    def history_preview(self, user):
        return self._queue.history_preview(user)

    def retry_failed(self, user):
        """Explicit operator retry of technical failures, never cancelled work."""
        return self._queue.retry_failed(user)
