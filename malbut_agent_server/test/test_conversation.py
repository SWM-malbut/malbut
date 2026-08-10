"""Tests for durable, user-isolated short-term conversations."""

import json
import sqlite3
import threading
from typing import Any, Dict

import pytest

from malbut_agent_server.conversation import (
    BeginTurnResult,
    ConversationChangedError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStateError,
    SQLiteConversationStore,
)


class FakeClock:
    """Controllable wall clock for deterministic expiry tests."""

    def __init__(self, now: float = 1000.0) -> None:
        """Start the clock at a deterministic timestamp."""
        self.now = now

    def __call__(self) -> float:
        """Return the current fake timestamp."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the fake timestamp forward."""
        self.now += seconds


def _response(number: int) -> Dict[str, Any]:
    return {
        'schema_version': 1,
        'decision': {
            'type': 'message',
            'message': f'로봇 답변 {number}',
            'tool_name': None,
            'arguments': {},
        },
    }


def _begin(
    store: SQLiteConversationStore,
    number: int,
    *,
    user_id: str = 'user-a',
    conversation_id: str = 'conversation-a',
    request_id: str = '',
    turn_id: str = '',
    fingerprint: str = '',
    content: str = '',
) -> BeginTurnResult:
    return store.begin_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        turn_id=turn_id or f'turn-{number}',
        request_id=request_id or f'request-{number}',
        request_fingerprint=fingerprint or f'fingerprint-{number}',
        user_content=content or f'사용자 발화 {number}',
    )


def _complete(
    store: SQLiteConversationStore,
    number: int,
    **begin_overrides: str,
) -> BeginTurnResult:
    result = _begin(store, number, **begin_overrides)
    assert result.token is not None
    store.complete_turn(
        result.token,
        f'로봇 답변 {number}',
        _response(number),
    )
    return result


def test_create_get_close_delete_lifecycle_and_ordered_messages() -> None:
    """Lifecycle operations preserve ordered history until deletion."""
    store = SQLiteConversationStore(':memory:')
    try:
        created = store.create('user-a', 'conversation-a')
        assert created.status == 'active'
        assert created.generation == 1
        assert created.revision == 0
        assert store.create('user-a', 'conversation-a') == created
        assert store.get('user-a', 'conversation-a') == created

        _complete(store, 1)
        _complete(store, 2)
        turns = store.list_turns('user-a', 'conversation-a')
        assert [turn.ordinal for turn in turns] == [1, 2]
        assert [turn.user_content for turn in turns] == [
            '사용자 발화 1',
            '사용자 발화 2',
        ]
        assert [
            message['role']
            for turn in turns
            for message in turn.to_messages()
        ] == ['user', 'assistant', 'user', 'assistant']
        assert [
            message['sequence']
            for turn in turns
            for message in turn.to_messages()
        ] == [1, 2, 3, 4]

        closed = store.close_session('user-a', 'conversation-a')
        assert closed.status == 'closed'
        assert store.close_session(
            'user-a',
            'conversation-a',
        ) == closed
        assert len(
            store.list_turns('user-a', 'conversation-a')
        ) == 2
        with pytest.raises(ConversationStateError):
            _begin(store, 3)

        assert store.delete('user-a', 'conversation-a') is True
        assert store.delete('user-a', 'conversation-a') is False
        with pytest.raises(ConversationNotFoundError):
            store.get('user-a', 'conversation-a')

        recreated = store.create('user-a', 'conversation-a')
        assert recreated.status == 'active'
        assert store.list_turns(
            'user-a',
            'conversation-a',
        ) == []
    finally:
        store.close()


def test_history_keeps_latest_ten_completed_turns_in_order() -> None:
    """A model reservation receives at least the latest ten exchanges."""
    store = SQLiteConversationStore(
        ':memory:',
        history_limit=10,
    )
    try:
        store.create('user-a', 'conversation-a')
        for number in range(1, 13):
            result = _begin(store, number)
            expected_start = max(1, number - 10)
            assert [
                turn.ordinal for turn in result.history
            ] == list(range(expected_start, number))
            assert result.token is not None
            store.complete_turn(
                result.token,
                f'로봇 답변 {number}',
                _response(number),
            )

        next_turn = _begin(store, 13)
        assert [turn.turn_id for turn in next_turn.history] == [
            f'turn-{number}' for number in range(3, 13)
        ]
        assert [
            turn.user_content for turn in next_turn.history
        ] == [
            f'사용자 발화 {number}' for number in range(3, 13)
        ]
        assert next_turn.token is not None
        store.fail_turn(next_turn.token)
    finally:
        store.close()


