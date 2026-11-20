"""SQLite schema DDL, verification, and legacy migration helpers."""

import sqlite3
import uuid
from typing import Optional

from malbut_agent_server.conversation_store.models import (
    CONFIRMATION_STORAGE_SCHEMA_VERSION,
    ConfirmationSchemaError,
)


CONFIRMATION_SCHEMA_METADATA_SQL = '''
CREATE TABLE confirmation_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
)
'''


CONFIRMATION_INTENTS_SQL = '''
CREATE TABLE confirmation_intents (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    turn_id TEXT NOT NULL,
    agent_request_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL CHECK (expires_at > issued_at),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'resolved', 'invalidated')
    ),
    disposition TEXT NOT NULL CHECK (
        disposition IN (
            'pending', 'approved', 'denied', 'canceled',
            'expired', 'invalidated'
        )
    ),
    requested_disposition TEXT CHECK (
        requested_disposition IS NULL OR requested_disposition IN (
            'approve', 'deny', 'cancel'
        )
    ),
    result_code TEXT NOT NULL,
    response_id TEXT,
    response_turn_id TEXT,
    response_fingerprint TEXT,
    resolved_at REAL,
    record_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    authority_kind TEXT NOT NULL DEFAULT 'none'
        CHECK (authority_kind = 'none'),
    execution_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (execution_authorized = 0),
    consume_once INTEGER NOT NULL DEFAULT 0
        CHECK (consume_once = 0),
    tool_call_id TEXT CHECK (tool_call_id IS NULL),
    mission_id TEXT CHECK (mission_id IS NULL),
    UNIQUE (user_id, agent_request_id),
    UNIQUE (user_id, decision_id),
    UNIQUE (user_id, proposal_fingerprint),
    FOREIGN KEY (user_id, conversation_id)
        REFERENCES conversation_sessions (user_id, conversation_id)
        ON DELETE CASCADE,
    CHECK (
        (state = 'pending'
         AND disposition = 'pending'
         AND requested_disposition IS NULL
         AND result_code = 'confirmation_pending'
         AND response_id IS NULL
         AND response_turn_id IS NULL
         AND response_fingerprint IS NULL
         AND resolved_at IS NULL)
        OR
        (state = 'resolved'
         AND disposition IN (
             'approved', 'denied', 'canceled', 'expired'
         )
         AND result_code != 'confirmation_pending'
         AND resolved_at IS NOT NULL
         AND (
             (response_id IS NULL
              AND response_turn_id IS NULL
              AND response_fingerprint IS NULL
              AND requested_disposition IS NULL
              AND disposition = 'expired')
             OR
             (response_id IS NOT NULL
              AND response_turn_id IS NOT NULL
              AND response_fingerprint IS NOT NULL
              AND requested_disposition IS NOT NULL)
         ))
        OR
        (state = 'invalidated'
         AND disposition = 'invalidated'
         AND requested_disposition IS NULL
         AND result_code != 'confirmation_pending'
         AND response_id IS NULL
         AND response_turn_id IS NULL
         AND response_fingerprint IS NULL
         AND resolved_at IS NOT NULL)
    )
)
'''


CONFIRMATION_RESPONSE_OWNER_INDEX_SQL = '''
CREATE UNIQUE INDEX confirmation_response_owner_idx
ON confirmation_intents (user_id, response_id)
WHERE response_id IS NOT NULL
'''


CONFIRMATION_ONE_PENDING_INDEX_SQL = '''
CREATE UNIQUE INDEX confirmation_one_pending_session_idx
ON confirmation_intents (
    user_id, session_instance_id, generation
)
WHERE state = 'pending'
'''


