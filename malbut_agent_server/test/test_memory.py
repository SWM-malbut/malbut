"""Tests for user-isolated SQLite memory retrieval."""

import json
import sqlite3
import stat
import threading
import time

import pytest

from malbut_agent_server.memory import (
    MEMORY_SCHEMA_VERSION,
    MEMORY_WRITER_PROTOCOL_VERSION,
    MemoryConsentError,
    MemoryMutationConflictError,
    MemoryNotFoundError,
    MemorySchemaVersionError,
    SQLiteMemoryStore,
)
from malbut_agent_server.schemas import ValidationError


def test_korean_memory_retrieval_and_user_isolation() -> None:
    """Korean particles should not prevent recall or cross user scope."""
    store = SQLiteMemoryStore(':memory:')
    try:
        store.add('user-a', '반려견 이름은 초코')
        store.add('user-b', '반려견 이름은 보리')

        result_a = store.search(
            'user-a',
            '우리 강아지 이름이 뭐였지?',
        )
        result_b = store.search(
            'user-b',
            '우리 강아지 이름이 뭐였지?',
        )

        assert [item.content for item in result_a] == [
            '반려견 이름은 초코'
        ]
        assert [item.content for item in result_b] == [
            '반려견 이름은 보리'
        ]
    finally:
        store.close()


def test_expired_memory_is_not_returned() -> None:
    """Expired records may remain stored but must not enter a prompt."""
    store = SQLiteMemoryStore(':memory:')
    try:
        store.add(
            'user-a',
            '반려견 이름은 오래된이름',
            expires_at=time.time() - 1,
        )
        assert store.search(
            'user-a',
            '반려견 이름이 뭐였지?',
        ) == []
        assert store.purge_expired() == 1
    finally:
        store.close()


def test_irrelevant_memory_is_not_returned() -> None:
    """Recency alone must not pull unrelated facts into context."""
    store = SQLiteMemoryStore(':memory:')
    try:
        store.add('user-a', '초코의 예방접종은 금요일')
        assert store.search('user-a', '오늘 날씨가 어때?') == []
    finally:
        store.close()


def test_falsey_non_object_metadata_is_rejected() -> None:
    """An empty list must not silently become an empty JSON object."""
    store = SQLiteMemoryStore(':memory:')
    try:
        with pytest.raises(ValidationError):
            store.add('user-a', '기억', metadata=[])
    finally:
        store.close()


def test_file_permissions_and_scoped_delete(tmp_path) -> None:
    """Persistent memory is private and deletion checks its owner."""
    database = tmp_path / 'memory.sqlite3'
    store = SQLiteMemoryStore(str(database))
    try:
        record = store.add('user-a', '삭제할 기억')
        mode = stat.S_IMODE(database.stat().st_mode)
        assert mode == 0o600
        assert store.delete('user-b', record.id) is False
        assert store.delete('user-a', record.id) is True
    finally:
        store.close()


