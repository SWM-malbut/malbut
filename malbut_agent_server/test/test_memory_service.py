"""Tests for the trusted, evidence-backed memory service boundary."""

import json
import sqlite3
import threading

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import (
    MemoryConsentError,
    MemoryMutationConflictError,
    SQLiteMemoryStore,
)
from malbut_agent_server.memory_service import (
    CompletedTurnEvidence,
    ConfirmedMemoryService,
    MemoryEvidenceError,
    SQLiteConversationEvidenceValidator,
)


def _complete_turn(
    store: SQLiteConversationStore,
    user_id: str,
    conversation_id: str,
    turn_id: str,
) -> None:
    """Create a conversation if needed and commit one completed turn."""
    store.create(user_id, conversation_id)
    begin = store.begin_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        request_id=f'request-{turn_id}',
        request_fingerprint=f'fingerprint-{turn_id}',
        user_content='이 내용을 기억해 줘. 확인할게.',
    )
    assert begin.token is not None
    store.complete_turn(
        begin.token,
        assistant_content='확인되었습니다.',
        response={'decision': {'type': 'response'}},
    )


def test_completed_same_user_turn_allows_confirmed_lifecycle() -> None:
    """Only real completed turns authorize create, update, and delete."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        created = service.commit_confirmed(
            user_id='user-a',
            request_id='memory-create-1',
            content='반려견 이름은 초코',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )

        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-2')
        updated = service.update_confirmed(
            user_id='user-a',
            memory_id=created.memory_id,
            request_id='memory-update-1',
            expected_revision=1,
            content='반려견 이름은 보리',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-2',
            user_confirmed=True,
        )

        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-3')
        deleted = service.delete_confirmed(
            user_id='user-a',
            memory_id=created.memory_id,
            request_id='memory-delete-1',
            expected_revision=updated.record_revision,
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-3',
            user_confirmed=True,
        )

        assert deleted.deleted is True
        assert [
            event.evidence_turn_id
            for event in reversed(memories.list_audit_events('user-a'))
        ] == ['turn-1', 'turn-2', 'turn-3']
        events = list(reversed(memories.list_audit_events('user-a')))
        assert all(event.evidence_session_instance_id for event in events)
        assert [event.evidence_generation for event in events] == [1, 1, 1]
        assert all(event.evidence_completed_at is not None for event in events)
        assert created.evidence_session_instance_id == (
            events[0].evidence_session_instance_id
        )
        assert created.evidence_generation == 1
    finally:
        memories.close()
        conversations.close()


def test_reset_reused_turn_identity_has_distinct_provenance() -> None:
    """Generation distinguishes the same conversation and turn after reset."""
    current_time = [100.0]
    conversations = SQLiteConversationStore(
        ':memory:',
        clock=lambda: current_time[0],
    )
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        first = service.commit_confirmed(
            user_id='user-a',
            request_id='generation-one',
            content='첫 번째 세대 기억',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )

        current_time[0] = 200.0
        conversations.reset('user-a', 'conversation-a')
        begin = conversations.begin_turn(
            user_id='user-a',
            conversation_id='conversation-a',
            turn_id='turn-1',
            request_id='request-generation-two',
            request_fingerprint='fingerprint-generation-two',
            user_content='같은 turn ID의 새 세대 확인',
        )
        assert begin.token is not None
        conversations.complete_turn(
            begin.token,
            assistant_content='확인되었습니다.',
            response={'decision': {'type': 'response'}},
        )
        second = service.commit_confirmed(
            user_id='user-a',
            request_id='generation-two',
            content='두 번째 세대 기억',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )

        assert first.evidence_conversation_id == (
            second.evidence_conversation_id
        )
        assert first.evidence_turn_id == second.evidence_turn_id
        assert first.evidence_session_instance_id == (
            second.evidence_session_instance_id
        )
        assert first.evidence_generation == 1
        assert second.evidence_generation == 2
        assert first.evidence_completed_at == 100.0
        assert second.evidence_completed_at == 200.0
        events = {
            event.request_id: event
            for event in memories.list_audit_events('user-a')
        }
        assert events['generation-one'].evidence_generation == 1
        assert events['generation-two'].evidence_generation == 2
    finally:
        memories.close()
        conversations.close()


def test_deleted_and_recreated_conversation_has_distinct_instance() -> None:
    """Session-instance identity distinguishes delete and recreate reuse."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        first = service.commit_confirmed(
            user_id='user-a',
            request_id='instance-one',
            content='첫 번째 인스턴스',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        assert conversations.delete('user-a', 'conversation-a') is True
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        second = service.commit_confirmed(
            user_id='user-a',
            request_id='instance-two',
            content='두 번째 인스턴스',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )

        assert first.evidence_session_instance_id is not None
        assert second.evidence_session_instance_id is not None
        assert first.evidence_session_instance_id != (
            second.evidence_session_instance_id
        )
        assert first.evidence_generation == second.evidence_generation == 1
    finally:
        memories.close()
        conversations.close()


