"""Durable, non-authorizing confirmation intent invariants."""

import hashlib
import json
import sqlite3
import threading
import time

import pytest

from malbut_agent_server.conversation import (
    CONFIRMATION_ONE_PENDING_SESSION_INDEX_SQL,
    CONFIRMATION_RESPONSE_OWNER_INDEX_SQL,
    LEGACY_CONFIRMATION_INTENTS_TABLE_SQL,
    LEGACY_CONFIRMATION_SCHEMA_METADATA_TABLE_SQL,
    ConfirmationIntentAlreadyTerminalError,
    ConfirmationIntentConflictError,
    ConfirmationIntentDraft,
    ConfirmationReservedResponseIdError,
    ConfirmationSchemaError,
    ConversationChangedError,
    ConversationClockError,
    SQLiteConversationStore,
)
from malbut_agent_server.monitor_room_target import Effects, TargetBinding
from malbut_agent_server.schemas import ValidationError


class MutableClock:
    """Small deterministic wall clock for durable deadline tests."""

    def __init__(self, now: float = 100.0) -> None:
        """Start at one finite timestamp."""
        self.now = now

    def __call__(self) -> float:
        """Return the current test timestamp."""
        return self.now


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _target_binding(arguments_json: str, suffix: str) -> TargetBinding:
    """Build one exact v3 room/effects binding for storage tests."""
    geometry = {
        'type': 'Polygon',
        'coordinates': [[
            [0.0, 0.0],
            [4.0, 0.0],
            [4.0, 4.0],
            [0.0, 4.0],
            [0.0, 0.0],
        ]],
    }
    geometry_json = json.dumps(
        geometry,
        sort_keys=True,
        separators=(',', ':'),
    )
    return TargetBinding(
        device_id=f'durable-device-{suffix}',
        device_binding_revision=f'durable-membership-{suffix}',
        source_revision=f'durable-source-{suffix}',
        map_id='durable-map-home',
        map_revision=f'durable-map-revision-{suffix}',
        semantic_revision=_digest(f'durable-semantics-{suffix}'),
        frame_id='map',
        room_id=f'durable-room-living-{suffix}',
        room_name='거실',
        room_category='living_room',
        source_arguments_digest=hashlib.sha256(
            arguments_json.encode('utf-8')
        ).hexdigest(),
        geometry_json=geometry_json,
        geometry_digest=hashlib.sha256(
            geometry_json.encode('utf-8')
        ).hexdigest(),
        representative_point=(2.0, 2.0),
        clearance_m=2.0,
        area_m2=16.0,
        effects=Effects(
            physical_navigation=True,
            camera_capture=True,
            external_video_stream=True,
            video_recording=False,
            audio_capture=False,
            max_duration_seconds=300,
            coverage_mode='whole_room',
            viewer_scope='requesting_user',
            talkback_allowed=False,
        ),
    )


