"""Tests for durable, user-isolated short-term conversations."""

import json
import os
import sqlite3
import threading
from dataclasses import FrozenInstanceError, replace
from typing import Any, Dict, Optional

import pytest

from malbut_agent_server.conversation import (
    BeginTurnResult,
    ConversationBusyError,
    ConversationChangedError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStateError,
    ConversationTrustedToolResult,
    SQLiteConversationStore,
    TrustedRoomMissionTerminalResult,
    TrustedToolResultCommit,
)
from malbut_agent_server.schemas import (
    MAX_UTTERANCE_LENGTH,
    ValidationError,
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


def _trusted_result(
    *,
    status: str = 'succeeded',
    code: str = 'simulation_succeeded',
    source: str = 'simulation_adapter',
    sequence: int = 7,
) -> TrustedRoomMissionTerminalResult:
    return TrustedRoomMissionTerminalResult(
        terminal_digest='a' * 64,
        status=status,
        code=code,
        source=source,
        sequence=sequence,
    )


def _trusted_envelope(
    session,
    *,
    feedback_id: str = 'room-feedback-1',
    request_id: str = 'room-feedback-request-1',
    tool_call_id: str = 'room-tool-call-1',
    user_id: str = 'user-a',
    conversation_id: str = 'conversation-a',
    session_instance_id: str = '',
    generation: int = 0,
    source_revision: int = -1,
    result: Optional[TrustedRoomMissionTerminalResult] = None,
) -> ConversationTrustedToolResult:
    return ConversationTrustedToolResult(
        feedback_id=feedback_id,
        request_id=request_id,
        tool_call_id=tool_call_id,
        user_id=user_id,
        conversation_id=conversation_id,
        session_instance_id=(
            session_instance_id or session.session_instance_id
        ),
        generation=generation or session.generation,
        source_revision=(
            session.revision
            if source_revision < 0
            else source_revision
        ),
        result=result or _trusted_result(),
    )


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


def test_exact_completed_turn_lookup_ignores_history_window() -> None:
    """Trusted evidence lookup can resolve an older current turn exactly."""
    store = SQLiteConversationStore(
        ':memory:',
        history_limit=10,
        max_turns_per_session=1000,
    )
    try:
        store.create('user-a', 'conversation-a')
        for number in range(1, 502):
            _complete(store, number)

        oldest = store.get_completed_turn(
            'user-a',
            'conversation-a',
            'turn-1',
        )
        assert oldest.ordinal == 1
        assert oldest.user_id == 'user-a'

        with pytest.raises(ConversationNotFoundError):
            store.get_completed_turn(
                'user-b',
                'conversation-a',
                'turn-1',
            )
        with pytest.raises(ConversationNotFoundError):
            store.get_completed_turn(
                'user-a',
                'conversation-a',
                'turn-missing',
            )

        store.reset('user-a', 'conversation-a')
        with pytest.raises(ConversationNotFoundError):
            store.get_completed_turn(
                'user-a',
                'conversation-a',
                'turn-1',
            )
    finally:
        store.close()


def test_pending_turn_is_not_completed_evidence() -> None:
    """Exact lookup never returns a pending conversation turn."""
    store = SQLiteConversationStore(':memory:')
    try:
        store.create('user-a', 'conversation-a')
        pending = _begin(store, 1)
        assert pending.token is not None
        with pytest.raises(ConversationNotFoundError):
            store.get_completed_turn(
                'user-a',
                'conversation-a',
                'turn-1',
            )
        store.fail_turn(pending.token)
    finally:
        store.close()


@pytest.mark.parametrize('terminal_state', ['closed', 'expired'])
def test_inactive_turn_is_not_confirmation_evidence(
    terminal_state: str,
) -> None:
    """A closed or expired session cannot authorize a new mutation."""
    clock = FakeClock()
    store = SQLiteConversationStore(
        ':memory:',
        ttl_seconds=60,
        clock=clock,
    )
    try:
        store.create('user-a', 'conversation-a')
        _complete(store, 1)
        if terminal_state == 'closed':
            store.close_session('user-a', 'conversation-a')
        else:
            clock.advance(60)
        with pytest.raises(ConversationStateError):
            store.get_completed_turn(
                'user-a',
                'conversation-a',
                'turn-1',
            )
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


def test_snapshot_is_atomic_and_missing_session_rolls_back() -> None:
    """Snapshot returns one generation and leaves no transaction on failure."""
    store = SQLiteConversationStore(':memory:')
    try:
        generated = store.create('user-a')
        _complete(
            store,
            1,
            conversation_id=generated.conversation_id,
        )

        snapshot = store.snapshot(
            'user-a',
            generated.conversation_id,
        )
        assert snapshot.session == store.get(
            'user-a',
            generated.conversation_id,
        )
        assert [turn.ordinal for turn in snapshot.turns] == [1]
        assert snapshot.summary is None
        assert snapshot.session.to_dict()['conversation_id'] == (
            generated.conversation_id
        )
        assert snapshot.turns[0].to_dict()['decision']['type'] == (
            'message'
        )

        with pytest.raises(ConversationNotFoundError):
            store.snapshot('user-a', 'missing-conversation')
        with pytest.raises(ValueError, match='turn limit'):
            store.snapshot('user-a', generated.conversation_id, limit=0)

        assert store.get('user-a', generated.conversation_id).status == (
            'active'
        )
    finally:
        store.close()


def test_terminal_create_close_and_session_limit_fail_closed() -> None:
    """Terminal IDs stay reserved and per-user session limits are enforced."""
    clock = FakeClock()
    store = SQLiteConversationStore(
        ':memory:',
        ttl_seconds=60,
        max_sessions_per_user=1,
        clock=clock,
    )
    try:
        store.create('user-a', 'conversation-a')
        with pytest.raises(ConversationStateError, match='limit'):
            store.create('user-a', 'conversation-b')

        closed = store.close_session('user-a', 'conversation-a')
        assert closed.status == 'closed'
        with pytest.raises(ConversationConflictError, match='closed'):
            store.create('user-a', 'conversation-a')
        with pytest.raises(ConversationNotFoundError):
            store.close_session('user-b', 'conversation-a')
    finally:
        store.close()

    expiring = SQLiteConversationStore(
        ':memory:',
        ttl_seconds=60,
        clock=clock,
    )
    try:
        expiring.create('user-a', 'expired-conversation')
        clock.advance(60)
        with pytest.raises(ConversationStateError, match='expired'):
            expiring.close_session('user-a', 'expired-conversation')
        with pytest.raises(ConversationConflictError, match='expired'):
            expiring.create('user-a', 'expired-conversation')
    finally:
        expiring.close()


def test_turn_limit_and_strict_completion_payloads_fail_closed() -> None:
    """Bounded turns and response fields reject oversized or forged values."""
    store = SQLiteConversationStore(
        ':memory:',
        max_turns_per_session=10,
    )
    try:
        store.create('user-a', 'conversation-a')
        for number in range(1, 11):
            _complete(store, number)
        with pytest.raises(ConversationStateError, match='turn limit'):
            _begin(store, 11)

        store.reset('user-a', 'conversation-a')
        pending = _begin(store, 20)
        assert pending.token is not None
        with pytest.raises(ValidationError, match='assistant_content'):
            store.complete_turn(
                pending.token,
                object(),  # type: ignore[arg-type]
                _response(20),
            )
        with pytest.raises(ValidationError, match='too long'):
            store.complete_turn(
                pending.token,
                'x' * (MAX_UTTERANCE_LENGTH + 1),
                _response(20),
            )
        with pytest.raises(ValidationError, match='object'):
            store.complete_turn(
                pending.token,
                'valid assistant response',
                [],  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError, match='too large'):
            store.complete_turn(
                pending.token,
                'valid assistant response',
                {'payload': 'x' * 65536},
            )

        assert store.list_turns('user-a', 'conversation-a') == []
        store.fail_turn(pending.token)
    finally:
        store.close()


def test_concurrent_exact_duplicate_reports_pending_conflict() -> None:
    """Concurrent exact retries share no response before commit completes."""
    store = SQLiteConversationStore(':memory:')
    barrier = threading.Barrier(3)
    results = []
    errors = []
    result_lock = threading.Lock()

    def reserve_same_request() -> None:
        barrier.wait()
        try:
            result = _begin(store, 1)
            with result_lock:
                results.append(result)
        except Exception as error:  # noqa: B902
            with result_lock:
                errors.append(error)

    try:
        store.create('user-a', 'conversation-a')
        threads = [
            threading.Thread(target=reserve_same_request)
            for _index in range(2)
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
        assert 'same turn is already in progress' in str(errors[0])
        assert results[0].token is not None
        store.fail_turn(results[0].token)
    finally:
        store.close()


@pytest.mark.parametrize(
    ('keyword', 'value', 'message'),
    [
        ('database_path', '', 'database_path'),
        ('ttl_seconds', True, 'integer'),
        ('ttl_seconds', 59, 'between'),
        ('history_limit', 9, 'between'),
        ('max_sessions_per_user', 0, 'between'),
        ('max_turns_per_session', 9, 'between'),
        ('summary_max_chars', 255, 'between'),
    ],
)
def test_store_configuration_is_strictly_bounded(
    keyword: str,
    value: object,
    message: str,
) -> None:
    """Invalid persistence bounds cannot silently weaken retention limits."""
    arguments = {'database_path': ':memory:'}
    arguments[keyword] = value
    with pytest.raises(ValueError, match=message):
        SQLiteConversationStore(**arguments)  # type: ignore[arg-type]


def test_nonfinite_clock_is_rejected_before_lifecycle_mutation() -> None:
    """A non-finite clock cannot create an immortal or expired session."""
    clock = FakeClock(float('nan'))
    store = SQLiteConversationStore(':memory:', clock=clock)
    try:
        with pytest.raises(RuntimeError, match='clock'):
            store.create('user-a', 'conversation-a')
        clock.now = 1000.0
        with pytest.raises(ConversationNotFoundError):
            store.get('user-a', 'conversation-a')
    finally:
        store.close()


def test_trusted_tool_result_exact_replay_survives_reopen(
    tmp_path,
) -> None:
    """One terminal result has one durable receipt and no fake turn."""
    database_path = os.fspath(tmp_path / 'conversation.sqlite3')
    first = SQLiteConversationStore(database_path)
    try:
        session = first.create('user-a', 'conversation-a')
        envelope = _trusted_envelope(session)
        expires_at = session.expires_at
        committed = first.append_trusted_tool_result(envelope)
        assert type(committed) is TrustedToolResultCommit
        assert committed.cached is False
        assert committed.conversation_revision_after == 1
        assert first.get('user-a', 'conversation-a').expires_at == (
            expires_at
        )
        assert first.list_turns('user-a', 'conversation-a') == []
    finally:
        first.close()

    reopened = SQLiteConversationStore(database_path)
    try:
        replay = reopened.append_trusted_tool_result(envelope)
        assert replay.cached is True
        assert replay.commit_id == committed.commit_id
        assert replay.conversation_revision_after == (
            committed.conversation_revision_after
        )
        assert replay.committed_at == committed.committed_at
        listed = reopened.list_trusted_tool_results(
            'user-a',
            'conversation-a',
        )
        assert len(listed) == 1
        assert listed[0].commit_id == committed.commit_id
        exact = reopened.get_trusted_tool_result(
            'user-a',
            'conversation-a',
            envelope.feedback_id,
        )
        assert exact.commit_id == committed.commit_id
        snapshot = reopened.snapshot('user-a', 'conversation-a')
        assert snapshot.turns == ()
        assert len(snapshot.trusted_tool_results) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize(
    'changed',
    [
        {'feedback_id': 'room-feedback-changed'},
        {'request_id': 'room-feedback-request-changed'},
        {'tool_call_id': 'room-tool-call-changed'},
        {'source_revision': 1},
        {
            'result': _trusted_result(
                status='failed',
                code='recovery_unavailable',
                source='recovery',
            ),
        },
    ],
)
def test_trusted_tool_result_changed_retry_conflicts_generically(
    changed,
) -> None:
    """Reusing any one idempotency key never accepts changed input."""
    store = SQLiteConversationStore(':memory:')
    try:
        session = store.create('user-a', 'conversation-a')
        original = _trusted_envelope(session)
        store.append_trusted_tool_result(original)
        forged = replace(original, **changed)
        with pytest.raises(ConversationConflictError) as caught:
            store.append_trusted_tool_result(forged)
        rendered = str(caught.value)
        assert 'room-feedback-1' not in rendered
        assert 'room-tool-call-1' not in rendered
        assert store.get('user-a', 'conversation-a').revision == 1
    finally:
        store.close()


def test_trusted_tool_result_cross_owner_keys_do_not_leak() -> None:
    """Global feedback and Tool ids cannot be probed through another owner."""
    store = SQLiteConversationStore(':memory:')
    try:
        first = store.create('user-a', 'conversation-a')
        second = store.create('user-b', 'conversation-b')
        original = _trusted_envelope(first)
        store.append_trusted_tool_result(original)
        forged = _trusted_envelope(
            second,
            user_id='user-b',
            conversation_id='conversation-b',
            feedback_id=original.feedback_id,
            request_id='unrelated-request',
            tool_call_id='unrelated-tool',
        )
        with pytest.raises(ConversationConflictError) as caught:
            store.append_trusted_tool_result(forged)
        assert original.feedback_id not in str(caught.value)
        with pytest.raises(ConversationNotFoundError):
            store.get_trusted_tool_result(
                'user-b',
                'conversation-b',
                original.feedback_id,
            )
    finally:
        store.close()


def test_trusted_tool_result_requires_exact_active_destination() -> None:
    """Instance, generation, and source revision are fail-closed."""
    store = SQLiteConversationStore(':memory:')
    try:
        session = store.create('user-a', 'conversation-a')
        invalid = (
            replace(
                _trusted_envelope(session),
                session_instance_id='wrong-session-instance',
            ),
            replace(_trusted_envelope(session), generation=2),
            replace(_trusted_envelope(session), source_revision=1),
        )
        for envelope in invalid:
            with pytest.raises(ConversationChangedError):
                store.append_trusted_tool_result(envelope)
            assert store.get('user-a', 'conversation-a').revision == 0

        wrong_owner = replace(
            _trusted_envelope(session),
            user_id='user-b',
        )
        with pytest.raises(ConversationNotFoundError):
            store.append_trusted_tool_result(wrong_owner)
        store.close_session('user-a', 'conversation-a')
        with pytest.raises(ConversationStateError, match='closed'):
            store.append_trusted_tool_result(_trusted_envelope(session))
    finally:
        store.close()


def test_pending_turn_makes_trusted_result_retryably_busy() -> None:
    """A Tool result cannot invalidate an in-flight turn CAS token."""
    store = SQLiteConversationStore(':memory:')
    try:
        session = store.create('user-a', 'conversation-a')
        pending = _begin(store, 1)
        assert pending.token is not None
        with pytest.raises(ConversationBusyError) as caught:
            store.append_trusted_tool_result(
                _trusted_envelope(session)
            )
        assert caught.value.retryable is True
        assert store.get('user-a', 'conversation-a').revision == 0
        assert store.list_trusted_tool_results(
            'user-a', 'conversation-a'
        ) == []
        store.fail_turn(pending.token)
        committed = store.append_trusted_tool_result(
            _trusted_envelope(session)
        )
        assert committed.conversation_revision_after == 1
    finally:
        store.close()


def test_trusted_result_request_namespace_is_cross_table() -> None:
    """Turn and Tool-result requests cannot alias in either direction."""
    store = SQLiteConversationStore(':memory:')
    try:
        session = store.create('user-a', 'conversation-a')
        _complete(store, 1, request_id='shared-request')
        with pytest.raises(ConversationConflictError):
            store.append_trusted_tool_result(
                _trusted_envelope(
                    store.get('user-a', 'conversation-a'),
                    request_id='shared-request',
                )
            )

        result = _trusted_envelope(
            store.get('user-a', 'conversation-a'),
            feedback_id='feedback-two',
            request_id='result-only-request',
            tool_call_id='tool-two',
        )
        store.append_trusted_tool_result(result)
        with pytest.raises(ConversationConflictError):
            _begin(store, 2, request_id='result-only-request')

        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(  # noqa: SLF001
                '''
                INSERT INTO conversation_turns (
                    user_id, conversation_id, session_instance_id,
                    turn_id, request_id, request_fingerprint,
                    generation, ordinal, status, user_content, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ''',
                (
                    'user-a',
                    'conversation-a',
                    session.session_instance_id,
                    'raw-turn',
                    'result-only-request',
                    'raw-fingerprint',
                    session.generation,
                    99,
                    'raw-content',
                    1.0,
                ),
            )
        store._connection.rollback()  # noqa: SLF001
    finally:
        store.close()


def test_concurrent_connections_commit_one_trusted_result(tmp_path) -> None:
    """BEGIN IMMEDIATE serializes exact duplicate delivery."""
    database = tmp_path / 'conversation.sqlite3'
    setup = SQLiteConversationStore(str(database))
    session = setup.create('user-a', 'conversation-a')
    setup.close()
    envelope = _trusted_envelope(session)
    barrier = threading.Barrier(3)
    commits = []
    errors = []
    output_lock = threading.Lock()

    def append() -> None:
        store = SQLiteConversationStore(str(database))
        try:
            barrier.wait()
            commit = store.append_trusted_tool_result(envelope)
            with output_lock:
                commits.append(commit)
        except Exception as error:  # noqa: B902
            with output_lock:
                errors.append(error)
        finally:
            store.close()

    threads = [threading.Thread(target=append) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(commits) == 2
    assert len({commit.commit_id for commit in commits}) == 1
    assert sorted(commit.cached for commit in commits) == [False, True]

    verifier = SQLiteConversationStore(str(database))
    try:
        assert verifier.get('user-a', 'conversation-a').revision == 1
        assert len(verifier.list_trusted_tool_results(
            'user-a', 'conversation-a'
        )) == 1
    finally:
        verifier.close()


def test_trusted_append_rechecks_expiry_after_waiting_for_writer(
    tmp_path,
) -> None:
    """A blocked append cannot commit with its pre-lock wall-clock time."""
    database = tmp_path / 'writer-expiry.sqlite3'
    clock = FakeClock()
    store = SQLiteConversationStore(
        str(database),
        ttl_seconds=60,
        clock=clock,
    )
    blocker = sqlite3.connect(str(database), timeout=5.0)
    attempted = threading.Event()
    result_box = {}
    try:
        session = store.create('user-a', 'conversation-a')
        envelope = _trusted_envelope(session)
        original_begin = store._begin  # noqa: SLF001

        def observed_begin() -> None:
            attempted.set()
            original_begin()

        store._begin = observed_begin  # noqa: SLF001
        blocker.execute('BEGIN IMMEDIATE')

        def append() -> None:
            try:
                result_box['result'] = (
                    store.append_trusted_tool_result(envelope)
                )
            except Exception as error:  # noqa: B902
                result_box['error'] = error

        worker = threading.Thread(target=append)
        worker.start()
        assert attempted.wait(timeout=5)
        clock.advance(61)
        blocker.commit()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert 'result' not in result_box
        assert isinstance(
            result_box.get('error'),
            ConversationStateError,
        )
        observer = sqlite3.connect(str(database))
        try:
            assert observer.execute(
                'SELECT COUNT(*) '
                'FROM conversation_trusted_tool_results'
            ).fetchone()[0] == 0
            assert observer.execute(
                'SELECT revision FROM conversation_sessions'
            ).fetchone()[0] == 0
        finally:
            observer.close()

        expired = store.get('user-a', 'conversation-a')
        assert expired.status == 'expired'
        assert expired.generation == 2
        assert expired.revision == 1
        assert expired.expires_at == session.expires_at
        assert store.list_trusted_tool_results(
            'user-a', 'conversation-a'
        ) == []
    finally:
        blocker.rollback()
        blocker.close()
        store.close()


def test_trusted_append_sanitizes_raw_clock_failure() -> None:
    """A clock exception cannot leak text through cause or context."""
    secret = 'CLOCK_SECRET_SHOULD_NOT_ESCAPE'

    class FailingClock:
        def __init__(self) -> None:
            self.failed = False

        def __call__(self) -> float:
            if self.failed:
                raise RuntimeError(secret)
            return 1000.0

    clock = FailingClock()
    store = SQLiteConversationStore(':memory:', clock=clock)
    try:
        session = store.create('user-a', 'conversation-a')
        clock.failed = True
        with pytest.raises(ConversationStateError) as caught:
            store.append_trusted_tool_result(
                _trusted_envelope(session)
            )
        assert secret not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        clock.failed = False
        assert store.get('user-a', 'conversation-a').revision == 0
        assert store.list_trusted_tool_results(
            'user-a', 'conversation-a'
        ) == []
    finally:
        store.close()


def test_reset_hides_trusted_result_but_exact_replay_remains() -> None:
    """Generation reset hides normal reads without losing the receipt."""
    store = SQLiteConversationStore(':memory:')
    try:
        session = store.create('user-a', 'conversation-a')
        envelope = _trusted_envelope(session)
        original = store.append_trusted_tool_result(envelope)
        reset = store.reset('user-a', 'conversation-a')
        assert reset.generation == 2
        assert store.list_trusted_tool_results(
            'user-a', 'conversation-a'
        ) == []
        assert store.snapshot(
            'user-a', 'conversation-a'
        ).trusted_tool_results == ()
        with pytest.raises(ConversationNotFoundError):
            store.get_trusted_tool_result(
                'user-a', 'conversation-a', envelope.feedback_id
            )
        replay = store.append_trusted_tool_result(envelope)
        assert replay.cached is True
        assert replay.commit_id == original.commit_id
        assert store.get('user-a', 'conversation-a').revision == (
            reset.revision
        )
    finally:
        store.close()


@pytest.mark.parametrize('terminal_state', ['closed', 'expired'])
def test_terminal_session_allows_only_exact_trusted_result_replay(
    terminal_state: str,
) -> None:
    """A terminal lifecycle cannot turn a receipt replay into activity."""
    clock = FakeClock()
    store = SQLiteConversationStore(
        ':memory:',
        ttl_seconds=60,
        clock=clock,
    )
    try:
        session = store.create('user-a', 'conversation-a')
        envelope = _trusted_envelope(session)
        original = store.append_trusted_tool_result(envelope)
        expires_at = store.get(
            'user-a', 'conversation-a'
        ).expires_at
        if terminal_state == 'closed':
            terminal = store.close_session(
                'user-a', 'conversation-a'
            )
        else:
            clock.advance(60)
            terminal = store.get('user-a', 'conversation-a')
        revision_before = terminal.revision
        replay = store.append_trusted_tool_result(envelope)
        assert replay.cached is True
        assert replay.commit_id == original.commit_id
        after = store.get('user-a', 'conversation-a')
        assert after.revision == revision_before
        assert after.expires_at == expires_at
        fresh = replace(
            envelope,
            feedback_id='fresh-feedback',
            request_id='fresh-request',
            tool_call_id='fresh-tool',
            generation=after.generation,
        )
        with pytest.raises(ConversationStateError):
            store.append_trusted_tool_result(fresh)
    finally:
        store.close()


@pytest.mark.parametrize('lifecycle', ['reset', 'close_session', 'delete'])
def test_append_and_lifecycle_race_has_one_serial_order(
    tmp_path,
    lifecycle: str,
) -> None:
    """Append and lifecycle transactions produce no torn destination."""
    database = tmp_path / f'{lifecycle}.sqlite3'
    setup = SQLiteConversationStore(str(database))
    session = setup.create('user-a', 'conversation-a')
    setup.close()
    envelope = _trusted_envelope(session)
    append_store = SQLiteConversationStore(str(database))
    lifecycle_store = SQLiteConversationStore(str(database))
    barrier = threading.Barrier(3)
    outcomes = []
    output_lock = threading.Lock()

    def append() -> None:
        try:
            barrier.wait()
            result = append_store.append_trusted_tool_result(envelope)
            outcome = ('append', result.cached)
        except (ConversationNotFoundError, ConversationStateError,
                ConversationChangedError):
            outcome = ('append_rejected', False)
        with output_lock:
            outcomes.append(outcome)

    def mutate() -> None:
        barrier.wait()
        getattr(lifecycle_store, lifecycle)(
            'user-a', 'conversation-a'
        )
        outcome = ('lifecycle', True)
        with output_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=append),
        threading.Thread(target=mutate),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)
    append_store.close()
    lifecycle_store.close()
    assert all(not thread.is_alive() for thread in threads)
    assert ('lifecycle', True) in outcomes
    assert outcomes[0][0] in {'append', 'append_rejected', 'lifecycle'}

    connection = sqlite3.connect(str(database))
    try:
        session_count = connection.execute(
            'SELECT COUNT(*) FROM conversation_sessions'
        ).fetchone()[0]
        result_count = connection.execute(
            'SELECT COUNT(*) FROM conversation_trusted_tool_results'
        ).fetchone()[0]
        if lifecycle == 'delete':
            assert (session_count, result_count) == (0, 0)
        else:
            assert session_count == 1
            assert result_count in {0, 1}
    finally:
        connection.close()


def test_trusted_result_schema_is_content_free_and_strict(tmp_path) -> None:
    """The persisted result has only allowlisted content-free fields."""
    database = tmp_path / 'conversation.sqlite3'
    store = SQLiteConversationStore(str(database))
    try:
        session = store.create('user-a', 'conversation-a')
        envelope = _trusted_envelope(session)
        commit = store.append_trusted_tool_result(envelope)
        with pytest.raises(FrozenInstanceError):
            commit.envelope.result.viewer_live = True
        for forged in (
            {'viewer_live': True},
            {'physical_effects': True},
            {'runtime_mode': 'production'},
            {'durability': 'process_local'},
            {'lease_scope': 'store_connection'},
            {'status': 'running'},
            {'code': 'raw_provider_error'},
            {'source': 'robot'},
        ):
            with pytest.raises(ValidationError):
                replace(_trusted_result(), **forged)
    finally:
        store.close()

    database_bytes = database.read_bytes()
    for forbidden in (
        b'room_label',
        b'map_id',
        b'device_id',
        b'plan_digest',
        b'transcript',
        b'viewer_url',
        b'raw_error',
        '거실'.encode('utf-8'),
    ):
        assert forbidden not in database_bytes


def test_store_revalidates_a_mutated_frozen_result_snapshot() -> None:
    """Low-level object mutation cannot bypass the append-time boundary."""
    store = SQLiteConversationStore(':memory:')
    try:
        session = store.create('user-a', 'conversation-a')
        envelope = _trusted_envelope(session)
        object.__setattr__(envelope.result, 'viewer_live', True)
        with pytest.raises(ValidationError, match='simulation marker'):
            store.append_trusted_tool_result(envelope)
        assert store.get('user-a', 'conversation-a').revision == 0
        assert store.list_trusted_tool_results(
            'user-a', 'conversation-a'
        ) == []
    finally:
        store.close()


@pytest.mark.parametrize(
    ('status', 'code', 'source'),
    [
        ('failed', 'preflight_failed', 'recovery'),
        ('timed_out', 'authorization_expired', 'controller'),
        ('timed_out', 'authorization_expired', 'recovery'),
        ('failed', 'event_capacity_reached', 'controller'),
    ],
)
def test_trusted_result_accepts_all_bounded_ledger_terminal_sources(
    status: str,
    code: str,
    source: str,
) -> None:
    """Valid controller, adapter, and recovery terminal paths remain usable."""
    result = _trusted_result(
        status=status,
        code=code,
        source=source,
    )
    assert result.status == status
    assert result.code == code
    assert result.source == source


def test_trusted_result_database_errors_are_fully_sanitized(
    tmp_path,
) -> None:
    """Underlying SQLite messages are absent from cause and context."""
    secret = 'DATABASE_SECRET_SHOULD_NOT_ESCAPE'
    database = tmp_path / 'conversation.sqlite3'
    store = SQLiteConversationStore(str(database))
    try:
        session = store.create('user-a', 'conversation-a')
        store._connection.execute(  # noqa: SLF001
            f'''
            CREATE TRIGGER inject_secret_failure
            BEFORE INSERT ON conversation_trusted_tool_results
            BEGIN
                SELECT RAISE(ABORT, '{secret}');
            END
            '''
        )
        store._connection.commit()  # noqa: SLF001
        with pytest.raises(ConversationConflictError) as caught:
            store.append_trusted_tool_result(
                _trusted_envelope(session)
            )
        assert secret not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert store.get('user-a', 'conversation-a').revision == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    ('column', 'value'),
    [
        ('request_fingerprint', '0' * 64),
        ('commit_id', 'forged-commit-id'),
        ('conversation_revision_after', 2),
    ],
)
def test_reopen_rejects_tampered_trusted_result_binding(
    tmp_path,
    column: str,
    value,
) -> None:
    """A changed canonical binding cannot be replayed after restart."""
    database = tmp_path / 'conversation.sqlite3'
    store = SQLiteConversationStore(str(database))
    session = store.create('user-a', 'conversation-a')
    store.append_trusted_tool_result(_trusted_envelope(session))
    store.close()

    connection = sqlite3.connect(str(database))
    assert column in {
        'request_fingerprint',
        'commit_id',
        'conversation_revision_after',
    }
    connection.execute(
        f'''
        UPDATE conversation_trusted_tool_results
        SET {column} = ?
        ''',
        (value,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match='binding'):
        SQLiteConversationStore(str(database))