def test_confirmed_mutation_lifecycle_uses_cas_and_evidence() -> None:
    """Confirmed CRUD records evidence and rejects stale revisions."""
    store = SQLiteMemoryStore(':memory:', clock=lambda: 1000.0)
    marker = 'PRIVATE_MEMORY_MARKER_강아지_초코'
    try:
        with pytest.raises(MemoryConsentError):
            store.commit_confirmed(
                user_id='user-a',
                request_id='create-without-consent',
                content=marker,
                evidence_conversation_id='conversation-1',
                evidence_turn_id='turn-1',
                user_confirmed=False,
            )

        created = store.commit_confirmed(
            user_id='user-a',
            request_id='create-1',
            content=marker,
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        assert created.operation == 'create'
        assert created.record_revision == 1
        assert created.user_revision == 1
        assert marker not in json.dumps(
            created.to_dict(),
            ensure_ascii=False,
        )
        record = store.get_for_user('user-a', created.memory_id)
        assert record is not None
        assert record.content == marker
        assert record.revision == 1
        assert record.updated_at == 1000.0
        assert record.evidence_conversation_id == 'conversation-1'
        assert record.evidence_turn_id == 'turn-1'

        updated = store.update_confirmed(
            user_id='user-a',
            memory_id=created.memory_id,
            request_id='update-1',
            expected_revision=1,
            content='강아지 이름은 보리',
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-2',
            user_confirmed=True,
            kind='fact',
        )
        assert updated.record_revision == 2
        assert updated.user_revision == 2
        record = store.get_for_user('user-a', created.memory_id)
        assert record is not None
        assert record.content == '강아지 이름은 보리'
        assert record.revision == 2
        assert record.evidence_turn_id == 'turn-2'

        with pytest.raises(MemoryMutationConflictError):
            store.update_confirmed(
                user_id='user-a',
                memory_id=created.memory_id,
                request_id='stale-update',
                expected_revision=1,
                content='오래된 CAS',
                evidence_conversation_id='conversation-1',
                evidence_turn_id='turn-3',
                user_confirmed=True,
            )

        deleted = store.delete_confirmed(
            user_id='user-a',
            memory_id=created.memory_id,
            request_id='delete-1',
            expected_revision=2,
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-3',
            user_confirmed=True,
        )
        assert deleted.deleted is True
        assert deleted.record_revision == 3
        assert deleted.user_revision == 3
        assert store.get_for_user('user-a', created.memory_id) is None

        events = store.list_audit_events('user-a')
        assert [event.operation for event in reversed(events)] == [
            'create',
            'update',
            'delete',
        ]
        assert marker not in json.dumps(
            [event.to_dict() for event in events],
            ensure_ascii=False,
        )
    finally:
        store.close()


def test_confirmed_create_is_idempotent_across_reopen(tmp_path) -> None:
    """A matching request ID replays; a changed payload conflicts."""
    database = tmp_path / 'durable-idempotency.sqlite3'
    arguments = {
        'user_id': 'user-a',
        'request_id': 'durable-create-1',
        'content': '좋아하는 음료는 보리차',
        'evidence_conversation_id': 'conversation-1',
        'evidence_turn_id': 'turn-1',
        'user_confirmed': True,
    }
    first_store = SQLiteMemoryStore(str(database))
    try:
        first = first_store.commit_confirmed(**arguments)
        replay = first_store.commit_confirmed(**arguments)
        assert replay.cached is True
        assert replay.audit_event_id == first.audit_event_id
        assert replay.memory_id == first.memory_id
        assert replay.user_revision == first.user_revision
        assert len(first_store.list_for_user('user-a')) == 1
        assert len(first_store.list_audit_events('user-a')) == 1

        with pytest.raises(MemoryMutationConflictError):
            first_store.commit_confirmed(
                **{
                    **arguments,
                    'content': '서로 다른 내용',
                }
            )
    finally:
        first_store.close()

    reopened = SQLiteMemoryStore(str(database))
    try:
        replay = reopened.commit_confirmed(**arguments)
        assert replay.cached is True
        assert replay.audit_event_id == first.audit_event_id
        assert replay.memory_id == first.memory_id
        assert reopened.user_revision('user-a') == 1
        assert reopened.revision == 1
        assert len(reopened.list_for_user('user-a')) == 1
    finally:
        reopened.close()


def test_confirmed_replay_survives_expiry_but_updates_stay_blocked() -> None:
    """Idempotent results replay while expired rows remain immutable."""
    current_time = [100.0]
    store = SQLiteMemoryStore(
        ':memory:',
        clock=lambda: current_time[0],
    )
    create_arguments = {
        'user_id': 'user-a',
        'request_id': 'expiring-create',
        'content': '만료 예정 기억',
        'evidence_conversation_id': 'conversation-1',
        'evidence_turn_id': 'turn-1',
        'user_confirmed': True,
        'expires_at': 101.0,
    }
    try:
        created = store.commit_confirmed(**create_arguments)
        current_time[0] = 102.0
        replay = store.commit_confirmed(**create_arguments)
        assert replay.cached is True
        assert replay.audit_event_id == created.audit_event_id

        for expiry in (None, 103.0):
            with pytest.raises(
                MemoryMutationConflictError,
                match='expired memory',
            ):
                store.update_confirmed(
                    user_id='user-a',
                    memory_id=created.memory_id,
                    request_id=f'expired-update-{expiry}',
                    expected_revision=1,
                    content='만료 뒤 수정 금지',
                    evidence_conversation_id='conversation-1',
                    evidence_turn_id='turn-2',
                    user_confirmed=True,
                    expires_at=expiry,
                )

        with pytest.raises(MemoryMutationConflictError):
            store.update_confirmed(
                user_id='user-a',
                memory_id=created.memory_id,
                request_id='implicit-expired-update',
                expected_revision=1,
                content='만료 뒤 수정 금지',
                evidence_conversation_id='conversation-1',
                evidence_turn_id='turn-2',
                user_confirmed=True,
            )
    finally:
        store.close()


def test_update_and_delete_retries_do_not_repeat_mutations(tmp_path) -> None:
    """Update and delete replays retain their original audit result."""
    database = tmp_path / 'mutation-retries.sqlite3'
    store = SQLiteMemoryStore(str(database))
    try:
        created = store.commit_confirmed(
            user_id='user-a',
            request_id='create-1',
            content='반려견 이름은 초코',
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        update_arguments = {
            'user_id': 'user-a',
            'memory_id': created.memory_id,
            'request_id': 'update-1',
            'expected_revision': 1,
            'content': '반려견 이름은 보리',
            'evidence_conversation_id': 'conversation-1',
            'evidence_turn_id': 'turn-2',
            'user_confirmed': True,
        }
        updated = store.update_confirmed(**update_arguments)
        update_replay = store.update_confirmed(**update_arguments)
        assert update_replay.cached is True
        assert update_replay.audit_event_id == updated.audit_event_id
        assert store.user_revision('user-a') == 2

        delete_arguments = {
            'user_id': 'user-a',
            'memory_id': created.memory_id,
            'request_id': 'delete-1',
            'expected_revision': 2,
            'evidence_conversation_id': 'conversation-1',
            'evidence_turn_id': 'turn-3',
            'user_confirmed': True,
        }
        deleted = store.delete_confirmed(**delete_arguments)
        delete_replay = store.delete_confirmed(**delete_arguments)
        assert delete_replay.cached is True
        assert delete_replay.audit_event_id == deleted.audit_event_id
        assert delete_replay.deleted is True
        assert store.user_revision('user-a') == 3
        assert len(store.list_audit_events('user-a')) == 3
    finally:
        store.close()

    reopened = SQLiteMemoryStore(str(database))
    try:
        replay = reopened.delete_confirmed(**delete_arguments)
        assert replay.cached is True
        assert replay.audit_event_id == deleted.audit_event_id
        assert reopened.user_revision('user-a') == 3
        assert reopened.get_for_user(
            'user-a',
            created.memory_id,
        ) is None
    finally:
        reopened.close()


def test_two_connections_share_revisions_and_enforce_cas(tmp_path) -> None:
    """Persistent revisions expose cross-connection writes after reopen."""
    database = tmp_path / 'two-connections.sqlite3'
    first = SQLiteMemoryStore(str(database), clock=lambda: 1000.0)
    second = SQLiteMemoryStore(str(database), clock=lambda: 1001.0)
    try:
        before = second.revision
        created = first.commit_confirmed(
            user_id='user-a',
            request_id='create-1',
            content='반려견 이름은 초코',
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        assert second.revision == before + 1
        assert second.user_revision('user-a') == 1
        observed = second.get_for_user('user-a', created.memory_id)
        assert observed is not None
        assert observed.revision == 1

        updated = second.update_confirmed(
            user_id='user-a',
            memory_id=created.memory_id,
            request_id='update-1',
            expected_revision=1,
            content='반려견 이름은 보리',
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-2',
            user_confirmed=True,
        )
        assert updated.record_revision == 2
        assert first.revision == before + 2
        assert first.user_revision('user-a') == 2

        with pytest.raises(MemoryMutationConflictError):
            first.update_confirmed(
                user_id='user-a',
                memory_id=created.memory_id,
                request_id='stale-update',
                expected_revision=1,
                content='오래된 값',
                evidence_conversation_id='conversation-1',
                evidence_turn_id='turn-3',
                user_confirmed=True,
            )
    finally:
        second.close()
        first.close()

    reopened = SQLiteMemoryStore(str(database))
    try:
        assert reopened.revision == before + 2
        assert reopened.user_revision('user-a') == 2
        observed = reopened.get_for_user('user-a', created.memory_id)
        assert observed is not None
        assert observed.content == '반려견 이름은 보리'
        assert observed.revision == 2
    finally:
        reopened.close()


def test_owner_snapshot_ignores_other_users_and_detects_expiry() -> None:
    """Inference fences only its owner but still notice time expiry."""
    current_time = [100.0]
    store = SQLiteMemoryStore(
        ':memory:',
        clock=lambda: current_time[0],
    )
    try:
        record = store.add(
            'user-a',
            '반려견 이름은 초코',
            expires_at=101.0,
            created_at=99.0,
        )
        records, owner_revision = store.search_with_owner_revision(
            'user-a',
            '반려견 이름이 뭐였지?',
        )
        assert [item.id for item in records] == [record.id]
        assert store.owner_snapshot_is_current(
            'user-a',
            owner_revision,
            records,
        ) is True

        store.add('user-b', '다른 사용자의 기억')
        assert store.owner_snapshot_is_current(
            'user-a',
            owner_revision,
            records,
        ) is True

        current_time[0] = 101.0
        assert store.owner_snapshot_is_current(
            'user-a',
            owner_revision,
            records,
        ) is False
    finally:
        store.close()


def test_owner_snapshot_is_stable_across_connections_and_gate(
    tmp_path,
) -> None:
    """Cross-process writers either advance revision or are rejected."""
    database = tmp_path / 'cross-process-snapshot.sqlite3'
    current_time = [100.0]
    first = SQLiteMemoryStore(
        str(database),
        clock=lambda: current_time[0],
    )
    second = SQLiteMemoryStore(
        str(database),
        clock=lambda: current_time[0],
    )
    try:
        record = first.add(
            'user-a',
            '반려견 이름은 초코',
            expires_at=101.0,
            created_at=99.0,
        )
        records, revision = first.search_with_owner_revision(
            'user-a',
            '반려견 이름이 뭐였지?',
        )

        second.add('user-b', '다른 사용자 기억')
        assert first.owner_snapshot_is_current(
            'user-a',
            revision,
            records,
        ) is True

        unmanaged = sqlite3.connect(database)
        try:
            with pytest.raises(sqlite3.OperationalError):
                unmanaged.execute(
                    "UPDATE memories SET content = 'bypass' WHERE id = ?",
                    (record.id,),
                )
            unmanaged.rollback()
        finally:
            unmanaged.close()
        assert first.owner_snapshot_is_current(
            'user-a',
            revision,
            records,
        ) is True

        second.delete('user-a', record.id)
        assert first.owner_snapshot_is_current(
            'user-a',
            revision,
            records,
        ) is False

        expiring = first.add(
            'user-a',
            '만료 예정 기억',
            expires_at=101.0,
            created_at=99.0,
        )
        records, revision = first.search_with_owner_revision(
            'user-a',
            '만료 예정 기억',
        )
        assert [item.id for item in records] == [expiring.id]
        current_time[0] = 101.0
        assert first.owner_snapshot_is_current(
            'user-a',
            revision,
            records,
        ) is False
    finally:
        second.close()
        first.close()


def test_confirmed_mutations_do_not_cross_user_scope() -> None:
    """Another user cannot observe, update, delete, or audit a record."""
    store = SQLiteMemoryStore(':memory:')
    try:
        created = store.commit_confirmed(
            user_id='user-a',
            request_id='create-a',
            content='사용자 A의 비공개 기억',
            evidence_conversation_id='conversation-a',
            evidence_turn_id='turn-a',
            user_confirmed=True,
        )
        assert store.get_for_user('user-b', created.memory_id) is None
        assert store.search('user-b', '비공개') == []
        assert store.list_audit_events('user-b') == []
        assert store.user_revision('user-b') == 0

        with pytest.raises(MemoryNotFoundError):
            store.update_confirmed(
                user_id='user-b',
                memory_id=created.memory_id,
                request_id='update-b',
                expected_revision=1,
                content='탈취 시도',
                evidence_conversation_id='conversation-b',
                evidence_turn_id='turn-b',
                user_confirmed=True,
            )
        with pytest.raises(MemoryNotFoundError):
            store.delete_confirmed(
                user_id='user-b',
                memory_id=created.memory_id,
                request_id='delete-b',
                expected_revision=1,
                evidence_conversation_id='conversation-b',
                evidence_turn_id='turn-b',
                user_confirmed=True,
            )
        assert store.get_for_user('user-a', created.memory_id) is not None
        assert store.user_revision('user-b') == 0
    finally:
        store.close()


def test_version_one_database_is_migrated_without_data_loss(
    tmp_path,
) -> None:
    """Opening the old schema backfills version and evidence columns."""
    database = tmp_path / 'version-one.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute(
        '''
        CREATE TABLE memories (
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
    connection.execute(
        '''
        INSERT INTO memories (
            id, user_id, kind, content, source, confidence,
            created_at, expires_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            'legacy-memory',
            'user-a',
            'fact',
            '이전 스키마 기억',
            'legacy',
            1.0,
            100.0,
            None,
            '{}',
        ),
    )
    connection.commit()
    connection.close()

    store = SQLiteMemoryStore(str(database), clock=lambda: 1000.0)
    try:
        record = store.get_for_user('user-a', 'legacy-memory')
        assert record is not None
        assert record.content == '이전 스키마 기억'
        assert record.revision == 1
        assert record.updated_at == 100.0
        assert record.evidence_conversation_id is None
        assert record.evidence_turn_id is None
        assert record.evidence_session_instance_id is None
        assert record.evidence_generation is None
        assert record.evidence_completed_at is None
        assert store.revision == 1
        assert store.user_revision('user-a') == 1

        result = store.update_confirmed(
            user_id='user-a',
            memory_id='legacy-memory',
            request_id='migrated-update',
            expected_revision=1,
            content='마이그레이션 후 수정',
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        assert result.record_revision == 2
        assert result.user_revision == 2
    finally:
        store.close()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            '''
            SELECT schema_version,
                   min_writer_protocol,
                   max_writer_protocol
            FROM memory_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone() == (
            MEMORY_SCHEMA_VERSION,
            MEMORY_WRITER_PROTOCOL_VERSION,
            MEMORY_WRITER_PROTOCOL_VERSION,
        )
    finally:
        connection.close()


def test_low_level_confirmed_compatibility_marks_unknown_provenance() -> None:
    """Trusted low-level callers retain explicit unknown provenance."""
    store = SQLiteMemoryStore(':memory:', clock=lambda: 1000.0)
    try:
        result = store.commit_confirmed(
            user_id='user-a',
            request_id='trusted-low-level',
            content='trusted adapter compatibility',
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
        record = store.get_for_user('user-a', result.memory_id)
        event = store.list_audit_events('user-a')[0]

        assert record is not None
        assert result.evidence_session_instance_id is None
        assert result.evidence_generation is None
        assert result.evidence_completed_at is None
        assert record.evidence_session_instance_id is None
        assert event.evidence_session_instance_id is None
        assert result.to_dict()['evidence_session_instance_id'] is None
    finally:
        store.close()


def test_provenance_is_part_of_durable_idempotency_fingerprint() -> None:
    """The same request cannot be rebound to a different turn instance."""
    store = SQLiteMemoryStore(':memory:')
    arguments = {
        'user_id': 'user-a',
        'request_id': 'bound-request',
        'content': 'bound memory',
        'evidence_conversation_id': 'conversation-1',
        'evidence_turn_id': 'turn-1',
        'user_confirmed': True,
        'evidence_session_instance_id': 'instance-1',
        'evidence_generation': 1,
        'evidence_completed_at': 100.0,
    }
    try:
        first = store.commit_confirmed(**arguments)
        replay = store.commit_confirmed(**arguments)
        assert replay.cached is True
        assert replay.evidence_session_instance_id == 'instance-1'
        assert replay.audit_event_id == first.audit_event_id

        with pytest.raises(MemoryMutationConflictError):
            store.commit_confirmed(
                **{
                    **arguments,
                    'evidence_session_instance_id': 'instance-2',
                }
            )
        assert len(store.list_audit_events('user-a')) == 1
    finally:
        store.close()


def test_version_two_writer_gate_is_upgraded_atomically(tmp_path) -> None:
    """The previous gate version migrates and rejects its old writer."""
    database = tmp_path / 'version-two-gate.sqlite3'
    store = SQLiteMemoryStore(str(database))
    try:
        store.add('user-a', 'upgrade survivor')
    finally:
        store.close()

    connection = sqlite3.connect(database)
    connection.create_function(
        'memory_writer_protocol_version',
        0,
        lambda: MEMORY_WRITER_PROTOCOL_VERSION,
    )
    try:
        connection.execute(
            '''
            UPDATE memory_schema_metadata
            SET schema_version = 2,
                min_writer_protocol = 2,
                max_writer_protocol = 2
            WHERE singleton = 1
            '''
        )
        connection.commit()
    finally:
        connection.close()

    migrated = SQLiteMemoryStore(str(database))
    try:
        assert migrated.list_for_user('user-a')[0].content == (
            'upgrade survivor'
        )
    finally:
        migrated.close()

    connection = sqlite3.connect(database)
    connection.create_function(
        'memory_writer_protocol_version',
        0,
        lambda: 2,
    )
    try:
        metadata = connection.execute(
            '''
            SELECT schema_version, min_writer_protocol, max_writer_protocol
            FROM memory_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        assert tuple(metadata) == (
            MEMORY_SCHEMA_VERSION,
            MEMORY_WRITER_PROTOCOL_VERSION,
            MEMORY_WRITER_PROTOCOL_VERSION,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match='incompatible memory writer protocol',
        ):
            connection.execute(
                "UPDATE memories SET kind = 'old-writer' WHERE user_id = ?",
                ('user-a',),
            )
    finally:
        connection.close()


def test_real_version_two_tables_are_migrated_with_legacy_rows(
    tmp_path,
) -> None:
    """A real v2 layout gains every v3 column before its gate returns."""
    database = tmp_path / 'real-version-two.sqlite3'
    connection = sqlite3.connect(database)
    connection.executescript(
        '''
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL,
            metadata_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL,
            evidence_conversation_id TEXT,
            evidence_turn_id TEXT
        );
        CREATE TABLE memory_mutation_requests (
            user_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (user_id, request_id)
        );
        CREATE TABLE memory_audit_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            request_id TEXT,
            record_revision_before INTEGER NOT NULL,
            record_revision_after INTEGER NOT NULL,
            user_revision INTEGER NOT NULL,
            global_revision INTEGER NOT NULL,
            occurred_at REAL NOT NULL,
            evidence_conversation_id TEXT,
            evidence_turn_id TEXT
        );
        CREATE TABLE memory_schema_metadata (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            min_writer_protocol INTEGER NOT NULL,
            max_writer_protocol INTEGER NOT NULL,
            migrated_at REAL NOT NULL
        );
        INSERT INTO memory_schema_metadata
        VALUES (1, 2, 2, 2, 100.0);
        INSERT INTO memory_mutation_requests
        VALUES (
            'user-a', 'legacy-request', 'create',
            'legacy-fingerprint', '{}', 100.0
        );
        INSERT INTO memory_audit_events
        VALUES (
            'legacy-event', 'user-a', 'legacy-memory', 'create',
            'legacy-request', 0, 1, 1, 1, 100.0,
            'conversation-a', 'turn-a'
        );
        CREATE TRIGGER memory_writer_gate_memory_mutation_requests_update
        BEFORE UPDATE ON memory_mutation_requests
        BEGIN
            SELECT CASE
                WHEN memory_writer_protocol_version() != 2
                THEN RAISE(
                    ABORT,
                    'incompatible memory writer protocol'
                )
            END;
        END;
        '''
    )
    connection.commit()
    connection.close()

    store = SQLiteMemoryStore(str(database))
    store.close()

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        request = connection.execute(
            '''
            SELECT request_payload_fingerprint,
                   fingerprint_version,
                   evidence_session_instance_id,
                   evidence_generation,
                   evidence_completed_at
            FROM memory_mutation_requests
            WHERE request_id = 'legacy-request'
            '''
        ).fetchone()
        audit = connection.execute(
            '''
            SELECT evidence_session_instance_id,
                   evidence_generation,
                   evidence_completed_at
            FROM memory_audit_events
            WHERE event_id = 'legacy-event'
            '''
        ).fetchone()
        assert tuple(request) == (
            'legacy-fingerprint',
            1,
            None,
            None,
            None,
        )
        assert tuple(audit) == (None, None, None)
        metadata = connection.execute(
            '''
            SELECT schema_version, min_writer_protocol, max_writer_protocol
            FROM memory_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        assert tuple(metadata) == (
            MEMORY_SCHEMA_VERSION,
            MEMORY_WRITER_PROTOCOL_VERSION,
            MEMORY_WRITER_PROTOCOL_VERSION,
        )
    finally:
        connection.close()

    legacy = sqlite3.connect(database)
    legacy.create_function(
        'memory_writer_protocol_version',
        0,
        lambda: 2,
    )
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match='incompatible memory writer protocol',
        ):
            legacy.execute(
                '''
                UPDATE memory_mutation_requests
                SET created_at = 101.0
                WHERE request_id = 'legacy-request'
                '''
            )
    finally:
        legacy.close()


def test_missing_schema_metadata_singleton_fails_closed(tmp_path) -> None:
    """A metadata table without its singleton is corruption, not v1."""
    database = tmp_path / 'missing-memory-metadata.sqlite3'
    store = SQLiteMemoryStore(str(database))
    store.close()

    connection = sqlite3.connect(database)
    connection.create_function(
        'memory_writer_protocol_version',
        0,
        lambda: MEMORY_WRITER_PROTOCOL_VERSION,
    )
    connection.execute('DELETE FROM memory_schema_metadata')
    connection.commit()
    connection.close()

    with pytest.raises(
        MemorySchemaVersionError,
        match='metadata is incomplete',
    ):
        SQLiteMemoryStore(str(database))


def test_version_two_metadata_rejects_a_non_v2_writer_scope(
    tmp_path,
) -> None:
    """A v2 schema cannot be migrated if its own writer was excluded."""
    database = tmp_path / 'invalid-v2-writer-scope.sqlite3'
    store = SQLiteMemoryStore(str(database))
    store.close()

    connection = sqlite3.connect(database)
    connection.create_function(
        'memory_writer_protocol_version',
        0,
        lambda: MEMORY_WRITER_PROTOCOL_VERSION,
    )
    connection.execute(
        '''
        UPDATE memory_schema_metadata
        SET schema_version = 2,
            min_writer_protocol = 3,
            max_writer_protocol = 3
        WHERE singleton = 1
        '''
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        MemorySchemaVersionError,
        match='writer protocol',
    ):
        SQLiteMemoryStore(str(database))


@pytest.mark.parametrize(
    ('corruption', 'expected_message'),
    [
        ('invalid-json', 'stored memory mutation response is invalid'),
        ('non-object', 'stored memory mutation response is invalid'),
        ('partial-provenance', 'stored memory mutation result is invalid'),
        ('invalid-provenance', 'stored memory mutation result is invalid'),
    ],
)
def test_corrupt_idempotency_response_fails_closed(
    tmp_path,
    corruption,
    expected_message,
) -> None:
    """A persisted replay must remain valid JSON with valid provenance."""
    database = tmp_path / f'corrupt-response-{corruption}.sqlite3'
    arguments = {
        'user_id': 'user-a',
        'request_id': 'corrupt-response',
        'content': '검증된 기억',
        'evidence_conversation_id': 'conversation-a',
        'evidence_turn_id': 'turn-a',
        'user_confirmed': True,
        'evidence_session_instance_id': 'instance-a',
        'evidence_generation': 1,
        'evidence_completed_at': 100.0,
    }
    store = SQLiteMemoryStore(str(database), clock=lambda: 101.0)
    try:
        response = store.commit_confirmed(**arguments).to_stored_dict()
    finally:
        store.close()

    if corruption == 'invalid-json':
        response_json = '{'
    elif corruption == 'non-object':
        response_json = '[]'
    else:
        if corruption == 'partial-provenance':
            response['evidence_completed_at'] = None
        else:
            response['evidence_session_instance_id'] = ''
        response_json = json.dumps(response)

    connection = sqlite3.connect(database)
    connection.create_function(
        'memory_writer_protocol_version',
        0,
        lambda: MEMORY_WRITER_PROTOCOL_VERSION,
    )
    connection.execute(
        '''
        UPDATE memory_mutation_requests
        SET response_json = ?
        WHERE user_id = ? AND request_id = ?
        ''',
        (response_json, 'user-a', 'corrupt-response'),
    )
    connection.commit()
    connection.close()

    reopened = SQLiteMemoryStore(str(database), clock=lambda: 101.0)
    try:
        with pytest.raises(RuntimeError, match=expected_message):
            reopened.prepare_confirmed_create(
                user_id=arguments['user_id'],
                request_id=arguments['request_id'],
                content=arguments['content'],
                evidence_conversation_id=(
                    arguments['evidence_conversation_id']
                ),
                evidence_turn_id=arguments['evidence_turn_id'],
                user_confirmed=True,
            )
    finally:
        reopened.close()


def test_idempotency_provenance_column_mismatch_fails_closed(
    tmp_path,
) -> None:
    """Replay rejects disagreement between its row and stored result."""
    database = tmp_path / 'mismatched-provenance.sqlite3'
    arguments = {
        'user_id': 'user-a',
        'request_id': 'mismatched-provenance',
        'content': '검증된 기억',
        'evidence_conversation_id': 'conversation-a',
        'evidence_turn_id': 'turn-a',
        'user_confirmed': True,
        'evidence_session_instance_id': 'instance-a',
        'evidence_generation': 1,
        'evidence_completed_at': 100.0,
    }
    store = SQLiteMemoryStore(str(database), clock=lambda: 101.0)
    try:
        store.commit_confirmed(**arguments)
    finally:
        store.close()

    connection = sqlite3.connect(database)
    connection.create_function(
        'memory_writer_protocol_version',
        0,
        lambda: MEMORY_WRITER_PROTOCOL_VERSION,
    )
    connection.execute(
        '''
        UPDATE memory_mutation_requests
        SET evidence_generation = 2
        WHERE user_id = ? AND request_id = ?
        ''',
        ('user-a', 'mismatched-provenance'),
    )
    connection.commit()
    connection.close()

    reopened = SQLiteMemoryStore(str(database), clock=lambda: 101.0)
    try:
        with pytest.raises(RuntimeError, match='provenance is inconsistent'):
            reopened.commit_confirmed(**arguments)
        with pytest.raises(RuntimeError, match='provenance is inconsistent'):
            reopened.prepare_confirmed_create(
                user_id=arguments['user_id'],
                request_id=arguments['request_id'],
                content=arguments['content'],
                evidence_conversation_id=(
                    arguments['evidence_conversation_id']
                ),
                evidence_turn_id=arguments['evidence_turn_id'],
                user_confirmed=True,
            )
    finally:
        reopened.close()


def test_incompatible_schema_is_rejected_without_stranding_lock(
    tmp_path,
) -> None:
    """A runtime must fail closed on incompatible memory metadata."""
    database = tmp_path / 'future-schema.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute(
        '''
        CREATE TABLE memory_schema_metadata (
            singleton INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            min_writer_protocol INTEGER NOT NULL,
            max_writer_protocol INTEGER NOT NULL,
            migrated_at REAL NOT NULL
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO memory_schema_metadata
        VALUES (1, ?, ?, ?, 100.0)
        ''',
        (
            MEMORY_SCHEMA_VERSION + 1,
            MEMORY_WRITER_PROTOCOL_VERSION + 1,
            MEMORY_WRITER_PROTOCOL_VERSION + 1,
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        MemorySchemaVersionError,
        match='schema is incompatible',
    ):
        SQLiteMemoryStore(str(database))

    connection = sqlite3.connect(database, timeout=0.1)
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.rollback()
    finally:
        connection.close()


def test_incompatible_writer_metadata_is_rejected(tmp_path) -> None:
    """A persisted writer range excludes an incompatible runtime."""
    database = tmp_path / 'writer-range.sqlite3'
    store = SQLiteMemoryStore(str(database))
    store.close()

    connection = sqlite3.connect(database)
    connection.create_function(
        'memory_writer_protocol_version',
        0,
        lambda: MEMORY_WRITER_PROTOCOL_VERSION,
    )
    connection.execute(
        '''
        UPDATE memory_schema_metadata
        SET min_writer_protocol = ?, max_writer_protocol = ?
        WHERE singleton = 1
        ''',
        (
            MEMORY_WRITER_PROTOCOL_VERSION + 1,
            MEMORY_WRITER_PROTOCOL_VERSION + 1,
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        MemorySchemaVersionError,
        match='writer protocol',
    ):
        SQLiteMemoryStore(str(database))


def test_writer_gate_blocks_unmanaged_and_legacy_connections(
    tmp_path,
) -> None:
    """Database triggers reject raw SQL and mixed-version writers."""
    database = tmp_path / 'writer-gate.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute(
        '''
        CREATE TABLE unrelated_shared_state (
            id INTEGER PRIMARY KEY,
            value TEXT NOT NULL
        )
        '''
    )
    connection.commit()
    preexisting_unmanaged = sqlite3.connect(database)
    connection.close()
    store = SQLiteMemoryStore(str(database), clock=lambda: 100.0)
    try:
        record = store.add('user-a', 'writer gate original')

        try:
            with pytest.raises(
                sqlite3.OperationalError,
                match='memory_writer_protocol_version',
            ):
                preexisting_unmanaged.execute(
                    'DELETE FROM memories WHERE id = ?',
                    (record.id,),
                )
            preexisting_unmanaged.rollback()
            preexisting_unmanaged.execute(
                '''
                INSERT INTO unrelated_shared_state (value)
                VALUES ('still writable')
                '''
            )
            preexisting_unmanaged.commit()
        finally:
            preexisting_unmanaged.close()

        unmanaged = sqlite3.connect(database)
        try:
            with pytest.raises(
                sqlite3.OperationalError,
                match='memory_writer_protocol_version',
            ):
                unmanaged.execute(
                    'DELETE FROM memories WHERE id = ?',
                    (record.id,),
                )
            unmanaged.rollback()
        finally:
            unmanaged.close()

        legacy = sqlite3.connect(database)
        legacy.create_function(
            'memory_writer_protocol_version',
            0,
            lambda: MEMORY_WRITER_PROTOCOL_VERSION - 1,
        )
        try:
            with pytest.raises(
                sqlite3.IntegrityError,
                match='incompatible memory writer protocol',
            ):
                legacy.execute(
                    '''
                    UPDATE memories
                    SET content = 'legacy overwrite'
                    WHERE id = ?
                    ''',
                    (record.id,),
                )
            legacy.rollback()
        finally:
            legacy.close()

        observed = store.get_for_user('user-a', record.id)
        assert observed is not None
        assert observed.content == 'writer gate original'
    finally:
        store.close()


def test_concurrent_version_one_open_migrates_once(tmp_path) -> None:
    """Concurrent first opens serialize schema inspection and ALTERs."""
    database = tmp_path / 'concurrent-version-one.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute(
        '''
        CREATE TABLE memories (
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
    connection.commit()
    connection.close()

    barrier = threading.Barrier(8)
    errors = []

    def open_store() -> None:
        try:
            barrier.wait()
            store = SQLiteMemoryStore(str(database))
            store.close()
        except Exception as error:  # evidence retains the actual exception
            errors.append(error)

    threads = [threading.Thread(target=open_store) for _index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors, repr(errors)
    migrated = SQLiteMemoryStore(str(database))
    migrated.close()
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row['name'])
            for row in connection.execute(
                'PRAGMA table_info(memories)'
            ).fetchall()
        }
        assert {
            'revision',
            'updated_at',
            'evidence_conversation_id',
            'evidence_turn_id',
        } <= columns
    finally:
        connection.close()


def test_failed_migration_rolls_back_writer_lock(tmp_path) -> None:
    """A malformed legacy schema cannot strand a SQLite writer lock."""
    database = tmp_path / 'malformed-legacy.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE memories (id TEXT PRIMARY KEY)')
    connection.commit()
    connection.close()

    retained_errors = []
    with pytest.raises(sqlite3.OperationalError) as first_error:
        SQLiteMemoryStore(str(database))
    retained_errors.append(first_error.value)

    connection = sqlite3.connect(database, timeout=0.1)
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.rollback()
    finally:
        connection.close()
    assert len(retained_errors) == 1


def test_expiry_boundary_and_purge_advance_durable_revision() -> None:
    """The exact expiry instant hides and audibly purges a record."""
    store = SQLiteMemoryStore(':memory:', clock=lambda: 100.0)
    try:
        expired = store.add(
            'user-a',
            '만료 경계 기억',
            expires_at=100.0,
            created_at=99.0,
        )
        before = store.revision
        assert store.get_for_user(
            'user-a',
            expired.id,
            now=100.0,
        ) is None
        assert store.list_for_user('user-a', now=100.0) == []
        assert store.purge_expired(now=100.0) == 1
        assert store.revision == before + 1
        assert store.user_revision('user-a') == 2
        assert store.list_audit_events('user-a')[0].operation == (
            'expire_purge'
        )

        with pytest.raises(ValidationError):
            store.commit_confirmed(
                user_id='user-a',
                request_id='already-expired',
                content='이미 만료',
                evidence_conversation_id='conversation-1',
                evidence_turn_id='turn-1',
                user_confirmed=True,
                expires_at=100.0,
            )
    finally:
        store.close()


def test_audit_and_idempotency_tables_do_not_duplicate_content(
    tmp_path,
) -> None:
    """Operational evidence stores hashes and IDs, not memory content."""
    database = tmp_path / 'content-free-audit.sqlite3'
    marker = 'NEVER_COPY_THIS_MEMORY_CONTENT_92741'
    store = SQLiteMemoryStore(str(database))
    try:
        store.commit_confirmed(
            user_id='user-a',
            request_id='create-marker',
            content=marker,
            evidence_conversation_id='conversation-1',
            evidence_turn_id='turn-1',
            user_confirmed=True,
        )
    finally:
        store.close()

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        audit_columns = {
            str(row['name'])
            for row in connection.execute(
                'PRAGMA table_info(memory_audit_events)'
            ).fetchall()
        }
        assert 'content' not in audit_columns
        assert 'metadata_json' not in audit_columns
        audit_values = [
            dict(row)
            for row in connection.execute(
                'SELECT * FROM memory_audit_events'
            ).fetchall()
        ]
        request_values = [
            dict(row)
            for row in connection.execute(
                'SELECT * FROM memory_mutation_requests'
            ).fetchall()
        ]
        operational_json = json.dumps(
            {
                'audit': audit_values,
                'idempotency': request_values,
            },
            ensure_ascii=False,
        )
        assert marker not in operational_json
    finally:
        connection.close()


@pytest.mark.parametrize(
    ('request_id', 'message'),
    (
        (None, 'request_id must be a string'),
        ('   ', 'request_id must not be empty'),
        ('x' * 129, 'request_id must be at most'),
        ('request\ncontrol', 'request_id must not contain control'),
    ),
)
def test_confirmed_create_rejects_malformed_request_identifiers(
    request_id,
    message: str,
) -> None:
    """Mutation identifiers reject ambiguous or unsafe boundary values."""
    store = SQLiteMemoryStore(':memory:')
    try:
        with pytest.raises(ValidationError, match=message):
            store.commit_confirmed(
                user_id='user-a',
                request_id=request_id,
                content='저장되지 않을 기억',
                evidence_conversation_id='conversation-a',
                evidence_turn_id='turn-a',
                user_confirmed=True,
            )
        assert store.revision == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    ('overrides', 'message'),
    (
        ({'content': ''}, 'memory content must not be empty'),
        ({'content': 'x' * 4001}, 'memory content is too long'),
        ({'kind': ''}, 'memory kind is invalid'),
        ({'source': 'x' * 65}, 'memory source is invalid'),
        ({'confidence': True}, 'memory confidence must be a number'),
        ({'confidence': 1.01}, 'memory confidence must be between'),
        ({'expires_at': 'tomorrow'}, 'expires_at must be a number'),
        ({'expires_at': float('nan')}, 'expires_at must be finite'),
        ({'metadata': {'value': float('nan')}}, 'finite JSON values'),
        ({'metadata': {'value': 'x' * 8001}}, 'metadata is too large'),
        ({'created_at': False}, 'timestamp must be a number'),
        ({'created_at': float('inf')}, 'timestamp must be finite'),
    ),
)
def test_trusted_add_rejects_invalid_record_boundaries(
    overrides,
    message: str,
) -> None:
    """Trusted compatibility writes still enforce every record boundary."""
    store = SQLiteMemoryStore(':memory:')
    arguments = {
        'user_id': 'user-a',
        'content': '유효한 기본 기억',
        'kind': 'fact',
        'source': 'user_verified',
        'confidence': 1.0,
    }
    arguments.update(overrides)
    try:
        with pytest.raises(ValidationError, match=message):
            store.add(**arguments)
        assert store.revision == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    ('query', 'limit', 'message'),
    (
        ('', 5, 'memory query must not be empty'),
        ('기억', True, 'memory search limit must be an integer'),
        ('기억', 0, 'memory search limit must be between'),
        ('기억', 11, 'memory search limit must be between'),
    ),
)
def test_search_rejects_invalid_query_and_limit(
    query,
    limit,
    message: str,
) -> None:
    """Retrieval rejects empty questions and out-of-contract limits."""
    store = SQLiteMemoryStore(':memory:')
    try:
        with pytest.raises(ValidationError, match=message):
            store.search('user-a', query, limit=limit)
    finally:
        store.close()


@pytest.mark.parametrize('limit', (True, 0, 501))
def test_audit_listing_rejects_invalid_limits(limit) -> None:
    """Audit reads keep integer and bounded pagination semantics."""
    store = SQLiteMemoryStore(':memory:')
    try:
        with pytest.raises(ValidationError, match='audit limit'):
            store.list_audit_events('user-a', limit=limit)
    finally:
        store.close()


@pytest.mark.parametrize(
    ('instance_id', 'generation', 'completed_at', 'message'),
    (
        ('instance-a', None, None, 'complete or entirely unknown'),
        ('instance-a', True, 100.0, 'positive integer'),
        ('instance-a', 0, 100.0, 'positive integer'),
        ('instance-a', 1, True, 'completed_at must be finite'),
        ('instance-a', 1, float('nan'), 'completed_at must be finite'),
    ),
)
def test_confirmed_create_rejects_invalid_evidence_provenance(
    instance_id,
    generation,
    completed_at,
    message: str,
) -> None:
    """Evidence provenance is accepted only when complete and well typed."""
    store = SQLiteMemoryStore(':memory:')
    try:
        with pytest.raises(ValidationError, match=message):
            store.commit_confirmed(
                user_id='user-a',
                request_id='invalid-provenance',
                content='저장되지 않을 기억',
                evidence_conversation_id='conversation-a',
                evidence_turn_id='turn-a',
                user_confirmed=True,
                evidence_session_instance_id=instance_id,
                evidence_generation=generation,
                evidence_completed_at=completed_at,
            )
        assert store.revision == 0
    finally:
        store.close()


@pytest.mark.parametrize('expected_revision', (False, 0))
def test_confirmed_update_rejects_invalid_expected_revision(
    expected_revision,
) -> None:
    """CAS revisions reject booleans and non-positive integers."""
    store = SQLiteMemoryStore(':memory:')
    try:
        with pytest.raises(ValidationError, match='expected_revision'):
            store.update_confirmed(
                user_id='user-a',
                memory_id='memory-a',
                request_id='update-invalid-revision',
                expected_revision=expected_revision,
                content='수정되지 않을 기억',
                evidence_conversation_id='conversation-a',
                evidence_turn_id='turn-a',
                user_confirmed=True,
            )
    finally:
        store.close()


@pytest.mark.parametrize('content', ('', 'x' * 4001))
def test_update_and_prepare_reject_invalid_content(content: str) -> None:
    """Both mutation phases enforce the same content-size contract."""
    store = SQLiteMemoryStore(':memory:')
    arguments = {
        'user_id': 'user-a',
        'memory_id': 'memory-a',
        'request_id': 'invalid-update-content',
        'expected_revision': 1,
        'content': content,
        'evidence_conversation_id': 'conversation-a',
        'evidence_turn_id': 'turn-a',
        'user_confirmed': True,
    }
    try:
        with pytest.raises(ValidationError, match='memory content'):
            store.prepare_confirmed_update(**arguments)
        with pytest.raises(ValidationError, match='memory content'):
            store.update_confirmed(**arguments)
        assert store.revision == 0
    finally:
        store.close()


def test_owner_snapshot_rejects_invalid_revision_and_records() -> None:
    """Snapshot validation fails before querying with untrusted evidence."""
    store = SQLiteMemoryStore(':memory:')
    own_record = store.add('user-a', '내 기억')
    other_record = store.add('user-b', '다른 사용자의 기억')
    try:
        with pytest.raises(ValidationError, match='owner revision'):
            store.owner_snapshot_is_current('user-a', False, [])
        with pytest.raises(ValidationError, match='MemoryRecord'):
            store.owner_snapshot_is_current('user-a', 1, [object()])
        with pytest.raises(ValidationError, match='owner does not match'):
            store.owner_snapshot_is_current(
                'user-a',
                1,
                [other_record],
            )
        assert store.owner_snapshot_is_current(
            'user-a',
            store.user_revision('user-a'),
            [own_record],
        )
    finally:
        store.close()