def _reserve_confirmation(
    store: SQLiteConversationStore,
    clock: MutableClock,
    *,
    suffix: str = '1',
    expires_in: float = 60.0,
    user_id: str = 'durable-user',
    conversation_id: str = 'durable-conversation',
):
    session = store.create(user_id, conversation_id)
    begin = store.begin_turn(
        user_id=session.user_id,
        conversation_id=session.conversation_id,
        turn_id=f'durable-turn-{suffix}',
        request_id=f'durable-request-{suffix}',
        request_fingerprint=_digest(f'request-{suffix}'),
        user_content='거실 전체를 보여줘',
    )
    token = begin.token
    assert token is not None
    arguments = {'location': '거실'}
    arguments_json = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    target = _target_binding(arguments_json, suffix)
    issued_at = clock.now
    expires_at = issued_at + expires_in
    confirmation_message = (
        '거실에서 이동해 방 전체를 확인하고 실시간 영상을 전송할까요? '
        '최대 300초, 녹화·음성·말하기는 사용하지 않습니다.'
    )
    fingerprint_body = {
        'schema_version': 3,
        'agent_request_id': token.request_id,
        'user_id': token.user_id,
        'speech_session_id': 'durable-speech-session',
        'source_utterance_id': f'durable-utterance-{suffix}',
        'conversation_id': token.conversation_id,
        'conversation_session_instance_id': token.session_instance_id,
        'conversation_generation': token.generation,
        'conversation_revision': token.revision + 1,
        'conversation_ordinal': token.ordinal,
        'turn_id': token.turn_id,
        'decision_id': f'durable-decision-{suffix}',
        'tool_name': 'monitor_room',
        'arguments': arguments,
        'issued_at': issued_at,
        'expires_at': expires_at,
        'risk_level': 'L3',
        'message': confirmation_message,
        'target': target.to_private_dict(),
    }
    proposal_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()
    draft = ConfirmationIntentDraft(
        schema_version=3,
        confirmation_request_id=f'durable-confirmation-{suffix}',
        agent_request_id=token.request_id,
        user_id=token.user_id,
        speech_session_id='durable-speech-session',
        source_utterance_id=f'durable-utterance-{suffix}',
        conversation_id=token.conversation_id,
        session_instance_id=token.session_instance_id,
        generation=token.generation,
        revision=token.revision + 1,
        ordinal=token.ordinal,
        turn_id=token.turn_id,
        decision_id=f'durable-decision-{suffix}',
        tool_name='monitor_room',
        arguments_digest=hashlib.sha256(
            arguments_json.encode('utf-8')
        ).hexdigest(),
        proposal_fingerprint=proposal_fingerprint,
        issued_at=issued_at,
        expires_at=expires_at,
        risk_level='L3',
        confirmation_message=confirmation_message,
        target_binding_schema_version=target.schema_version,
        target_device_id=target.device_id,
        target_device_binding_revision=target.device_binding_revision,
        target_source_revision=target.source_revision,
        target_map_id=target.map_id,
        target_map_revision=target.map_revision,
        target_semantic_revision=target.semantic_revision,
        target_frame_id=target.frame_id,
        target_room_id=target.room_id,
        target_room_name=target.room_name,
        target_room_category=target.room_category,
        target_geometry_json=target.geometry_json,
        target_geometry_digest=target.geometry_digest,
        target_representative_x=target.representative_point[0],
        target_representative_y=target.representative_point[1],
        target_clearance_m=target.clearance_m,
        target_area_m2=target.area_m2,
        target_source_arguments_digest=target.source_arguments_digest,
        target_binding_digest=target.binding_digest,
        effects_schema_version=target.effects.schema_version,
        effect_physical_navigation=target.effects.physical_navigation,
        effect_camera_capture=target.effects.camera_capture,
        effect_external_video_stream=target.effects.external_video_stream,
        effect_video_recording=target.effects.video_recording,
        effect_audio_capture=target.effects.audio_capture,
        effect_coverage_mode=target.effects.coverage_mode,
        effect_viewer_scope=target.effects.viewer_scope,
        effect_talkback_allowed=target.effects.talkback_allowed,
        effect_max_duration_seconds=target.effects.max_duration_seconds,
        effects_digest=target.effects_digest,
    )
    response = {
        'schema_version': 3,
        'public': {
            'request_id': token.request_id,
            'conversation': {
                'conversation_id': token.conversation_id,
                'session_instance_id': token.session_instance_id,
                'turn_id': token.turn_id,
                'generation': token.generation,
                'revision': token.revision + 1,
                'ordinal': token.ordinal,
            },
            'decision': {
                'type': 'tool_call',
                'tool_name': 'monitor_room',
                'arguments': arguments,
            },
            'safety': {'allowed': True},
            'execution': {
                'decision_id': draft.decision_id,
                'issued_at': issued_at,
                'expires_at': expires_at,
                'proposal_authorized': True,
                'state_trusted': True,
                'authorized': False,
                'consume_once': False,
                'tool_call_id': None,
            },
        },
    }
    return token, draft, response