def test_reset_between_evidence_validation_and_mutation_is_reproducible(
) -> None:
    """Persist the verified origin while exposing the cross-store TOCTOU."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    base_validator = SQLiteConversationEvidenceValidator(conversations)

    class ResetAfterValidation:
        """Reset immediately after obtaining otherwise valid evidence."""

        def validate_completed_turn(
            self,
            user_id: str,
            conversation_id: str,
            turn_id: str,
        ) -> CompletedTurnEvidence:
            """Return generation one after making generation two current."""
            evidence = base_validator.validate_completed_turn(
                user_id,
                conversation_id,
                turn_id,
            )
            conversations.reset(user_id, conversation_id)
            return evidence

    service = ConfirmedMemoryService(memories, ResetAfterValidation())
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        result = service.commit_confirmed(
            user_id='user-a',
            request_id='deterministic-toctou',
            content='검증 직후 reset된 근거',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )

        assert conversations.get('user-a', 'conversation-a').generation == 2
        assert result.evidence_generation == 1
        assert memories.list_audit_events(
            'user-a'
        )[0].evidence_generation == 1
    finally:
        memories.close()
        conversations.close()


def test_validated_provenance_persists_in_every_mutation_artifact(
    tmp_path,
) -> None:
    """Record, audit, idempotency, and replay retain one exact origin."""
    conversation_database = tmp_path / 'provenance-conversation.sqlite3'
    memory_database = tmp_path / 'provenance-memory.sqlite3'
    conversations = SQLiteConversationStore(str(conversation_database))
    memories = SQLiteMemoryStore(str(memory_database))
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    arguments = {
        'user_id': 'user-a',
        'request_id': 'durable-provenance',
        'content': '모든 artifact에 같은 근거',
        'evidence_conversation_id': 'conversation-a',
        'evidence_turn_id': 'turn-1',
        'user_confirmed': True,
    }
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        evidence = conversations.get_completed_turn(
            'user-a',
            'conversation-a',
            'turn-1',
        )
        result = service.commit_confirmed(**arguments)
    finally:
        memories.close()
        conversations.close()

    memories = SQLiteMemoryStore(str(memory_database))
    try:
        record = memories.get_for_user('user-a', result.memory_id)
        event = memories.list_audit_events('user-a')[0]
        replay = memories.prepare_confirmed_create(**arguments).cached_result
        assert record is not None
        assert replay is not None
        for artifact in (record, event, replay):
            assert artifact.evidence_session_instance_id == (
                evidence.session_instance_id
            )
            assert artifact.evidence_generation == evidence.generation
            assert artifact.evidence_completed_at == evidence.completed_at
    finally:
        memories.close()

    connection = sqlite3.connect(memory_database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            '''
            SELECT *
            FROM memory_mutation_requests
            WHERE user_id = ? AND request_id = ?
            ''',
            ('user-a', 'durable-provenance'),
        ).fetchone()
        assert row is not None
        response = json.loads(row['response_json'])
        assert row['fingerprint_version'] == 2
        assert row['request_fingerprint'] != (
            row['request_payload_fingerprint']
        )
        assert row['evidence_session_instance_id'] == (
            evidence.session_instance_id
        )
        assert row['evidence_generation'] == evidence.generation
        assert row['evidence_completed_at'] == evidence.completed_at
        assert response['evidence_session_instance_id'] == (
            evidence.session_instance_id
        )
        assert response['evidence_generation'] == evidence.generation
        assert response['evidence_completed_at'] == evidence.completed_at
    finally:
        connection.close()


def test_service_does_not_bless_low_level_unknown_provenance() -> None:
    """A trusted compatibility write cannot become verified by replay."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    arguments = {
        'user_id': 'user-a',
        'request_id': 'unknown-provenance',
        'content': 'low-level trusted write',
        'evidence_conversation_id': 'conversation-a',
        'evidence_turn_id': 'turn-1',
        'user_confirmed': True,
    }
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        low_level = memories.commit_confirmed(**arguments)
        assert low_level.evidence_session_instance_id is None

        with pytest.raises(
            MemoryEvidenceError,
            match='no validated evidence provenance',
        ):
            service.commit_confirmed(**arguments)
        assert len(memories.list_audit_events('user-a')) == 1
    finally:
        memories.close()
        conversations.close()


