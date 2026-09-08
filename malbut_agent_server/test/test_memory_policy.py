"""Verify durable consent, fact mutations and transactional isolation."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from malbut_agent_server.memory import (
    SQLiteMemoryStore, initialize_memory_schema,
)
from malbut_agent_server.schemas import ValidationError


def source(text='내 이름은 현재야', turn='turn-1'):
    """Return an original utterance bound to one conversation turn."""
    return {
        'conversation_id': 'conversation-1',
        'session_instance_id': 'instance-1',
        'generation': 1,
        'turn_id': turn,
        'request_id': 'request-' + turn,
        'text': text,
    }


def fact(value='현재', **changes):
    """Build a direct name fact with matching original evidence."""
    result = {
        'kind': 'name', 'subject': 'user', 'attribute': 'name',
        'value': value, 'evidence': f'내 이름은 {value}야',
    }
    result.update(changes)
    return result


@pytest.fixture
def memory(tmp_path):
    """Close a persistent test store after each test."""
    store = SQLiteMemoryStore(str(tmp_path / 'memory.sqlite3'))
    yield store
    store.close()


def test_legacy_migration_preserves_records_without_granting_consent(tmp_path):
    """Preexisting memories do not count as evidence of personalization."""
    path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(path)
    connection.execute('''CREATE TABLE memories (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
        content TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL,
        created_at REAL NOT NULL, expires_at REAL, metadata_json TEXT NOT NULL
    )''')
    connection.execute(
        "INSERT INTO memories VALUES ('old', 'alice', 'fact', '기억', "
        "'user_verified', 1.0, 1.0, NULL, '{}')",
    )
    connection.commit()
    connection.close()
    store = SQLiteMemoryStore(str(path))
    try:
        assert store.list_for_user('alice')[0].id == 'old'
        assert store.policy_state('alice') == {
            'enabled': False, 'revision': 0, 'legacy_cutoff': 0.0,
        }
        assert store.revision == 0
    finally:
        store.close()


def test_default_disabled_and_raw_add_does_not_consent(memory):
    """Automatic storage needs consent even after a diagnostic insertion."""
    memory.add('alice', '기존 기억')
    assert memory.policy_state('alice')['enabled'] is False
    with pytest.raises(ValidationError, match='disabled'):
        memory.upsert_fact('alice', fact(), source())
    assert len(memory.list_for_user('alice')) == 1


def test_policy_and_global_revisions_survive_connections_and_restart(memory):
    """All writers observe durable counters while user scopes stay separate."""
    other = SQLiteMemoryStore(memory.database_path)
    try:
        enabled = memory.set_personalization('alice', True, source())
        assert enabled == other.policy_state('alice')
        assert enabled['revision'] == 1
        memory.upsert_fact('alice', fact(), source())
        alice_revision = memory.policy_state('alice')['revision']
        assert other.policy_state('alice')['revision'] == alice_revision
        global_revision = other.revision
        other.add('bob', '다른 사용자')
        assert memory.policy_state('alice')['revision'] == alice_revision
        assert memory.revision == global_revision + 1
    finally:
        other.close()
    reopened = SQLiteMemoryStore(memory.database_path)
    try:
        assert reopened.policy_state('alice')['enabled'] is True
        assert reopened.revision == memory.revision
        assert reopened.list_for_user('alice')[0].metadata['version'] == 1
    finally:
        reopened.close()


def test_dedup_and_conflict_do_not_advance_revision(memory):
    """Deduplicate normalized facts and require correction for new names."""
    memory.set_personalization('alice', True, source())
    stored = memory.upsert_fact('alice', fact(), source())
    revision = memory.policy_state('alice')['revision']
    same = memory.upsert_fact(
        'alice', fact(
            subject=' USER ', value=' 현재 ', evidence='내 이름은 현재야',
        ), source(),
    )
    assert same['status'] == 'unchanged'
    assert same['records'][0].id == stored['records'][0].id
    conflicting = memory.upsert_fact(
        'alice', fact('민수'), source('내 이름은 민수야', 'turn-2'),
    )
    assert conflicting['status'] == 'conflict'
    assert conflicting['records'][0].id == stored['records'][0].id
    assert memory.policy_state('alice')['revision'] == revision


def test_multiple_preferences_coexist_in_same_subject_slot(memory):
    """Liking coffee does not silently replace liking tea."""
    memory.set_personalization('alice', True, source())
    for preference in ('커피를 좋아해', '차를 좋아해'):
        value = fact(
            preference, kind='preference', attribute='preference',
            evidence=preference,
        )
        assert memory.upsert_fact(
            'alice', value, source(preference, preference),
        )['status'] == 'stored'
    assert len(memory.list_for_user('alice')) == 2


def test_correction_creates_new_version_and_invalidates_only_old_id(memory):
    """Explicit correction replaces the selected fact and keeps provenance."""
    memory.set_personalization('alice', True, source())
    old = memory.upsert_fact('alice', fact(), source())['records'][0]
    memory.set_personalization('alice', False, source('개인화 중단', 'stop'))
    revised = memory.upsert_fact(
        'alice', fact('민수'), source('내 이름은 민수야', 'turn-2'),
        correct_ids=[old.id],
    )
    new = revised['records'][0]
    assert revised['status'] == 'stored'
    assert revised['invalidated_ids'] == [old.id]
    assert new.id != old.id
    assert new.metadata['version'] == 2
    assert new.metadata['source']['turn_id'] == 'turn-2'
    assert new.content == '사용자 이름은 민수'
    assert memory.invalidated_ids('alice') == {old.id}
    assert memory.invalidated_ids('bob') == set()
    assert memory.list_for_user('alice') == [new]


def test_delete_scope_cutoff_and_no_deleted_plaintext_in_tombstone(memory):
    """Block old context without storing deleted text in tombstones."""
    record = memory.add('alice', 'private old value')
    revision = memory.revision
    assert memory.remove_facts('bob', [record.id])['status'] == 'not_found'
    assert memory.revision == revision
    removed = memory.remove_facts('alice', [record.id])
    assert removed == {'status': 'deleted', 'invalidated_ids': [record.id]}
    assert memory.policy_state('alice')['legacy_cutoff'] > 0
    connection = sqlite3.connect(memory.database_path)
    try:
        row = connection.execute(
            'SELECT * FROM memory_tombstones',
        ).fetchone()
        assert 'private old value' not in repr(row)
    finally:
        connection.close()
    current_revision = memory.revision
    assert memory.delete('alice', record.id) is False
    assert memory.revision == current_revision


def test_deleted_source_cannot_be_reextracted_but_new_statement_can(memory):
    """Block a delayed old candidate while allowing new user statements."""
    memory.set_personalization('alice', True, source())
    record = memory.upsert_fact('alice', fact(), source())['records'][0]
    memory.delete('alice', record.id)
    assert memory.upsert_fact(
        'alice', fact(), source(),
    )['status'] == 'conflict'
    assert memory.list_for_user('alice') == []
    assert memory.upsert_fact(
        'alice', fact(), source(turn='new-statement'),
    )['status'] == 'stored'


def test_disable_preserves_facts_and_reenable_preserves_cutoff(memory):
    """Consent reenable never revives an invalidated context boundary."""
    memory.set_personalization('alice', True, source())
    record = memory.upsert_fact('alice', fact(), source())['records'][0]
    disabled = memory.set_personalization(
        'alice', False, source('기억 활용 중단', 'stop'),
    )
    assert disabled['enabled'] is False
    assert disabled['legacy_cutoff'] > 0
    assert memory.list_for_user('alice')[0].id == record.id
    assert memory.set_personalization(
        'alice', False, source('기억 활용 중단', 'stop'),
    ) == disabled
    enabled = memory.set_personalization(
        'alice', True, source('다시 기억해', 'resume'),
    )
    assert enabled['legacy_cutoff'] == disabled['legacy_cutoff']
    assert enabled['revision'] > disabled['revision']


def test_supplied_transaction_rollback_is_owned_by_caller(memory):
    """Consent, fact and counter changes roll back as one unit."""
    external = sqlite3.connect(memory.database_path)
    initialize_memory_schema(external)
    external.commit()
    initial_revision = memory.revision
    try:
        external.execute('BEGIN IMMEDIATE')
        memory.set_personalization(
            'alice', True, source(), connection=external,
        )
        memory.upsert_fact('alice', fact(), source(), connection=external)
        assert external.in_transaction
        assert memory.policy_state('alice', connection=external)['enabled']
        assert memory.list_for_user('alice', connection=external)
        assert memory.policy_state('alice')['enabled'] is False
        external.rollback()
        assert memory.list_for_user('alice') == []
        assert memory.revision == initial_revision
    finally:
        external.close()


def test_supplied_delete_and_correction_rollback_preserve_original(memory):
    """Rollback must restore fact rows, slots, tombstones and revisions."""
    memory.set_personalization('alice', True, source())
    old = memory.upsert_fact('alice', fact(), source())['records'][0]
    revision = memory.policy_state('alice')['revision']
    connection = sqlite3.connect(memory.database_path)
    initialize_memory_schema(connection)
    connection.commit()
    try:
        connection.execute('BEGIN IMMEDIATE')
        memory.upsert_fact(
            'alice', fact('민수'), source('내 이름은 민수야', 'correct'),
            correct_ids=[old.id], connection=connection,
        )
        connection.rollback()
        assert memory.list_for_user('alice') == [old]
        assert memory.invalidated_ids('alice') == set()
        assert memory.policy_state('alice')['revision'] == revision
        connection.execute('BEGIN IMMEDIATE')
        memory.remove_facts('alice', [old.id], connection=connection)
        assert connection.in_transaction
        connection.rollback()
        assert memory.list_for_user('alice') == [old]
    finally:
        connection.close()


def test_independent_writers_insert_same_fact_once(memory):
    """Database serialization prevents a duplicated fact across processes."""
    memory.set_personalization('alice', True, source())
    barrier = threading.Barrier(2)

    def write():
        store = SQLiteMemoryStore(memory.database_path)
        try:
            barrier.wait(timeout=5)
            return store.upsert_fact('alice', fact(), source())['status']
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write) for _ in range(2)]
        assert sorted(item.result(timeout=10) for item in futures) == [
            'stored', 'unchanged',
        ]
    assert len(memory.list_for_user('alice')) == 1


def test_binding_in_memory_connection_keeps_seeded_state():
    """Existing isolated test stores can share atomic conversation commits."""
    memory = SQLiteMemoryStore(':memory:')
    memory.set_personalization('alice', True, source())
    record = memory.upsert_fact('alice', fact(), source())['records'][0]
    connection = sqlite3.connect(':memory:')
    initialize_memory_schema(connection)
    connection.commit()
    try:
        revision = memory.revision
        memory.bind_connection(connection, threading.RLock())
        assert memory.revision == revision
        assert memory.list_for_user('alice') == [record]
        connection.execute('BEGIN IMMEDIATE')
        memory.remove_facts('alice', [record.id], connection=connection)
        connection.rollback()
        assert memory.list_for_user('alice') == [record]
        memory.close()
        assert connection.execute('SELECT 1').fetchone()[0] == 1
    finally:
        memory.close()
        connection.close()


def test_binding_persistent_store_to_other_database_is_rejected(memory):
    """A sharing mistake must not copy one user's persistent database."""
    other = sqlite3.connect(':memory:')
    try:
        with pytest.raises(ValueError, match='databases differ'):
            memory.bind_connection(other, threading.RLock())
    finally:
        other.close()