def _commit_confirmation(
    store: SQLiteConversationStore,
    clock: MutableClock,
    *,
    suffix: str = '1',
    expires_in: float = 60.0,
    user_id: str = 'durable-user',
    conversation_id: str = 'durable-conversation',
):
    token, draft, response = _reserve_confirmation(
        store,
        clock,
        suffix=suffix,
        expires_in=expires_in,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    store.complete_turn(
        token,
        assistant_content='거실 모니터링을 시작할까요?',
        response=response,
        confirmation_intent=draft,
    )
    return draft


def _create_version_02_database(path) -> None:
    connection = sqlite3.connect(str(path))
    connection.executescript(
        '''
        CREATE TABLE conversation_sessions (
            user_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            status TEXT NOT NULL,
            generation INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            PRIMARY KEY (user_id, conversation_id)
        );
        CREATE TABLE conversation_turns (
            user_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            generation INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            status TEXT NOT NULL,
            user_content TEXT NOT NULL,
            assistant_content TEXT,
            response_json TEXT,
            created_at REAL NOT NULL,
            completed_at REAL,
            PRIMARY KEY (
                user_id,
                conversation_id,
                generation,
                turn_id
            ),
            UNIQUE (user_id, request_id),
            UNIQUE (
                user_id,
                conversation_id,
                generation,
                ordinal
            )
        );
        '''
    )
    connection.execute(
        '''
        INSERT INTO conversation_sessions
        VALUES ('legacy-user', 'legacy-conversation',
                'active', 1, 0, 1, 1, 9999999999)
        '''
    )
    connection.commit()
    connection.close()


def _create_storage_v1_confirmation_database(path) -> dict:
    """Create an exact storage-v1 database with v2 request rows."""
    _create_version_02_database(path)
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            ';\n'.join(
                (
                    LEGACY_CONFIRMATION_INTENTS_TABLE_SQL,
                    CONFIRMATION_RESPONSE_OWNER_INDEX_SQL,
                    CONFIRMATION_ONE_PENDING_SESSION_INDEX_SQL,
                    LEGACY_CONFIRMATION_SCHEMA_METADATA_TABLE_SQL,
                )
            )
            + ';'
        )
        connection.execute(
            '''
            INSERT INTO confirmation_schema_metadata (
                singleton, schema_version
            ) VALUES (1, 1)
            '''
        )
        columns = (
            'schema_version',
            'confirmation_request_id',
            'agent_request_id',
            'user_id',
            'speech_session_id',
            'source_utterance_id',
            'conversation_id',
            'session_instance_id',
            'generation',
            'revision',
            'ordinal',
            'turn_id',
            'decision_id',
            'tool_name',
            'arguments_digest',
            'proposal_fingerprint',
            'issued_at',
            'expires_at',
            'risk_level',
            'state',
            'disposition',
            'requested_disposition',
            'result_code',
            'confirmation_result_id',
            'response_id',
            'response_fingerprint',
            'response_channel',
            'assurance_level',
            'provenance_ref',
            'verifier_ref',
            'resolved_at',
            'created_at',
            'updated_at',
            'authority_kind',
            'eligible_for_execution',
            'execution_authorized',
        )
        placeholders = ', '.join(f':{column}' for column in columns)
        statement = (
            f"INSERT INTO confirmation_intents ({', '.join(columns)}) "
            f'VALUES ({placeholders})'
        )
        common = {
            'schema_version': 2,
            'agent_request_id': 'legacy-agent-request',
            'user_id': 'legacy-user',
            'speech_session_id': 'legacy-speech-session',
            'conversation_id': 'legacy-conversation',
            'session_instance_id': 'legacy-session-instance',
            'generation': 1,
            'revision': 1,
            'tool_name': 'monitor_room',
            'arguments_digest': _digest('{"location":"거실"}'),
            'issued_at': 50.0,
            'expires_at': 500.0,
            'risk_level': 'L3',
            'authority_kind': 'none',
            'eligible_for_execution': 0,
            'execution_authorized': 0,
            'verifier_ref': None,
        }
        pending = {
            **common,
            'confirmation_request_id': 'legacy-pending-confirmation',
            'source_utterance_id': 'legacy-pending-utterance',
            'ordinal': 1,
            'turn_id': 'legacy-pending-turn',
            'decision_id': 'legacy-pending-decision',
            'proposal_fingerprint': _digest('legacy-pending-proposal'),
            'state': 'pending',
            'disposition': None,
            'requested_disposition': None,
            'result_code': None,
            'confirmation_result_id': None,
            'response_id': None,
            'response_fingerprint': None,
            'response_channel': None,
            'assurance_level': None,
            'provenance_ref': None,
            'resolved_at': None,
            'created_at': 50.0,
            'updated_at': 50.0,
        }
        terminal = {
            **common,
            'confirmation_request_id': 'legacy-terminal-confirmation',
            'source_utterance_id': 'legacy-terminal-utterance',
            'ordinal': 2,
            'turn_id': 'legacy-terminal-turn',
            'decision_id': 'legacy-terminal-decision',
            'proposal_fingerprint': _digest('legacy-terminal-proposal'),
            'state': 'resolved',
            'disposition': 'approve',
            'requested_disposition': 'approve',
            'result_code': 'confirmation_approval_recorded_no_execution',
            'confirmation_result_id': 'legacy-terminal-result',
            'response_id': 'legacy-terminal-response',
            'response_fingerprint': _digest('legacy-terminal-response'),
            'response_channel': 'ui_in_process',
            'assurance_level': 'unverified_in_process_ui',
            'provenance_ref': _digest('legacy-terminal-provenance'),
            'resolved_at': 75.0,
            'created_at': 50.0,
            'updated_at': 75.0,
        }
        connection.execute(statement, pending)
        connection.execute(statement, terminal)
        connection.commit()
    finally:
        connection.close()
    return {
        'pending_id': pending['confirmation_request_id'],
        'pending_proposal': pending['proposal_fingerprint'],
        'terminal_id': terminal['confirmation_request_id'],
        'terminal_proposal': terminal['proposal_fingerprint'],
    }


def _rewrite_stored_table_sql(
    database,
    table_name: str,
    old: str,
    new: str,
) -> None:
    """Tamper one temp database schema without rebuilding its table."""
    connection = sqlite3.connect(str(database))
    try:
        stored = connection.execute(
            '''
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = ?
            ''',
            (table_name,),
        ).fetchone()[0]
        assert stored.count(old) == 1
        schema_version = connection.execute(
            'PRAGMA schema_version'
        ).fetchone()[0]
        connection.execute('PRAGMA writable_schema=ON')
        connection.execute(
            '''
            UPDATE sqlite_master SET sql = ?
            WHERE type = 'table' AND name = ?
            ''',
            (stored.replace(old, new, 1), table_name),
        )
        connection.execute(
            f'PRAGMA schema_version = {schema_version + 1}'
        )
        connection.commit()
        connection.execute('PRAGMA writable_schema=OFF')
    finally:
        connection.close()