TEXT_TURN_REQUEST_CLAIMS_SQL = '''
CREATE TABLE text_turn_request_claims (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    turn_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN (
            'confirmation_unrecognized',
            'confirmation_not_pending',
            'confirmation_resolved',
            'confirmation_invalidated'
        )
    ),
    confirmation_request_id TEXT,
    created_at REAL NOT NULL,
    authority_kind TEXT NOT NULL DEFAULT 'none'
        CHECK (authority_kind = 'none'),
    execution_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (execution_authorized = 0),
    consume_once INTEGER NOT NULL DEFAULT 0
        CHECK (consume_once = 0),
    tool_call_id TEXT CHECK (tool_call_id IS NULL),
    mission_id TEXT CHECK (mission_id IS NULL),
    PRIMARY KEY (user_id, request_id),
    UNIQUE (
        user_id, conversation_id, session_instance_id, generation, turn_id
    ),
    FOREIGN KEY (user_id, conversation_id)
        REFERENCES conversation_sessions (user_id, conversation_id)
        ON DELETE CASCADE,
    FOREIGN KEY (confirmation_request_id)
        REFERENCES confirmation_intents (confirmation_request_id)
        ON DELETE CASCADE,
    CHECK (
        (outcome = 'confirmation_not_pending'
         AND confirmation_request_id IS NULL)
        OR
        (outcome != 'confirmation_not_pending'
         AND confirmation_request_id IS NOT NULL)
    )
)
'''


CONFIRMATION_SCHEMA_OBJECTS = {
    'confirmation_schema_metadata': (
        'table',
        CONFIRMATION_SCHEMA_METADATA_SQL,
    ),
    'confirmation_intents': (
        'table',
        CONFIRMATION_INTENTS_SQL,
    ),
    'confirmation_response_owner_idx': (
        'index',
        CONFIRMATION_RESPONSE_OWNER_INDEX_SQL,
    ),
    'confirmation_one_pending_session_idx': (
        'index',
        CONFIRMATION_ONE_PENDING_INDEX_SQL,
    ),
    'text_turn_request_claims': (
        'table',
        TEXT_TURN_REQUEST_CLAIMS_SQL,
    ),
}


def normalized_schema_sql(value: Optional[str]) -> str:
    """Normalize SQLite DDL text for strict confirmation schema comparison."""
    if value is None:
        return ''
    return ' '.join(value.strip().rstrip(';').split()).lower()


def initialize_conversation_schema(connection: sqlite3.Connection) -> None:
    """Create or validate all conversation, summary, trace, and confirmation tables."""
    from malbut_agent_server.memory_trace import initialize_trace_schema

    initialize_trace_schema(connection)
    connection.execute(
        '''
        CREATE TABLE IF NOT EXISTS conversation_sessions (
            user_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            session_instance_id TEXT NOT NULL,
            status TEXT NOT NULL,
            generation INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            PRIMARY KEY (user_id, conversation_id),
            CHECK (status IN ('active', 'closed', 'expired'))
        )
        '''
    )
    connection.execute(
        '''
        CREATE TABLE IF NOT EXISTS conversation_turns (
            user_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            session_instance_id TEXT NOT NULL,
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
            ),
            FOREIGN KEY (user_id, conversation_id)
                REFERENCES conversation_sessions (
                    user_id,
                    conversation_id
                )
                ON DELETE CASCADE,
            CHECK (status IN ('pending', 'completed'))
        )
        '''
    )
    ensure_session_instance_columns(connection)
    connection.execute(
        '''
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            user_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            session_instance_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            summary_id TEXT NOT NULL,
            summary_revision INTEGER NOT NULL,
            content TEXT NOT NULL,
            state_json TEXT NOT NULL,
            source_start_ordinal INTEGER NOT NULL,
            source_end_ordinal INTEGER NOT NULL,
            source_turn_count INTEGER NOT NULL,
            source_digest TEXT NOT NULL,
            summarizer TEXT NOT NULL,
            fallback_used INTEGER NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (
                user_id,
                conversation_id,
                session_instance_id,
                generation
            ),
            FOREIGN KEY (user_id, conversation_id)
                REFERENCES conversation_sessions (
                    user_id,
                    conversation_id
                )
                ON DELETE CASCADE
        )
        '''
    )
    connection.execute(
        '''
        CREATE INDEX IF NOT EXISTS conversation_turns_order_idx
        ON conversation_turns (
            user_id,
            conversation_id,
            generation,
            ordinal DESC
        )
        '''
    )
    connection.execute(
        '''
        CREATE UNIQUE INDEX IF NOT EXISTS
            conversation_one_pending_idx
        ON conversation_turns (user_id, conversation_id)
        WHERE status = 'pending'
        '''
    )
    initialize_confirmation_schema(connection)