def test_sessions_are_isolated_by_user_and_new_session_is_empty() -> None:
    """Identical IDs cannot mix histories across users or sessions."""
    store = SQLiteConversationStore(':memory:')
    try:
        store.create('user-a', 'shared-conversation')
        store.create('user-b', 'shared-conversation')
        _complete(
            store,
            1,
            user_id='user-a',
            conversation_id='shared-conversation',
            request_id='same-request',
            content='초코 이야기',
        )
        _complete(
            store,
            1,
            user_id='user-b',
            conversation_id='shared-conversation',
            request_id='same-request',
            content='보리 이야기',
        )

        turns_a = store.list_turns(
            'user-a',
            'shared-conversation',
        )
        turns_b = store.list_turns(
            'user-b',
            'shared-conversation',
        )
        assert [turn.user_content for turn in turns_a] == [
            '초코 이야기'
        ]
        assert [turn.user_content for turn in turns_b] == [
            '보리 이야기'
        ]
        with pytest.raises(ConversationNotFoundError):
            store.get('user-c', 'shared-conversation')

        store.create('user-a', 'new-conversation')
        first = _begin(
            store,
            2,
            user_id='user-a',
            conversation_id='new-conversation',
        )
        assert first.history == ()
        assert first.token is not None
        store.fail_turn(first.token)
    finally:
        store.close()


def test_reset_starts_new_generation_without_old_short_term_context() -> None:
    """Reset hides old turns and invalidates their request receipts."""
    store = SQLiteConversationStore(':memory:')
    try:
        store.create('user-a', 'conversation-a')
        _complete(store, 1)

        reset = store.reset('user-a', 'conversation-a')
        assert reset.status == 'active'
        assert reset.generation == 2
        assert reset.revision == 2
        assert store.list_turns(
            'user-a',
            'conversation-a',
        ) == []

        with pytest.raises(
            ConversationConflictError,
            match='previous conversation generation',
        ):
            _begin(store, 1)

        fresh = _begin(store, 2)
        assert fresh.history == ()
        assert fresh.token is not None
        assert fresh.token.generation == 2
        assert fresh.token.ordinal == 1
        store.complete_turn(
            fresh.token,
            '새 세션 답변',
            _response(2),
        )
        turns = store.list_turns('user-a', 'conversation-a')
        assert [turn.generation for turn in turns] == [2]
        assert [turn.ordinal for turn in turns] == [1]
    finally:
        store.close()


@pytest.mark.parametrize('operation', ['reset', 'close_session'])
def test_lifecycle_change_invalidates_in_flight_turn(
    operation: str,
) -> None:
    """An old inference cannot append after reset or close."""
    store = SQLiteConversationStore(':memory:')
    try:
        store.create('user-a', 'conversation-a')
        pending = _begin(store, 1)
        assert pending.token is not None

        getattr(store, operation)('user-a', 'conversation-a')
        with pytest.raises(ConversationChangedError):
            store.complete_turn(
                pending.token,
                '늦게 도착한 답변',
                _response(1),
            )
        assert store.list_turns(
            'user-a',
            'conversation-a',
        ) == []
    finally:
        store.close()


def test_idle_expiry_is_exact_and_reads_do_not_extend_it() -> None:
    """Expiry occurs at the deadline and a read is not activity."""
    clock = FakeClock()
    store = SQLiteConversationStore(
        ':memory:',
        ttl_seconds=60,
        clock=clock,
    )
    try:
        created = store.create('user-a', 'conversation-a')
        assert created.expires_at == 1060.0

        clock.advance(59)
        read = store.get('user-a', 'conversation-a')
        assert read.status == 'active'
        assert read.expires_at == 1060.0

        clock.advance(1)
        expired = store.get('user-a', 'conversation-a')
        assert expired.status == 'expired'
        assert expired.generation == 2
        assert expired.revision == 1
        with pytest.raises(
            ConversationStateError,
            match='expired',
        ):
            _begin(store, 1)
        assert store.purge_expired() == 0
    finally:
        store.close()