def test_pending_missing_and_other_user_turns_fail_closed() -> None:
    """Uncompleted or cross-owner evidence never mutates memory state."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        conversations.create('user-a', 'conversation-a')
        pending = conversations.begin_turn(
            user_id='user-a',
            conversation_id='conversation-a',
            turn_id='turn-pending',
            request_id='pending-request',
            request_fingerprint='pending-fingerprint',
            user_content='아직 처리 중',
        )
        assert pending.token is not None

        for conversation_id, turn_id in (
            ('conversation-a', 'turn-pending'),
            ('conversation-a', 'turn-missing'),
            ('conversation-b', 'turn-b'),
        ):
            with pytest.raises(
                MemoryEvidenceError,
                match='completed confirmation turn was not found',
            ):
                service.commit_confirmed(
                    user_id='user-a',
                    request_id=f'memory-{turn_id}',
                    content='저장되면 안 되는 기억',
                    evidence_conversation_id=conversation_id,
                    evidence_turn_id=turn_id,
                    user_confirmed=True,
                )

        assert memories.revision == 0
        assert memories.list_for_user('user-a') == []
    finally:
        memories.close()
        conversations.close()


def test_other_users_completed_turn_is_not_valid_evidence() -> None:
    """A completed turn remains scoped to its authenticated user."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        _complete_turn(conversations, 'user-b', 'shared-name', 'turn-b')
        with pytest.raises(MemoryEvidenceError):
            service.commit_confirmed(
                user_id='user-a',
                request_id='cross-owner-attempt',
                content='다른 사용자 증거로 저장 금지',
                evidence_conversation_id='shared-name',
                evidence_turn_id='turn-b',
                user_confirmed=True,
            )
        assert memories.revision == 0
    finally:
        memories.close()
        conversations.close()