def test_supplied_schema_initializer_does_not_commit():
    """Initialization remains part of a caller-owned migration transaction."""
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute('BEGIN IMMEDIATE')
        initialize_memory_schema(connection)
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'memories'",
        ).fetchone() is None
    finally:
        connection.close()


def test_invalid_source_and_unknown_correction_leave_data_unchanged(memory):
    """Invalid model candidates cannot mutate existing memories."""
    memory.set_personalization('alice', True, source())
    revision = memory.revision
    with pytest.raises(ValidationError, match='evidence'):
        memory.upsert_fact('alice', fact(), source('안녕'))
    assert memory.upsert_fact(
        'alice', fact(), source(), correct_ids=['another-users-id'],
    ) == {'status': 'conflict', 'records': [], 'invalidated_ids': []}
    assert memory.revision == revision
    assert memory.list_for_user('alice') == []


def test_expiry_and_raw_delete_bump_durable_user_state(memory):
    """Legacy mutators must participate in all newer freshness checks."""
    old = memory.add('alice', '오래된 기억', expires_at=1.0)
    revision = memory.policy_state('alice')['revision']
    assert memory.purge_expired(now=2.0) == 1
    assert memory.policy_state('alice')['revision'] == revision + 1
    assert old.id in memory.invalidated_ids('alice')
    assert memory.purge_expired(now=2.0) == 0
    assert memory.policy_state('alice')['revision'] == revision + 1
    assert memory.policy_state('bob')['revision'] == 0