def test_confirmation_schema_initializes_and_reopens(tmp_path) -> None:
    """A fresh file gets one validated confirmation schema version."""
    database = tmp_path / 'confirmation.sqlite3'
    store = SQLiteConversationStore(str(database))
    store.close()

    reopened = SQLiteConversationStore(str(database))
    reopened.close()

    connection = sqlite3.connect(str(database))
    try:
        metadata = connection.execute(
            '''
            SELECT singleton, schema_version
            FROM confirmation_schema_metadata
            '''
        ).fetchall()
        assert metadata == [(1, 3)]
        columns = {
            row[1]: row
            for row in connection.execute(
                'PRAGMA table_info(confirmation_intents)'
            ).fetchall()
        }
        assert columns['confirmation_request_id'][3] == 1
        assert columns['confirmation_request_id'][5] == 1
    finally:
        connection.close()


def test_storage_v1_migration_preserves_terminal_and_tombstones_pending(
    tmp_path,
) -> None:
    """V2 requests survive audit but can never become executable again."""
    database = tmp_path / 'confirmation-storage-v1.sqlite3'
    fixture = _create_storage_v1_confirmation_database(database)
    clock = MutableClock(100.0)

    migrated = SQLiteConversationStore(str(database), clock=clock)
    try:
        terminal = migrated.get_confirmation_intent(
            'legacy-user',
            fixture['terminal_id'],
        )
        assert terminal.schema_version == 2
        assert terminal.state == 'resolved'
        assert terminal.disposition == 'approve'
        assert terminal.result_code == (
            'confirmation_approval_recorded_no_execution'
        )
        assert terminal.response_id == 'legacy-terminal-response'
        assert terminal.resolved_at == 75.0
        assert terminal.target_binding_digest is None
        assert terminal.effects_digest is None
        assert terminal.to_public_dict()['target_binding'] == {
            'bound': False,
            'binding_digest': None,
            'effects_digest': None,
        }

        pending = migrated.get_confirmation_intent(
            'legacy-user',
            fixture['pending_id'],
        )
        assert pending.schema_version == 2
        assert pending.state == 'invalidated'
        assert pending.disposition is None
        assert pending.result_code == (
            'confirmation_binding_upgrade_required'
        )
        assert pending.response_id is None
        assert pending.resolved_at == clock.now
        assert pending.target_binding_digest is None
        assert pending.effects_digest is None

        metadata = migrated._connection.execute(
            '''
            SELECT singleton, schema_version
            FROM confirmation_schema_metadata
            '''
        ).fetchall()
        assert [tuple(row) for row in metadata] == [(1, 3)]
        assert migrated._connection.execute(
            '''
            SELECT COUNT(*) FROM sqlite_master
            WHERE name = 'confirmation_intents_v1_backup'
            '''
        ).fetchone()[0] == 0
    finally:
        migrated.close()

    restarted = SQLiteConversationStore(str(database), clock=clock)
    try:
        before_late_response = restarted.get_confirmation_intent(
            'legacy-user',
            fixture['pending_id'],
        )
        late_response = restarted.resolve_confirmation_intent(
            user_id='legacy-user',
            confirmation_request_id=fixture['pending_id'],
            proposal_fingerprint=fixture['pending_proposal'],
            response_id='late-v2-approval',
            response_fingerprint=_digest('late-v2-approval'),
            requested_disposition='approve',
            response_channel='ui_in_process',
            assurance_level='unverified_in_process_ui',
            provenance_ref=_digest('late-v2-provenance'),
        )
        assert late_response == before_late_response
        assert late_response.state == 'invalidated'
        assert late_response.result_code == (
            'confirmation_binding_upgrade_required'
        )
        assert late_response.response_id is None

        clock.now = 600.0
        assert restarted.expire_due_confirmation_intents() == ()
        assert restarted.get_confirmation_intent(
            'legacy-user',
            fixture['pending_id'],
        ) == late_response
    finally:
        restarted.close()

    final_restart = SQLiteConversationStore(str(database), clock=clock)
    try:
        assert final_restart.get_confirmation_intent(
            'legacy-user',
            fixture['pending_id'],
        ).result_code == 'confirmation_binding_upgrade_required'
        assert final_restart.get_confirmation_intent(
            'legacy-user',
            fixture['terminal_id'],
        ).result_code == 'confirmation_approval_recorded_no_execution'
    finally:
        final_restart.close()


@pytest.mark.parametrize('shape', ['partial', 'loose'])
def test_malformed_confirmation_schema_fails_closed_and_unlocks(
    tmp_path,
    shape: str,
) -> None:
    """Partial or lookalike confirmation tables are never self-healed."""
    database = tmp_path / f'malformed-{shape}.sqlite3'
    connection = sqlite3.connect(str(database))
    connection.execute(
        '''
        CREATE TABLE confirmation_intents (
            confirmation_request_id TEXT PRIMARY KEY
        )
        '''
    )
    if shape == 'loose':
        connection.execute(
            '''
            CREATE TABLE confirmation_schema_metadata (
                singleton INTEGER PRIMARY KEY,
                schema_version INTEGER
            )
            '''
        )
        connection.execute(
            'INSERT INTO confirmation_schema_metadata VALUES (1, 1)'
        )
    connection.commit()
    connection.close()

    with pytest.raises(ConfirmationSchemaError):
        SQLiteConversationStore(str(database))

    probe = sqlite3.connect(str(database), timeout=1.0)
    try:
        probe.execute('BEGIN IMMEDIATE')
        probe.rollback()
    finally:
        probe.close()


