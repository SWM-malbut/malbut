"""Focused persistence tests for non-authorizing text confirmations."""

import hashlib
import sqlite3
import threading

import pytest

from malbut_agent_server.conversation import (
    ConfirmationIntentAlreadyTerminalError,
    ConfirmationIntentConflictError,
    ConfirmationIntentNotFoundError,
    ConversationChangedError,
    ConversationStateError,
    SQLiteConversationStore,
)
from malbut_agent_server.text_confirmation import ConfirmationDraft


class MutableClock:
    """Deterministic server wall clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _reserve(
    store: SQLiteConversationStore,
    clock: MutableClock,
    *,
    suffix: str = '1',
    user_id: str = 'user-a',
    conversation_id: str = 'conversation-a',
    expires_in: float = 60.0,
):
    session = store.create(user_id, conversation_id)
    begin = store.begin_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        turn_id=f'turn-{suffix}',
        request_id=f'request-{suffix}',
        request_fingerprint=_digest(f'agent-request-{suffix}'),
        user_content='거실로 이동해줘',
    )
    token = begin.token
    assert token is not None
    arguments = {'location': '거실'}
    target_digest = _digest(f'target-binding-{suffix}')
    draft = ConfirmationDraft.create(
        confirmation_request_id=f'confirmation-{suffix}',
        user_id=user_id,
        conversation_id=conversation_id,
        session_instance_id=session.session_instance_id,
        generation=token.generation,
        revision=token.revision + 1,
        ordinal=token.ordinal,
        turn_id=token.turn_id,
        request_id=token.request_id,
        decision_id=f'decision-{suffix}',
        tool_name='navigate',
        arguments=arguments,
        message='거실로 이동할까요? 네, 아니요, 또는 취소라고 답해 주세요.',
        target_room_name='거실',
        target_room_category='living_room',
        target_binding_digest=target_digest,
        state_evidence_id=f'state-evidence-{suffix}',
        state_observed_at=clock.now,
        safety_policy_revision='malbut-safety-v1',
        issued_at=clock.now,
        expires_at=clock.now + expires_in,
    )
    response = {
        'schema_version': 3,
        'safety_binding': {
            'state_evidence_id': draft.state_evidence_id,
            'state_observed_at': draft.state_observed_at,
            'safety_policy_revision': draft.safety_policy_revision,
        },
        'public': {
            'request_id': token.request_id,
            'conversation': {
                'conversation_id': token.conversation_id,
                'turn_id': token.turn_id,
                'generation': token.generation,
                'revision': token.revision + 1,
                'ordinal': token.ordinal,
            },
            'decision': {
                'type': 'tool_call',
                'message': '거실로 이동할게요.',
                'tool_name': 'navigate',
                'arguments': arguments,
            },
            'safety': {'allowed': True},
            'execution': {
                'decision_id': draft.decision_id,
                'issued_at': draft.issued_at,
                'expires_at': draft.issued_at + 5.0,
                'proposal_authorized': True,
                'state_trusted': True,
                'authorized': False,
                'consume_once': False,
                'tool_call_id': None,
            },
        },
    }
    return token, draft, response, target_digest


def _commit(
    store: SQLiteConversationStore,
    clock: MutableClock,
    **values,
):
    token, draft, response, target_digest = _reserve(
        store,
        clock,
        **values,
    )
    store.complete_turn(
        token,
        '거실로 이동할까요?',
        response,
        confirmation_draft=draft,
    )
    return draft, target_digest


def test_confirmation_is_atomic_and_db_cannot_gain_authority(
    tmp_path,
) -> None:
    """Draft failure rolls back the turn; DB checks forbid authority."""
    clock = MutableClock()
    path = tmp_path / 'atomic.sqlite3'
    store = SQLiteConversationStore(str(path), clock=clock)
    try:
        token, draft, response, _target = _reserve(store, clock)
        mismatched = ConfirmationDraft(
            confirmation_request_id=draft.confirmation_request_id,
            user_id=draft.user_id,
            conversation_id=draft.conversation_id,
            session_instance_id=draft.session_instance_id,
            generation=draft.generation,
            revision=draft.revision,
            ordinal=draft.ordinal,
            turn_id=draft.turn_id,
            request_id='different-agent-request',
            decision_id=draft.decision_id,
            tool_name=draft.tool_name,
            arguments=draft.arguments_dict(),
            message=draft.message,
            target_room_name=draft.target_room_name,
            target_room_category=draft.target_room_category,
            target_binding_digest=draft.target_binding_digest,
            state_evidence_id=draft.state_evidence_id,
            state_observed_at=draft.state_observed_at,
            safety_policy_revision=draft.safety_policy_revision,
            issued_at=draft.issued_at,
            expires_at=draft.expires_at,
        )
        with pytest.raises(ConversationChangedError):
            store.complete_turn(
                token,
                '거실로 이동할까요?',
                response,
                confirmation_draft=mismatched,
            )
        forged_provenance = ConfirmationDraft(
            confirmation_request_id=draft.confirmation_request_id,
            user_id=draft.user_id,
            conversation_id=draft.conversation_id,
            session_instance_id=draft.session_instance_id,
            generation=draft.generation,
            revision=draft.revision,
            ordinal=draft.ordinal,
            turn_id=draft.turn_id,
            request_id=draft.request_id,
            decision_id=draft.decision_id,
            tool_name=draft.tool_name,
            arguments=draft.arguments_dict(),
            message=draft.message,
            target_room_name=draft.target_room_name,
            target_room_category=draft.target_room_category,
            target_binding_digest=draft.target_binding_digest,
            state_evidence_id='forged-state-evidence',
            state_observed_at=draft.state_observed_at,
            safety_policy_revision=draft.safety_policy_revision,
            issued_at=draft.issued_at,
            expires_at=draft.expires_at,
        )
        with pytest.raises(
            ConfirmationIntentConflictError,
            match='does not match source response',
        ):
            store.complete_turn(
                token,
                '거실로 이동할까요?',
                response,
                confirmation_draft=forged_provenance,
            )
        row = store._connection.execute(
            'SELECT status FROM conversation_turns'
        ).fetchone()
        assert row['status'] == 'pending'
        session = store.get('user-a', 'conversation-a')
        assert session.revision == 0
        assert store._connection.execute(
            'SELECT COUNT(*) FROM confirmation_intents'
        ).fetchone()[0] == 0
        store.fail_turn(token)

        _commit(store, clock, suffix='2')
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                '''
                UPDATE confirmation_intents
                SET execution_authorized = 1
                '''
            )
        store._connection.rollback()
    finally:
        store.close()


def test_restart_exact_response_replay_and_payload_conflict(
    tmp_path,
) -> None:
    """Pending and terminal records survive restart with exact replay."""
    clock = MutableClock()
    path = tmp_path / 'restart.sqlite3'
    first = SQLiteConversationStore(str(path), clock=clock)
    draft, target = _commit(first, clock)
    first.close()

    second = SQLiteConversationStore(str(path), clock=clock)
    try:
        pending = second.pending_confirmation(
            draft.user_id,
            draft.conversation_id,
        )
        assert pending is not None
        assert pending.proposal_fingerprint == draft.proposal_fingerprint
        fingerprint = _digest('verified-yes-response')
        approved = second.resolve_confirmation(
            draft.user_id,
            draft.conversation_id,
            response_id='confirmation-response-1',
            response_fingerprint=fingerprint,
            disposition='approve',
            now=clock.now,
            current_target_binding_digest=target,
        )
        assert approved.disposition == 'approved'
        assert approved.execution_authorized is False
        assert approved.consume_once is False
        assert approved.to_public_dict()['execution'][
            'nav2_start_count'
        ] == 0
        second.close()
        second = SQLiteConversationStore(str(path), clock=clock)
        assert second.confirmation_for_response(
            draft.user_id,
            'confirmation-response-1',
        ) == approved
        assert second.confirmation_for_response(
            'different-user',
            'confirmation-response-1',
        ) is None
        assert second.resolve_confirmation(
            draft.user_id,
            draft.conversation_id,
            response_id='confirmation-response-1',
            response_fingerprint=fingerprint,
            disposition='approve',
            now=clock.now,
            current_target_binding_digest=target,
        ) == approved
        with pytest.raises(ConfirmationIntentConflictError):
            second.resolve_confirmation(
                draft.user_id,
                draft.conversation_id,
                response_id='confirmation-response-1',
                response_fingerprint=_digest('mutated-payload'),
                disposition='approve',
                now=clock.now,
                current_target_binding_digest=target,
            )
    finally:
        second.close()


def test_wall_clock_rollback_cannot_approve_confirmation(tmp_path) -> None:
    """A response timestamp before proposal issue cannot mint an action."""
    clock = MutableClock()
    store = SQLiteConversationStore(
        str(tmp_path / 'clock-rollback.sqlite3'),
        clock=clock,
    )
    try:
        draft, target = _commit(store, clock)
        with pytest.raises(
            ConfirmationIntentConflictError,
            match='predates',
        ):
            store.resolve_confirmation(
                draft.user_id,
                draft.conversation_id,
                response_id='rolled-back-response',
                response_fingerprint=_digest('rolled-back-yes'),
                disposition='approve',
                now=draft.issued_at - 1.0,
                current_target_binding_digest=target,
            )
        pending = store.pending_confirmation(
            draft.user_id,
            draft.conversation_id,
        )
        assert pending is not None
        assert pending.disposition == 'pending'
    finally:
        store.close()


def test_ambiguous_text_claim_survives_restart_with_zero_authority(
    tmp_path,
) -> None:
    clock = MutableClock()
    path = tmp_path / 'claim-restart.sqlite3'
    first = SQLiteConversationStore(str(path), clock=clock)
    draft, _target = _commit(first, clock)
    fingerprint = _digest('full-text-turn-envelope')
    claim, record = first.claim_text_turn_response(
        draft.user_id,
        draft.conversation_id,
        request_id='ambiguous-response-1',
        turn_id='response-turn-1',
        request_fingerprint=fingerprint,
        outcome='confirmation_unrecognized',
        confirmation_request_id=draft.confirmation_request_id,
        now=clock.now,
    )
    assert record is not None
    assert record.disposition == 'pending'
    first.close()

    second = SQLiteConversationStore(str(path), clock=clock)
    try:
        replay = second.text_turn_request_claim(
            draft.user_id,
            claim.request_id,
            fingerprint,
        )
        assert replay is not None
        replay_claim, replay_record = replay
        assert replay_claim == claim
        assert replay_record == record
        row = second._connection.execute(
            '''
            SELECT authority_kind, execution_authorized, consume_once,
                   tool_call_id, mission_id
            FROM text_turn_request_claims
            WHERE user_id = ? AND request_id = ?
            ''',
            (draft.user_id, claim.request_id),
        ).fetchone()
        assert tuple(row) == ('none', 0, 0, None, None)
        with pytest.raises(ConfirmationIntentConflictError):
            second.text_turn_request_claim(
                draft.user_id,
                claim.request_id,
                _digest('mutated-envelope'),
            )
    finally:
        second.close()


def test_concurrent_approve_and_deny_have_one_terminal_winner(
    tmp_path,
) -> None:
    """BEGIN IMMEDIATE and row CAS admit only one terminal response."""
    clock = MutableClock()
    path = tmp_path / 'concurrent.sqlite3'
    approve_store = SQLiteConversationStore(str(path), clock=clock)
    draft, target = _commit(approve_store, clock)
    deny_store = SQLiteConversationStore(str(path), clock=clock)
    barrier = threading.Barrier(2)
    records = []
    errors = []

    def resolve(store, disposition: str) -> None:
        try:
            barrier.wait()
            records.append(store.resolve_confirmation(
                draft.user_id,
                draft.conversation_id,
                response_id=f'response-{disposition}',
                response_fingerprint=_digest(disposition),
                disposition=disposition,
                now=clock.now,
                current_target_binding_digest=target,
            ))
        except Exception as error:  # captured for exact assertion below
            errors.append(error)

    threads = [
        threading.Thread(target=resolve, args=(approve_store, 'approve')),
        threading.Thread(target=resolve, args=(deny_store, 'deny')),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert len(records) == 1
        assert records[0].disposition in {'approved', 'denied'}
        assert len(errors) == 1
        assert isinstance(
            errors[0],
            ConfirmationIntentAlreadyTerminalError,
        )
    finally:
        approve_store.close()
        deny_store.close()


def test_concurrent_ambiguous_claims_have_one_payload_winner(
    tmp_path,
) -> None:
    clock = MutableClock()
    path = tmp_path / 'concurrent-claim.sqlite3'
    first = SQLiteConversationStore(str(path), clock=clock)
    draft, _target = _commit(first, clock)
    second = SQLiteConversationStore(str(path), clock=clock)
    barrier = threading.Barrier(2)
    claims = []
    errors = []

    def claim(store, fingerprint: str) -> None:
        try:
            barrier.wait()
            claims.append(store.claim_text_turn_response(
                draft.user_id,
                draft.conversation_id,
                request_id='simultaneous-response',
                turn_id='simultaneous-turn',
                request_fingerprint=fingerprint,
                outcome='confirmation_unrecognized',
                confirmation_request_id=draft.confirmation_request_id,
                now=clock.now,
            ))
        except Exception as error:
            errors.append(error)

    threads = [
        threading.Thread(target=claim, args=(first, _digest('first'))),
        threading.Thread(target=claim, args=(second, _digest('second'))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert len(claims) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ConfirmationIntentConflictError)
        assert first.pending_confirmation(
            draft.user_id,
            draft.conversation_id,
        ) is not None
    finally:
        first.close()
        second.close()


def test_lifecycle_expiry_and_target_invalidation_fail_closed(
    tmp_path,
) -> None:
    """Target change, reset, close, expiry, and delete cannot approve."""
    clock = MutableClock()
    store = SQLiteConversationStore(
        str(tmp_path / 'lifecycle.sqlite3'),
        clock=clock,
    )
    try:
        draft, target = _commit(store, clock, suffix='target')
        with pytest.raises(ConfirmationIntentConflictError):
            store.invalidate_confirmation(
                draft.user_id,
                draft.conversation_id,
                result_code='confirmation_target_changed',
                now=clock.now,
                expected_target_binding_digest=_digest('wrong-target'),
            )
        invalidated = store.invalidate_confirmation(
            draft.user_id,
            draft.conversation_id,
            result_code='confirmation_target_changed',
            now=clock.now,
            expected_target_binding_digest=target,
        )
        assert invalidated.disposition == 'invalidated'
        assert invalidated.execution_authorized is False
        with pytest.raises(ConfirmationIntentAlreadyTerminalError):
            store.invalidate_confirmation(
                draft.user_id,
                draft.conversation_id,
                result_code='confirmation_target_changed',
                now=clock.now,
                expected_target_binding_digest=target,
            )

        reset_draft, reset_target = _commit(
            store,
            clock,
            suffix='reset',
            conversation_id='conversation-reset',
        )
        store.reset(reset_draft.user_id, reset_draft.conversation_id)
        assert store.confirmation_for_request(
            reset_draft.user_id,
            reset_draft.request_id,
        ).disposition == 'invalidated'
        with pytest.raises(ConfirmationIntentNotFoundError):
            store.resolve_confirmation(
                reset_draft.user_id,
                reset_draft.conversation_id,
                response_id='response-after-reset',
                response_fingerprint=_digest('after-reset'),
                disposition='approve',
                now=clock.now,
                current_target_binding_digest=reset_target,
            )

        close_draft, _ = _commit(
            store,
            clock,
            suffix='close',
            conversation_id='conversation-close',
        )
        store.close_session(
            close_draft.user_id,
            close_draft.conversation_id,
        )
        with pytest.raises(ConversationStateError):
            store.pending_confirmation(
                close_draft.user_id,
                close_draft.conversation_id,
            )

        expiring, _ = _commit(
            store,
            clock,
            suffix='expiry',
            conversation_id='conversation-expiry',
            expires_in=10.0,
        )
        clock.now += 11.0
        assert store.pending_confirmation(
            expiring.user_id,
            expiring.conversation_id,
        ) is None
        assert store.confirmation_for_request(
            expiring.user_id,
            expiring.request_id,
        ).disposition == 'expired'

        deleted, _ = _commit(
            store,
            clock,
            suffix='delete',
            conversation_id='conversation-delete',
        )
        assert store.delete(
            deleted.user_id,
            deleted.conversation_id,
        ) is True
        with pytest.raises(ConfirmationIntentNotFoundError):
            store.confirmation_for_request(
                deleted.user_id,
                deleted.request_id,
            )
    finally:
        store.close()
