"""Tests for user-isolated SQLite memory retrieval."""

import json
import sqlite3
import stat
import threading
import time

import pytest

from malbut_agent_server.memory import (
    MemoryConsentError,
    MemoryMutationConflictError,
    MemoryNotFoundError,
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