def test_expiry_invalidates_in_flight_turn() -> None:
    """A response finishing after idle expiry is never committed."""
    clock = FakeClock()
    store = SQLiteConversationStore(
        ':memory:',
        ttl_seconds=60,
        clock=clock,
    )
    try:
        store.create('user-a', 'conversation-a')
        pending = _begin(store, 1)
        assert pending.token is not None
        clock.advance(60)

        with pytest.raises(ConversationChangedError):
            store.complete_turn(
                pending.token,
                '만료 후 답변',
                _response(1),
            )
        assert store.get(
            'user-a',
            'conversation-a',
        ).status == 'expired'
        assert store.list_turns(
            'user-a',
            'conversation-a',
        ) == []
    finally:
        store.close()


def test_exact_retry_is_durable_and_changed_retry_conflicts(
    tmp_path,
) -> None:
    """Completed retries survive restart without creating a new turn."""
    database = tmp_path / 'conversation.sqlite3'
    original_response = _response(1)
    first_store = SQLiteConversationStore(str(database))
    try:
        first_store.create('user-a', 'conversation-a')
        pending = _begin(first_store, 1)
        assert pending.token is not None
        first_store.complete_turn(
            pending.token,
            '로봇 답변 1',
            original_response,
        )
    finally:
        first_store.close()

    store = SQLiteConversationStore(str(database))
    try:
        cached = _begin(store, 1)
        assert cached.token is None
        assert cached.history == ()
        assert cached.cached_response == original_response
        assert len(
            store.list_turns('user-a', 'conversation-a')
        ) == 1

        changed_inputs = (
            {'fingerprint': 'different-fingerprint'},
            {'turn_id': 'different-turn'},
            {'content': '다른 사용자 발화'},
        )
        for changed in changed_inputs:
            with pytest.raises(
                ConversationConflictError,
                match='different input',
            ):
                _begin(store, 1, **changed)

        with pytest.raises(
            ConversationConflictError,
            match='turn_id was already used',
        ):
            _begin(
                store,
                2,
                turn_id='turn-1',
            )
    finally:
        store.close()


def test_version_02_database_backfills_session_instance(
    tmp_path,
) -> None:
    """Existing sessions and turns gain one matching opaque identity."""
    database = tmp_path / 'legacy-conversation.sqlite3'
    connection = sqlite3.connect(str(database))
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
        VALUES (?, ?, 'active', 1, 1, 1, 1, 9999999999)
        ''',
        ('user-a', 'conversation-a'),
    )
    connection.execute(
        '''
        INSERT INTO conversation_turns
        VALUES (
            ?, ?, ?, ?, ?, 1, 1, 'completed', ?, ?, ?, 1, 1
        )
        ''',
        (
            'user-a',
            'conversation-a',
            'turn-1',
            'request-1',
            'fingerprint-1',
            '사용자 발화 1',
            '로봇 답변 1',
            json.dumps(_response(1), ensure_ascii=False),
        ),
    )
    connection.commit()
    connection.close()

    store = SQLiteConversationStore(str(database))
    try:
        session = store.get('user-a', 'conversation-a')
        turns = store.list_turns('user-a', 'conversation-a')
        assert session.session_instance_id
        assert turns[0].session_instance_id == (
            session.session_instance_id
        )
        assert store.get_summary(
            'user-a',
            'conversation-a',
        ) is None
    finally:
        store.close()


def test_concurrent_reservation_allows_only_one_in_flight_turn() -> None:
    """Concurrent requests for one session cannot take two ordinals."""
    store = SQLiteConversationStore(':memory:')
    barrier = threading.Barrier(3)
    results = []
    errors = []
    result_lock = threading.Lock()

    def reserve(number: int) -> None:
        barrier.wait()
        try:
            result = _begin(store, number)
            with result_lock:
                results.append(result)
        except Exception as error:  # noqa: B902
            with result_lock:
                errors.append(error)

    try:
        store.create('user-a', 'conversation-a')
        threads = [
            threading.Thread(target=reserve, args=(number,))
            for number in (1, 2)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ConversationConflictError)
        assert 'already in progress' in str(errors[0])
        assert results[0].token is not None
        assert results[0].token.ordinal == 1
        store.fail_turn(results[0].token)
    finally:
        store.close()
