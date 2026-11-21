"""Consent and provenance policy coordinated with the conversation commit."""

from malbut_agent_server.personal_memory_store import (
    CANCEL,
    CONSENT_PROMPT,
    NO,
    STALE_MESSAGE,
    YES,
    MemorySnapshot,
    PersonalMemoryContext,
    PersonalMemoryMutations,
    _PET,
    _PET_ALIAS,
    _PET_LABEL,
    _PET_ORDINALS,
    _PET_OWNER,
    _correct_name_fact,
    _label_value,
    _missing_memory_followup,
    _pet_name_correction,
    _pet_name_declaration,
    _pet_name_question,
    _preference_object,
    _self_introduction,
    _stored_identity_question,
    compact,
    direct_source,
    explicit_memory_management,
    fact_matches_source,
    initialize_personal_memory_schema,
    local_intent,
    management_request,
    negated_management,
    pet_owner_is_unambiguous,
    preference_matches_source,
    reply,
    requested_operation,
    selection,
    self_name_correction,
)

__all__ = [
    'CANCEL',
    'CONSENT_PROMPT',
    'MemorySnapshot',
    'NO',
    'PersonalMemory',
    'STALE_MESSAGE',
    'YES',
    '_PET',
    '_PET_ALIAS',
    '_PET_LABEL',
    '_PET_ORDINALS',
    '_PET_OWNER',
    '_correct_name_fact',
    '_label_value',
    '_missing_memory_followup',
    '_pet_name_correction',
    '_pet_name_declaration',
    '_pet_name_question',
    '_preference_object',
    '_self_introduction',
    '_stored_identity_question',
    'compact',
    'direct_source',
    'explicit_memory_management',
    'fact_matches_source',
    'local_intent',
    'management_request',
    'negated_management',
    'pet_owner_is_unambiguous',
    'preference_matches_source',
    'reply',
    'requested_operation',
    'selection',
    'self_name_correction',
]


class PersonalMemory:
    """Validate proposals and record their effects on one SQLite connection."""

    def __init__(self, memory_store, conversation_store):
        """Use the existing DB file and keep independent public interfaces."""
        self.memory = memory_store
        self.conversations = conversation_store
        db = conversation_store.database
        if (
            memory_store.database_path
            == conversation_store.database_path
            == ':memory:'
        ):
            memory_store.bind_connection(db.connection, db.lock)
        elif memory_store.database_path != conversation_store.database_path:
            raise ValueError(
                'conversation and memory must use the same SQLite database'
            )
        initialize_personal_memory_schema(db)
        self._ctx = PersonalMemoryContext(memory_store, conversation_store)
        self._mutations = PersonalMemoryMutations(
            memory_store, conversation_store, self._ctx,
        )

    def initial_settings(self, user_id):
        """Read the four conversation defaults without enabling memory."""
        return self._ctx.initial_settings(user_id)

    def set_initial_settings(self, user_id, settings):
        """Persist explicitly selected defaults independently of consent."""
        return self._ctx.set_initial_settings(user_id, settings)

    def snapshot(self, request, token, history, summary, memory_limit=5, *, defer_facts=False):
        """Read eligible history and memories under one read transaction."""
        return self._ctx.snapshot(
            request, token, history, summary, memory_limit, defer_facts=defer_facts,
        )

    def _pet_names(self, user_id, pet, conn):
        return self._ctx._pet_names(user_id, pet, conn)

    def _active_identity(self, user_id, kind, conn):
        return self._ctx._active_identity(user_id, kind, conn)

    @staticmethod
    def _name_history(user_id, conn, records, history):
        return PersonalMemoryContext._name_history(user_id, conn, records, history)

    @staticmethod
    def _model_candidates(records):
        return PersonalMemoryContext._model_candidates(records)

    def _check_deferred_turn(self, conn, token, snapshot):
        return self._ctx._check_deferred_turn(conn, token, snapshot)

    def recall_deferred_facts(self, token, snapshot, focus, limit, *, previous_name_id=None):
        """Acquire this turn's fact readset in one consent-checked snapshot."""
        return self._ctx.recall_deferred_facts(
            token, snapshot, focus, limit, previous_name_id=previous_name_id,
        )

    def prepare_source_review(self, request, snapshot, result, reviewer):
        """Review unresolved meanings before opening the commit transaction."""
        return PersonalMemoryContext.prepare_source_review(
            request, snapshot, result, reviewer,
        )

    def _context(self, user_id, token, state, history, summary, conn):
        return self._ctx._context(user_id, token, state, history, summary, conn)

    def local_decision(self, request, snapshot):
        """Keep clear controls and replies to consent questions model-free."""
        return PersonalMemoryContext.local_decision(request, snapshot)

    def pending_question(self, user_id, conversation_id, connection=None):
        """Return a question still bound to the user's live conversation."""
        return self._ctx.pending_question(
            user_id, conversation_id, connection=connection,
        )

    def _pending_question(self, conn, user_id, conversation_id):
        return self._ctx._pending_question(conn, user_id, conversation_id)

    def assert_fresh(self, user_id, request_id):
        """Reject stale cached or queued answers without executing again."""
        return self._ctx.assert_fresh(user_id, request_id)

    @staticmethod
    def _legacy_dependencies(response):
        """Read pre-migration personalized provenance without replaying it."""
        return PersonalMemoryContext._legacy_dependencies(response)

    def _question(self, conn, token, kind, proposal, source):
        return self._ctx._question(conn, token, kind, proposal, source)

    def commit(self, request, token, snapshot, result, conn):
        """Apply memory and final answer within the caller's transaction."""
        return self._mutations.commit(request, token, snapshot, result, conn)

    def _apply(self, request, token, snapshot, result, conn, *, _automatic_insert=False):
        return self._mutations._apply(
            request, token, snapshot, result, conn,
            _automatic_insert=_automatic_insert,
        )

    @staticmethod
    def _identify_targets(records, text):
        return PersonalMemoryMutations._identify_targets(records, text)

    def _mark_prior_sources(self, conn, user_id, records):
        """Exclude earlier duplicate mentions and their dependent dialogue."""
        return self._mutations._mark_prior_sources(conn, user_id, records)