def initialize_confirmation_schema(connection: sqlite3.Connection) -> None:
    """Create or strictly validate the additive confirmation schema."""
    object_names = tuple(CONFIRMATION_SCHEMA_OBJECTS)
    placeholders = ', '.join('?' for _name in object_names)
    rows = connection.execute(
        f'''
        SELECT type, name, sql
        FROM sqlite_master
        WHERE name IN ({placeholders})
        ''',
        object_names,
    ).fetchall()
    found = {str(row['name']): row for row in rows}
    expected_names = set(CONFIRMATION_SCHEMA_OBJECTS)
    if not found:
        connection.execute(CONFIRMATION_SCHEMA_METADATA_SQL)
        connection.execute(CONFIRMATION_INTENTS_SQL)
        connection.execute(CONFIRMATION_RESPONSE_OWNER_INDEX_SQL)
        connection.execute(CONFIRMATION_ONE_PENDING_INDEX_SQL)
        connection.execute(TEXT_TURN_REQUEST_CLAIMS_SQL)
        connection.execute(
            '''
            INSERT INTO confirmation_schema_metadata (
                singleton, schema_version
            ) VALUES (1, ?)
            ''',
            (CONFIRMATION_STORAGE_SCHEMA_VERSION,),
        )
        return
    if set(found) != expected_names:
        raise ConfirmationSchemaError(
            'confirmation persistence schema is incomplete'
        )
    for name, (object_type, expected_sql) in (
        CONFIRMATION_SCHEMA_OBJECTS.items()
    ):
        row = found[name]
        if row['type'] != object_type:
            raise ConfirmationSchemaError(
                'confirmation persistence schema object type '
                f'is invalid: {name}'
            )
        if normalized_schema_sql(row['sql']) != (
            normalized_schema_sql(expected_sql)
        ):
            raise ConfirmationSchemaError(
                'confirmation persistence schema does not match '
                f'version {CONFIRMATION_STORAGE_SCHEMA_VERSION}: '
                f'{name}'
            )
    metadata_rows = connection.execute(
        '''
        SELECT singleton, schema_version
        FROM confirmation_schema_metadata
        '''
    ).fetchall()
    if (
        len(metadata_rows) != 1
        or int(metadata_rows[0]['singleton']) != 1
        or int(metadata_rows[0]['schema_version'])
        != CONFIRMATION_STORAGE_SCHEMA_VERSION
    ):
        raise ConfirmationSchemaError(
            'confirmation persistence schema version is invalid'
        )


def ensure_session_instance_columns(connection: sqlite3.Connection) -> None:
    """Add opaque session identities to databases from version 0.2."""
    session_columns = {
        row['name']
        for row in connection.execute(
            'PRAGMA table_info(conversation_sessions)'
        ).fetchall()
    }
    if 'session_instance_id' not in session_columns:
        connection.execute(
            '''
            ALTER TABLE conversation_sessions
            ADD COLUMN session_instance_id TEXT
            '''
        )
    turn_columns = {
        row['name']
        for row in connection.execute(
            'PRAGMA table_info(conversation_turns)'
        ).fetchall()
    }
    if 'session_instance_id' not in turn_columns:
        connection.execute(
            '''
            ALTER TABLE conversation_turns
            ADD COLUMN session_instance_id TEXT
            '''
        )
    rows = connection.execute(
        '''
        SELECT user_id, conversation_id, session_instance_id
        FROM conversation_sessions
        '''
    ).fetchall()
    for row in rows:
        instance_id = row['session_instance_id']
        if not instance_id:
            instance_id = str(uuid.uuid4())
            connection.execute(
                '''
                UPDATE conversation_sessions
                SET session_instance_id = ?
                WHERE user_id = ? AND conversation_id = ?
                ''',
                (
                    instance_id,
                    row['user_id'],
                    row['conversation_id'],
                ),
            )
        connection.execute(
            '''
            UPDATE conversation_turns
            SET session_instance_id = ?
            WHERE user_id = ? AND conversation_id = ?
              AND (
                  session_instance_id IS NULL
                  OR session_instance_id = ''
              )
            ''',
            (
                instance_id,
                row['user_id'],
                row['conversation_id'],
            ),
        )