@pytest.mark.parametrize(
    ('index_name', 'replacement'),
    [
        (
            'confirmation_response_owner_idx',
            '''
            CREATE UNIQUE INDEX confirmation_response_owner_idx
            ON confirmation_intents (user_id, response_id)
            WHERE response_id IS NOT NULL AND 0
            ''',
        ),
        (
            'confirmation_one_pending_session_idx',
            '''
            CREATE UNIQUE INDEX confirmation_one_pending_session_idx
            ON confirmation_intents (
                user_id, conversation_id, session_instance_id
            )
            WHERE state = 'pending' AND 0
            ''',
        ),
    ],
)
def test_lookalike_partial_index_is_rejected(
    tmp_path,
    index_name: str,
    replacement: str,
) -> None:
    """A same-named index with a weaker predicate cannot pass reopen."""
    database = tmp_path / f'lookalike-{index_name}.sqlite3'
    store = SQLiteConversationStore(str(database))
    store.close()
    connection = sqlite3.connect(str(database))
    connection.execute(f'DROP INDEX {index_name}')
    connection.execute(replacement)
    connection.commit()
    connection.close()

    with pytest.raises(ConfirmationSchemaError):
        SQLiteConversationStore(str(database))


@pytest.mark.parametrize(
    ('old', 'new'),
    [
        ('    CHECK (expires_at > issued_at),\n', ''),
        (
            "    CHECK (\n        (state = 'pending'",
            "    CHECK (1 OR\n        (state = 'pending'",
        ),
    ],
)
def test_modified_confirmation_table_constraint_is_rejected(
    tmp_path,
    old: str,
    new: str,
) -> None:
    """The complete canonical table DDL is checked on every reopen."""
    suffix = hashlib.sha256(old.encode()).hexdigest()[:8]
    database = tmp_path / f'tampered-{suffix}.sqlite3'
    store = SQLiteConversationStore(str(database))
    store.close()
    _rewrite_stored_table_sql(
        database,
        'confirmation_intents',
        old,
        new,
    )

    with pytest.raises(ConfirmationSchemaError):
        SQLiteConversationStore(str(database))


def test_non_integer_confirmation_schema_version_is_rejected(
    tmp_path,
) -> None:
    """A REAL value that truncates to version two cannot pass reopen."""
    database = tmp_path / 'metadata-real-version.sqlite3'
    store = SQLiteConversationStore(str(database))
    store.close()
    connection = sqlite3.connect(str(database))
    try:
        connection.execute('PRAGMA ignore_check_constraints=ON')
        connection.execute(
            '''
            UPDATE confirmation_schema_metadata
            SET schema_version = 2.5
            '''
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ConfirmationSchemaError):
        SQLiteConversationStore(str(database))


def test_confirmation_trigger_is_rejected(tmp_path) -> None:
    """A trigger cannot mutate the immutable confirmation evidence row."""
    database = tmp_path / 'confirmation-trigger.sqlite3'
    store = SQLiteConversationStore(str(database))
    store.close()
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            '''
            CREATE TRIGGER confirmation_mutator
            AFTER INSERT ON confirmation_intents
            BEGIN
                UPDATE confirmation_intents
                SET tool_name = 'mutated'
                WHERE confirmation_request_id = NEW.confirmation_request_id;
            END
            '''
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ConfirmationSchemaError):
        SQLiteConversationStore(str(database))