def test_evidence_lookup_is_exact_beyond_history_window() -> None:
    """A completed turn remains valid beyond list and prompt windows."""
    conversations = SQLiteConversationStore(
        ':memory:',
        history_limit=10,
        max_turns_per_session=1000,
    )
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        conversations.create('user-a', 'conversation-a')
        for number in range(1, 502):
            _complete_turn(
                conversations,
                'user-a',
                'conversation-a',
                f'turn-{number}',
            )
        created = service.commit_confirmed(
            user_id='user-a',
            request_id='old-evidence-create',
            content='501턴 뒤에도 검증되는 기억',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        assert memories.get_for_user(
            'user-a',
            created.memory_id,
        ) is not None
    finally:
        memories.close()
        conversations.close()


def test_consent_is_checked_before_evidence_lookup() -> None:
    """A caller without consent cannot probe conversation existence."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        with pytest.raises(MemoryConsentError):
            service.commit_confirmed(
                user_id='user-a',
                request_id='no-consent',
                content='저장 금지',
                evidence_conversation_id='unknown-conversation',
                evidence_turn_id='unknown-turn',
                user_confirmed=False,
            )
        assert memories.revision == 0
    finally:
        memories.close()
        conversations.close()


def test_exact_retry_survives_deleted_evidence_but_conflict_does_not(
    tmp_path,
) -> None:
    """Durable replay precedes evidence lookup without bypassing conflicts."""
    conversation_database = tmp_path / 'conversations.sqlite3'
    memory_database = tmp_path / 'memories.sqlite3'
    conversations = SQLiteConversationStore(str(conversation_database))
    memories = SQLiteMemoryStore(str(memory_database))
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    arguments = {
        'user_id': 'user-a',
        'request_id': 'durable-service-create',
        'content': '재시도에도 같은 기억',
        'evidence_conversation_id': 'conversation-a',
        'evidence_turn_id': 'turn-1',
        'user_confirmed': True,
    }
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        first = service.commit_confirmed(**arguments)
        assert conversations.delete('user-a', 'conversation-a') is True
    finally:
        memories.close()
        conversations.close()

    conversations = SQLiteConversationStore(str(conversation_database))
    memories = SQLiteMemoryStore(str(memory_database))
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        replay = service.commit_confirmed(**arguments)
        assert replay.cached is True
        assert replay.audit_event_id == first.audit_event_id
        assert len(memories.list_for_user('user-a')) == 1

        with pytest.raises(MemoryMutationConflictError):
            service.commit_confirmed(
                **{
                    **arguments,
                    'content': '같은 키의 다른 payload',
                }
            )
        assert len(memories.list_for_user('user-a')) == 1
    finally:
        memories.close()
        conversations.close()


def test_update_and_delete_retries_survive_evidence_reset() -> None:
    """All mutation operations preserve replay after evidence removal."""
    conversations = SQLiteConversationStore(':memory:')
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        created = service.commit_confirmed(
            user_id='user-a',
            request_id='create-for-replay',
            content='수정 전 기억',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-2')
        update_arguments = {
            'user_id': 'user-a',
            'memory_id': created.memory_id,
            'request_id': 'update-for-replay',
            'expected_revision': 1,
            'content': '수정된 기억',
            'evidence_conversation_id': 'conversation-a',
            'evidence_turn_id': 'turn-2',
            'user_confirmed': True,
        }
        updated = service.update_confirmed(**update_arguments)

        conversations.reset('user-a', 'conversation-a')
        update_replay = service.update_confirmed(**update_arguments)
        assert update_replay.cached is True
        assert update_replay.audit_event_id == updated.audit_event_id

        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-3')
        delete_arguments = {
            'user_id': 'user-a',
            'memory_id': created.memory_id,
            'request_id': 'delete-for-replay',
            'expected_revision': 2,
            'evidence_conversation_id': 'conversation-a',
            'evidence_turn_id': 'turn-3',
            'user_confirmed': True,
        }
        deleted = service.delete_confirmed(**delete_arguments)

        conversations.reset('user-a', 'conversation-a')
        delete_replay = service.delete_confirmed(**delete_arguments)
        assert delete_replay.cached is True
        assert delete_replay.audit_event_id == deleted.audit_event_id
        assert len(memories.list_audit_events('user-a')) == 3
    finally:
        memories.close()
        conversations.close()


def test_closed_or_expired_evidence_only_allows_exact_retry() -> None:
    """Stale sessions cannot authorize new mutations after their lifetime."""
    current_time = [100.0]
    conversations = SQLiteConversationStore(
        ':memory:',
        ttl_seconds=60,
        clock=lambda: current_time[0],
    )
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        _complete_turn(conversations, 'user-a', 'closed-session', 'turn-1')
        closed_arguments = {
            'user_id': 'user-a',
            'request_id': 'closed-session-create',
            'content': '닫히기 전에 저장된 기억',
            'evidence_conversation_id': 'closed-session',
            'evidence_turn_id': 'turn-1',
            'user_confirmed': True,
        }
        closed_result = service.commit_confirmed(**closed_arguments)
        conversations.close_session('user-a', 'closed-session')
        closed_replay = service.commit_confirmed(**closed_arguments)
        assert closed_replay.cached is True
        assert closed_replay.audit_event_id == closed_result.audit_event_id
        with pytest.raises(MemoryEvidenceError):
            service.commit_confirmed(
                **{
                    **closed_arguments,
                    'request_id': 'closed-session-new-request',
                }
            )

        _complete_turn(conversations, 'user-a', 'expiring-session', 'turn-2')
        expiry_arguments = {
            'user_id': 'user-a',
            'request_id': 'expiring-session-create',
            'content': '만료 전에 저장된 기억',
            'evidence_conversation_id': 'expiring-session',
            'evidence_turn_id': 'turn-2',
            'user_confirmed': True,
        }
        expiry_result = service.commit_confirmed(**expiry_arguments)
        current_time[0] = 161.0
        expiry_replay = service.commit_confirmed(**expiry_arguments)
        assert expiry_replay.cached is True
        assert expiry_replay.audit_event_id == expiry_result.audit_event_id
        with pytest.raises(MemoryEvidenceError):
            service.commit_confirmed(
                **{
                    **expiry_arguments,
                    'request_id': 'expired-session-new-request',
                }
            )
    finally:
        memories.close()
        conversations.close()


class _MismatchedValidator:
    """Return a syntactically valid but incorrectly bound evidence value."""

    def validate_completed_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> CompletedTurnEvidence:
        """Deliberately return another user's binding."""
        return CompletedTurnEvidence(
            user_id='user-b',
            conversation_id=conversation_id,
            turn_id=turn_id,
            session_instance_id='session-instance',
            generation=1,
            completed_at=100.0,
        )


def test_service_revalidates_injected_validator_binding() -> None:
    """An invalid injected result cannot cross the service boundary."""
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(memories, _MismatchedValidator())
    try:
        with pytest.raises(MemoryEvidenceError, match='did not match'):
            service.commit_confirmed(
                user_id='user-a',
                request_id='mismatch',
                content='저장 금지',
                evidence_conversation_id='conversation-a',
                evidence_turn_id='turn-a',
                user_confirmed=True,
            )
        assert memories.revision == 0
    finally:
        memories.close()


def test_memory_gate_coexists_with_shared_conversation_database(
    tmp_path,
) -> None:
    """Memory metadata must not claim SQLite's shared user_version."""
    database = tmp_path / 'shared-runtime.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute('PRAGMA user_version = 73')
    connection.close()

    conversations = SQLiteConversationStore(str(database))
    memories = SQLiteMemoryStore(str(database))
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        _complete_turn(
            conversations,
            'user-a',
            'shared-conversation',
            'shared-turn',
        )
        created = service.commit_confirmed(
            user_id='user-a',
            request_id='shared-memory-create',
            content='공유 DB에서도 안전한 기억',
            evidence_conversation_id='shared-conversation',
            evidence_turn_id='shared-turn',
            user_confirmed=True,
        )
        assert memories.get_for_user(
            'user-a',
            created.memory_id,
        ) is not None
    finally:
        memories.close()
        conversations.close()

    memories = SQLiteMemoryStore(str(database))
    conversations = SQLiteConversationStore(str(database))
    try:
        assert conversations.list_turns(
            'user-a',
            'shared-conversation',
        )[0].turn_id == 'shared-turn'
        assert memories.get_for_user(
            'user-a',
            created.memory_id,
        ) is not None
        connection = sqlite3.connect(database)
        try:
            assert connection.execute(
                'PRAGMA user_version'
            ).fetchone()[0] == 73
        finally:
            connection.close()
    finally:
        conversations.close()
        memories.close()


def test_cross_connection_service_idempotency_is_atomic(tmp_path) -> None:
    """Concurrent services commit one record and replay one result."""
    database = tmp_path / 'shared-service.sqlite3'
    conversations_a = SQLiteConversationStore(str(database))
    conversations_b = SQLiteConversationStore(str(database))
    memories_a = SQLiteMemoryStore(str(database))
    memories_b = SQLiteMemoryStore(str(database))
    service_a = ConfirmedMemoryService(
        memories_a,
        SQLiteConversationEvidenceValidator(conversations_a),
    )
    service_b = ConfirmedMemoryService(
        memories_b,
        SQLiteConversationEvidenceValidator(conversations_b),
    )
    barrier = threading.Barrier(2)
    results = []
    errors = []
    arguments = {
        'user_id': 'user-a',
        'request_id': 'concurrent-same-request',
        'content': '동시에 한 번만 저장',
        'evidence_conversation_id': 'conversation-a',
        'evidence_turn_id': 'turn-1',
        'user_confirmed': True,
    }
    try:
        _complete_turn(
            conversations_a,
            'user-a',
            'conversation-a',
            'turn-1',
        )

        def commit(service) -> None:
            try:
                barrier.wait()
                results.append(service.commit_confirmed(**arguments))
            except Exception as error:  # retain evidence for assertion
                errors.append(error)

        threads = [
            threading.Thread(target=commit, args=(service,))
            for service in (service_a, service_b)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 2
        assert {result.cached for result in results} == {False, True}
        assert len({result.memory_id for result in results}) == 1
        assert len({result.audit_event_id for result in results}) == 1
        assert len(memories_a.list_for_user('user-a')) == 1
        assert len(memories_a.list_audit_events('user-a')) == 1
    finally:
        memories_b.close()
        memories_a.close()
        conversations_b.close()
        conversations_a.close()


def test_cross_connection_request_conflict_is_atomic(tmp_path) -> None:
    """Concurrent differing payloads yield one commit and one conflict."""
    database = tmp_path / 'shared-conflict.sqlite3'
    conversations_a = SQLiteConversationStore(str(database))
    conversations_b = SQLiteConversationStore(str(database))
    memories_a = SQLiteMemoryStore(str(database))
    memories_b = SQLiteMemoryStore(str(database))
    services = (
        ConfirmedMemoryService(
            memories_a,
            SQLiteConversationEvidenceValidator(conversations_a),
        ),
        ConfirmedMemoryService(
            memories_b,
            SQLiteConversationEvidenceValidator(conversations_b),
        ),
    )
    barrier = threading.Barrier(2)
    results = []
    errors = []
    try:
        _complete_turn(
            conversations_a,
            'user-a',
            'conversation-a',
            'turn-1',
        )

        def commit(service, content: str) -> None:
            try:
                barrier.wait()
                results.append(
                    service.commit_confirmed(
                        user_id='user-a',
                        request_id='concurrent-conflict',
                        content=content,
                        evidence_conversation_id='conversation-a',
                        evidence_turn_id='turn-1',
                        user_confirmed=True,
                    )
                )
            except Exception as error:  # retain evidence for assertion
                errors.append(error)

        threads = [
            threading.Thread(
                target=commit,
                args=(service, f'서로 다른 내용 {index}'),
            )
            for index, service in enumerate(services)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], MemoryMutationConflictError)
        assert len(memories_a.list_for_user('user-a')) == 1
        assert len(memories_a.list_audit_events('user-a')) == 1
    finally:
        memories_b.close()
        memories_a.close()
        conversations_b.close()
        conversations_a.close()


class _StaticEvidenceValidator:
    """Return one configured value to exercise the trust boundary."""

    def __init__(self, evidence) -> None:
        """Store the deliberately trusted-or-untrusted result."""
        self._evidence = evidence

    def validate_completed_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
    ):
        """Return the configured result without modifying it."""
        return self._evidence