def test_confirmation_insert_failure_rolls_back_completed_turn() -> None:
    """An expired draft cannot leave a completed turn without its intent."""
    clock = MutableClock()
    store = SQLiteConversationStore(':memory:', clock=clock)
    try:
        token, draft, response = _reserve_confirmation(
            store,
            clock,
            expires_in=1.0,
        )
        clock.now = draft.expires_at
        with pytest.raises(ConversationChangedError):
            store.complete_turn(
                token,
                assistant_content='거실 모니터링을 시작할까요?',
                response=response,
                confirmation_intent=draft,
            )
        assert store._connection.in_transaction is False
        row = store._connection.execute(
            '''
            SELECT status FROM conversation_turns
            WHERE user_id = ? AND request_id = ?
            ''',
            (token.user_id, token.request_id),
        ).fetchone()
        assert row['status'] == 'pending'
        session = store.get(token.user_id, token.conversation_id)
        assert session.revision == token.revision
        assert store._connection.execute(
            'SELECT COUNT(*) FROM confirmation_intents'
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_pending_and_terminal_intents_survive_restart(tmp_path) -> None:
    """Restart preserves pending state and exact terminal replay semantics."""
    database = tmp_path / 'restart.sqlite3'
    clock = MutableClock()
    store = SQLiteConversationStore(str(database), clock=clock)
    draft = _commit_confirmation(store, clock)
    store.close()

    reopened = SQLiteConversationStore(str(database), clock=clock)
    pending = reopened.get_confirmation_intent(
        draft.user_id,
        draft.confirmation_request_id,
    )
    assert pending.state == 'pending'
    terminal = reopened.resolve_confirmation_intent(
        user_id=draft.user_id,
        confirmation_request_id=draft.confirmation_request_id,
        proposal_fingerprint=draft.proposal_fingerprint,
        response_id='restart-response-1',
        response_fingerprint=_digest('restart-response'),
        requested_disposition='approve',
        response_channel='ui_in_process',
        assurance_level='unverified_in_process_ui',
        provenance_ref=_digest('restart-provenance'),
    )
    assert terminal.state == 'resolved'
    assert terminal.disposition == 'approve'
    assert terminal.to_public_dict()['authority'] == {
        'kind': 'none',
        'eligible_for_execution': False,
        'execution_authorized': False,
        'consume_once': False,
        'tool_call_id': None,
        'mission_id': None,
    }
    reopened.close()

    replay_store = SQLiteConversationStore(str(database), clock=clock)
    try:
        replay = replay_store.resolve_confirmation_intent(
            user_id=draft.user_id,
            confirmation_request_id=draft.confirmation_request_id,
            proposal_fingerprint=draft.proposal_fingerprint,
            response_id='restart-response-1',
            response_fingerprint=_digest('restart-response'),
            requested_disposition='approve',
            response_channel='ui_in_process',
            assurance_level='unverified_in_process_ui',
            provenance_ref=_digest('restart-provenance'),
        )
        assert replay == terminal
        with pytest.raises(ConfirmationIntentConflictError):
            replay_store.resolve_confirmation_intent(
                user_id=draft.user_id,
                confirmation_request_id=draft.confirmation_request_id,
                proposal_fingerprint=draft.proposal_fingerprint,
                response_id='restart-response-1',
                response_fingerprint=_digest('mutated-response'),
                requested_disposition='approve',
                response_channel='ui_in_process',
                assurance_level='unverified_in_process_ui',
                provenance_ref=_digest('restart-provenance'),
            )
        with pytest.raises(ConfirmationIntentConflictError):
            replay_store.resolve_confirmation_intent(
                user_id=draft.user_id,
                confirmation_request_id=draft.confirmation_request_id,
                proposal_fingerprint=draft.proposal_fingerprint,
                response_id='restart-response-1',
                response_fingerprint=_digest('restart-response'),
                requested_disposition='deny',
                response_channel='ui_in_process',
                assurance_level='unverified_in_process_ui',
                provenance_ref=_digest('restart-provenance'),
            )
    finally:
        replay_store.close()


def test_restart_sweep_expires_at_exact_server_deadline(tmp_path) -> None:
    """A durable deadline is enforced without process-local speech state."""
    database = tmp_path / 'restart-expiry.sqlite3'
    clock = MutableClock()
    store = SQLiteConversationStore(str(database), clock=clock)
    draft = _commit_confirmation(
        store,
        clock,
        expires_in=10.0,
    )
    store.close()

    clock.now = draft.expires_at - 0.001
    reopened = SQLiteConversationStore(str(database), clock=clock)
    try:
        assert reopened.expire_due_confirmation_intents() == ()
        assert reopened.get_confirmation_intent(
            draft.user_id,
            draft.confirmation_request_id,
        ).state == 'pending'
        clock.now = draft.expires_at
        expired = reopened.expire_due_confirmation_intents()
        assert len(expired) == 1
        assert expired[0].state == 'resolved'
        assert expired[0].disposition == 'expired'
        assert expired[0].result_code == 'confirmation_expired'
        assert expired[0].response_channel == 'server_expiry'
        assert expired[0].to_public_dict()['authority'][
            'execution_authorized'
        ] is False
        assert reopened.expire_due_confirmation_intents() == ()
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ('channel', 'assurance'),
    [
        ('voice', 'local_speech_binding'),
        ('ui_in_process', 'unverified_in_process_ui'),
    ],
)
def test_user_channel_cannot_claim_server_expiry_response_id(
    channel: str,
    assurance: str,
) -> None:
    """A user response cannot prevent another request from expiring."""
    clock = MutableClock()
    store = SQLiteConversationStore(':memory:', clock=clock)
    try:
        first = _commit_confirmation(
            store,
            clock,
            suffix='reserved-a',
            expires_in=10.0,
            conversation_id='reserved-conversation-a',
        )
        second = _commit_confirmation(
            store,
            clock,
            suffix='reserved-b',
            expires_in=100.0,
            conversation_id='reserved-conversation-b',
        )
        reserved_id, _fingerprint, _provenance = (
            store.confirmation_expiry_envelope(
                first.confirmation_request_id,
                first.proposal_fingerprint,
            )
        )
        with pytest.raises(ConfirmationReservedResponseIdError):
            store.resolve_confirmation_intent(
                user_id=second.user_id,
                confirmation_request_id=(
                    second.confirmation_request_id
                ),
                proposal_fingerprint=second.proposal_fingerprint,
                response_id=reserved_id,
                response_fingerprint=_digest('reserved-user-response'),
                requested_disposition='approve',
                response_channel=channel,
                assurance_level=assurance,
                provenance_ref=_digest('reserved-user-provenance'),
            )
        assert store.get_confirmation_intent(
            second.user_id,
            second.confirmation_request_id,
        ).state == 'pending'

        clock.now = first.expires_at
        expired = store.expire_due_confirmation_intents()
        assert [record.confirmation_request_id for record in expired] == [
            first.confirmation_request_id,
        ]
        assert expired[0].disposition == 'expired'
    finally:
        store.close()