@pytest.mark.parametrize(
    ('evidence', 'message'),
    (
        (object(), 'invalid result'),
        (
            CompletedTurnEvidence(
                user_id='user-a',
                conversation_id='conversation-a',
                turn_id='turn-a',
                session_instance_id='',
                generation=1,
                completed_at=100.0,
            ),
            'evidence was incomplete',
        ),
        (
            CompletedTurnEvidence(
                user_id='user-a',
                conversation_id='conversation-a',
                turn_id='turn-a',
                session_instance_id='instance-a',
                generation=True,
                completed_at=100.0,
            ),
            'evidence was incomplete',
        ),
        (
            CompletedTurnEvidence(
                user_id='user-a',
                conversation_id='conversation-a',
                turn_id='turn-a',
                session_instance_id='instance-a',
                generation=1,
                completed_at=float('nan'),
            ),
            'evidence was incomplete',
        ),
    ),
)
def test_service_rejects_malformed_validator_results(
    evidence,
    message: str,
) -> None:
    """Injected validators cannot return malformed provenance as trusted."""
    memories = SQLiteMemoryStore(':memory:')
    service = ConfirmedMemoryService(
        memories,
        _StaticEvidenceValidator(evidence),
    )
    try:
        with pytest.raises(MemoryEvidenceError, match=message):
            service.commit_confirmed(
                user_id='user-a',
                request_id='malformed-evidence',
                content='저장되지 않을 기억',
                evidence_conversation_id='conversation-a',
                evidence_turn_id='turn-a',
                user_confirmed=True,
            )
        assert memories.revision == 0
    finally:
        memories.close()


def test_service_update_persists_explicit_expiry() -> None:
    """The prepare and commit phases preserve an explicit new expiry."""
    current_time = [100.0]
    conversations = SQLiteConversationStore(
        ':memory:',
        clock=lambda: current_time[0],
    )
    memories = SQLiteMemoryStore(
        ':memory:',
        clock=lambda: current_time[0],
    )
    service = ConfirmedMemoryService(
        memories,
        SQLiteConversationEvidenceValidator(conversations),
    )
    try:
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-1')
        created = service.commit_confirmed(
            user_id='user-a',
            request_id='expiry-create',
            content='만료 변경 전 기억',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        _complete_turn(conversations, 'user-a', 'conversation-a', 'turn-2')
        updated = service.update_confirmed(
            user_id='user-a',
            memory_id=created.memory_id,
            request_id='expiry-update',
            expected_revision=created.record_revision,
            content='만료 변경 후 기억',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-2',
            user_confirmed=True,
            expires_at=200.0,
        )

        record = memories.get_for_user('user-a', updated.memory_id)
        assert record is not None
        assert record.expires_at == 200.0
    finally:
        memories.close()
        conversations.close()