@pytest.mark.parametrize(
    'mutated_field',
    ['response_id', 'response_fingerprint', 'provenance_ref'],
)
def test_server_expiry_requires_exact_internal_envelope(
    mutated_field: str,
) -> None:
    """Arbitrary server-clock claims cannot terminalize an intent."""
    clock = MutableClock()
    store = SQLiteConversationStore(':memory:', clock=clock)
    try:
        draft = _commit_confirmation(
            store,
            clock,
            expires_in=10.0,
        )
        response_id, fingerprint, provenance = (
            store.confirmation_expiry_envelope(
                draft.confirmation_request_id,
                draft.proposal_fingerprint,
            )
        )
        envelope = {
            'response_id': response_id,
            'response_fingerprint': fingerprint,
            'provenance_ref': provenance,
        }
        envelope[mutated_field] = (
            'wrong-server-response'
            if mutated_field == 'response_id'
            else _digest(f'wrong-{mutated_field}')
        )
        clock.now = draft.expires_at
        with pytest.raises(ValidationError):
            store.resolve_confirmation_intent(
                user_id=draft.user_id,
                confirmation_request_id=draft.confirmation_request_id,
                proposal_fingerprint=draft.proposal_fingerprint,
                requested_disposition='cancel',
                response_channel='server_expiry',
                assurance_level='server_clock',
                **envelope,
            )
        assert store.get_confirmation_intent(
            draft.user_id,
            draft.confirmation_request_id,
        ).state == 'pending'

        terminal = store.resolve_confirmation_intent(
            user_id=draft.user_id,
            confirmation_request_id=draft.confirmation_request_id,
            proposal_fingerprint=draft.proposal_fingerprint,
            response_id=response_id,
            response_fingerprint=fingerprint,
            requested_disposition='cancel',
            response_channel='server_expiry',
            assurance_level='server_clock',
            provenance_ref=provenance,
        )
        assert terminal.disposition == 'expired'
    finally:
        store.close()


def test_clock_before_confirmation_issue_is_rejected() -> None:
    """A backwards wall clock cannot approve a not-yet-issued request."""
    clock = MutableClock()
    store = SQLiteConversationStore(':memory:', clock=clock)
    try:
        draft = _commit_confirmation(store, clock)
        clock.now = draft.issued_at - 1.0
        with pytest.raises(ConversationClockError):
            store.resolve_confirmation_intent(
                user_id=draft.user_id,
                confirmation_request_id=draft.confirmation_request_id,
                proposal_fingerprint=draft.proposal_fingerprint,
                response_id='backward-clock-response',
                response_fingerprint=_digest('backward-clock'),
                requested_disposition='approve',
                response_channel='ui_in_process',
                assurance_level='unverified_in_process_ui',
                provenance_ref=_digest('backward-clock-provenance'),
            )
        assert store.get_confirmation_intent(
            draft.user_id,
            draft.confirmation_request_id,
        ).state == 'pending'
    finally:
        store.close()


def test_legacy_expiry_id_collision_is_tombstoned_without_batch_abort(
) -> None:
    """A pre-fix collision cannot roll back other deadline transitions."""
    clock = MutableClock()
    store = SQLiteConversationStore(':memory:', clock=clock)
    try:
        first = _commit_confirmation(
            store,
            clock,
            suffix='legacy-a',
            expires_in=10.0,
            conversation_id='legacy-conversation-a',
        )
        third = _commit_confirmation(
            store,
            clock,
            suffix='legacy-c',
            expires_in=10.0,
            conversation_id='legacy-conversation-c',
        )
        second = _commit_confirmation(
            store,
            clock,
            suffix='legacy-b',
            expires_in=100.0,
            conversation_id='legacy-conversation-b',
        )
        terminal = store.resolve_confirmation_intent(
            user_id=second.user_id,
            confirmation_request_id=second.confirmation_request_id,
            proposal_fingerprint=second.proposal_fingerprint,
            response_id='legacy-owner-response',
            response_fingerprint=_digest('legacy-owner-response'),
            requested_disposition='deny',
            response_channel='ui_in_process',
            assurance_level='unverified_in_process_ui',
            provenance_ref=_digest('legacy-owner-provenance'),
        )
        reserved_id, _fingerprint, _provenance = (
            store.confirmation_expiry_envelope(
                first.confirmation_request_id,
                first.proposal_fingerprint,
            )
        )
        store._connection.execute(
            '''
            UPDATE confirmation_intents SET response_id = ?
            WHERE confirmation_request_id = ?
            ''',
            (reserved_id, terminal.confirmation_request_id),
        )
        store._connection.commit()

        clock.now = first.expires_at
        outcomes = store.expire_due_confirmation_intents()
        by_id = {
            outcome.confirmation_request_id: outcome
            for outcome in outcomes
        }
        assert by_id[first.confirmation_request_id].state == 'invalidated'
        assert by_id[first.confirmation_request_id].result_code == (
            'confirmation_expiry_response_id_conflict'
        )
        assert by_id[third.confirmation_request_id].disposition == 'expired'
    finally:
        store.close()


def test_two_connections_have_one_confirmation_terminal_winner(
    tmp_path,
) -> None:
    """Competing approve and deny writers have one SQLite winner."""
    database = tmp_path / 'terminal-race.sqlite3'
    clock = MutableClock()
    first = SQLiteConversationStore(str(database), clock=clock)
    draft = _commit_confirmation(first, clock)
    second = SQLiteConversationStore(str(database), clock=clock)
    barrier = threading.Barrier(3)
    results = []
    errors = []
    result_lock = threading.Lock()

    def resolve(store, disposition: str) -> None:
        try:
            barrier.wait(timeout=2.0)
            result = store.resolve_confirmation_intent(
                user_id=draft.user_id,
                confirmation_request_id=(
                    draft.confirmation_request_id
                ),
                proposal_fingerprint=draft.proposal_fingerprint,
                response_id=f'race-{disposition}',
                response_fingerprint=_digest(
                    f'race-{disposition}'
                ),
                requested_disposition=disposition,
                response_channel='ui_in_process',
                assurance_level='unverified_in_process_ui',
                provenance_ref=_digest(
                    f'race-{disposition}-provenance'
                ),
            )
            with result_lock:
                results.append(result)
        except Exception as error:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(error)

    threads = [
        threading.Thread(target=resolve, args=(first, 'approve')),
        threading.Thread(target=resolve, args=(second, 'deny')),
    ]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2.0)
        for thread in threads:
            thread.join(timeout=5.0)
            assert not thread.is_alive()
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(
            errors[0],
            ConfirmationIntentAlreadyTerminalError,
        )
        terminal = first.get_confirmation_intent(
            draft.user_id,
            draft.confirmation_request_id,
        )
        assert terminal.state == 'resolved'
        assert terminal.disposition in {'approve', 'deny'}
        assert terminal.disposition == results[0].disposition
    finally:
        second.close()
        first.close()


def test_concurrent_legacy_open_migrates_once(tmp_path) -> None:
    """Concurrent first opens serialize legacy ALTER and confirmation DDL."""
    database = tmp_path / 'legacy-concurrent.sqlite3'
    _create_version_02_database(database)
    barrier = threading.Barrier(9)
    errors = []
    errors_lock = threading.Lock()

    def open_store() -> None:
        try:
            barrier.wait()
            store = SQLiteConversationStore(str(database))
            store.get('legacy-user', 'legacy-conversation')
            store.close()
        except Exception as error:  # pragma: no cover - asserted below
            with errors_lock:
                errors.append(error)

    threads = [
        threading.Thread(target=open_store)
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive()

    assert errors == []
    connection = sqlite3.connect(str(database))
    try:
        session_columns = [
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(conversation_sessions)'
            ).fetchall()
        ]
        assert session_columns.count('session_instance_id') == 1
        assert connection.execute(
            'SELECT COUNT(*) FROM confirmation_schema_metadata'
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_wal_initialization_retries_a_locked_transition(
    tmp_path,
    monkeypatch,
) -> None:
    """A concurrent writer cannot make file-store startup flaky."""
    database = tmp_path / 'wal-transition.sqlite3'
    _create_version_02_database(database)
    blocker = sqlite3.connect(str(database))
    blocker.execute('BEGIN IMMEDIATE')
    entered = threading.Event()
    original = SQLiteConversationStore._enable_wal_with_retry
    stores = []
    errors = []

    def observed_enable(store) -> None:
        entered.set()
        original(store)

    def open_store() -> None:
        try:
            stores.append(SQLiteConversationStore(str(database)))
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    monkeypatch.setattr(
        SQLiteConversationStore,
        '_enable_wal_with_retry',
        observed_enable,
    )
    thread = threading.Thread(target=open_store)
    try:
        thread.start()
        assert entered.wait(timeout=2.0)
        time.sleep(0.05)
        assert thread.is_alive()
        blocker.rollback()
        thread.join(timeout=6.0)
        assert not thread.is_alive()
        assert errors == []
        assert len(stores) == 1
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        for store in stores:
            store.close()


def test_raw_writers_cannot_add_execution_authority(tmp_path) -> None:
    """Database checks keep every intent permanently non-authorizing."""
    database = tmp_path / 'authority.sqlite3'
    clock = MutableClock()
    store = SQLiteConversationStore(str(database), clock=clock)
    draft = _commit_confirmation(store, clock)
    store.close()

    connection = sqlite3.connect(str(database))
    try:
        for column, value in (
            ('authority_kind', 'tool'),
            ('eligible_for_execution', 1),
            ('execution_authorized', 1),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f'''
                    UPDATE confirmation_intents
                    SET {column} = ?
                    WHERE confirmation_request_id = ?
                    ''',
                    (value, draft.confirmation_request_id),
                )
            connection.rollback()
    finally:
        connection.close()
