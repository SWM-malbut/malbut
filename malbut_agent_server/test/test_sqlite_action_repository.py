"""Focused durability tests for the SWM25-132 action ledger."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading

import pytest

from malbut_agent_server.adapters.outbound.sqlite_action_repository import (
    ActionClaimLostError,
    ActionConflictError,
    ActionPersistenceError,
    SQLiteActionRepository,
    insert_action_for_approved_confirmation,
)
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.domain.robot_action import (
    ActionState,
    DispatchAuthorization,
)
from malbut_agent_server.text_confirmation import (
    ConfirmationDraft,
    ConfirmationResolution,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _pending_confirmation(path, suffix: str = '1'):
    now = 100.0
    user_id = f'user-{suffix}'
    conversation_id = f'conversation-{suffix}'
    store = SQLiteConversationStore(str(path), clock=lambda: now)
    session = store.create(user_id, conversation_id)
    begin = store.begin_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        turn_id=f'turn-{suffix}',
        request_id=f'request-{suffix}',
        request_fingerprint=_digest(f'request-{suffix}'),
        user_content='거실로 이동해줘',
    )
    token = begin.token
    assert token is not None
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
        arguments={'location': '거실'},
        message='거실로 이동할까요? 네, 아니요, 또는 취소라고 답해 주세요.',
        target_room_name='거실',
        target_room_category='living_room',
        target_binding_digest=_digest(f'target-{suffix}'),
        state_evidence_id=f'confirmation-state-{suffix}',
        state_observed_at=now,
        safety_policy_revision='malbut-safety-v1',
        issued_at=now,
        expires_at=now + 30.0,
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
                'arguments': {'location': '거실'},
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
    store.complete_turn(
        token,
        draft.message,
        response,
        confirmation_draft=draft,
    )
    pending = store.confirmation_for_request(
        user_id,
        draft.request_id,
    )
    store.close()
    return pending


def _terminal_record(pending, resolved_at: float = 101.0):
    resolution = ConfirmationResolution.from_verified_response(
        pending,
        caller_user_id=pending.user_id,
        caller_conversation_id=pending.conversation_id,
        caller_session_instance_id=pending.session_instance_id,
        caller_generation=pending.generation,
        response_id=f'response-{pending.confirmation_request_id}',
        response_turn_id=f'response-turn-{pending.confirmation_request_id}',
        response_fingerprint=_digest(
            f'response-{pending.confirmation_request_id}'
        ),
        requested_disposition='approve',
    )
    return pending.resolve(resolution, resolved_at=resolved_at)


def _write_terminal(connection, terminal) -> None:
    connection.execute(
        '''
        UPDATE confirmation_intents
        SET state = 'resolved', disposition = 'approved',
            requested_disposition = 'approve',
            result_code = 'confirmation_approved', response_id = ?,
            response_turn_id = ?, response_fingerprint = ?,
            resolved_at = ?, record_json = ?, updated_at = ?
        WHERE confirmation_request_id = ? AND state = 'pending'
        ''',
        (
            terminal.response_id,
            terminal.response_turn_id,
            terminal.response_fingerprint,
            terminal.resolved_at,
            json.dumps(
                terminal.to_private_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            ),
            terminal.resolved_at,
            terminal.confirmation_request_id,
        ),
    )


def _create_action(path, suffix: str = '1'):
    pending = _pending_confirmation(path, suffix)
    terminal = _terminal_record(pending)
    initializer = SQLiteActionRepository(str(path))
    initializer.close()
    connection = sqlite3.connect(str(path))
    connection.execute('BEGIN IMMEDIATE')
    _write_terminal(connection, terminal)
    action = insert_action_for_approved_confirmation(
        connection,
        terminal,
        now=terminal.resolved_at,
    )
    connection.commit()
    connection.close()
    return terminal, action


def _authorization(action, now: float) -> DispatchAuthorization:
    return DispatchAuthorization(
        state_evidence_id=f'fresh-{action.action_id}',
        state_observed_at=now,
        safety_policy_revision=(
            action.binding.confirmation_safety_policy_revision
        ),
        target_binding_digest=action.binding.target_binding_digest,
        authorized_at=now,
    )


def test_approval_and_action_rollback_together_on_insert_failure(
    tmp_path,
) -> None:
    """A failed action INSERT cannot leave a committed approval."""
    path = tmp_path / 'atomic.sqlite3'
    pending = _pending_confirmation(path)
    terminal = _terminal_record(pending)
    initializer = SQLiteActionRepository(str(path))
    initializer.close()
    setup = sqlite3.connect(str(path))
    setup.execute(
        '''
        CREATE TRIGGER fail_action_insert
        BEFORE INSERT ON robot_actions
        BEGIN
            SELECT RAISE(ABORT, 'forced action insert failure');
        END
        '''
    )
    setup.commit()
    setup.close()
    connection = sqlite3.connect(str(path))
    connection.execute('BEGIN IMMEDIATE')
    _write_terminal(connection, terminal)
    with pytest.raises(ActionConflictError):
        insert_action_for_approved_confirmation(
            connection,
            terminal,
            now=terminal.resolved_at,
        )
    connection.rollback()
    state = connection.execute(
        'SELECT state, disposition FROM confirmation_intents '
        'WHERE confirmation_request_id = ?',
        (pending.confirmation_request_id,),
    ).fetchone()
    assert state == ('pending', 'pending')
    assert connection.execute(
        'SELECT COUNT(*) FROM robot_actions'
    ).fetchone()[0] == 0
    connection.close()


def test_duplicate_approval_reuses_one_exact_server_action(tmp_path) -> None:
    """Replay returns one action and detects copied-binding tampering."""
    path = tmp_path / 'duplicate.sqlite3'
    terminal, action = _create_action(path)
    connection = sqlite3.connect(str(path))
    connection.execute('BEGIN IMMEDIATE')
    replay = insert_action_for_approved_confirmation(
        connection,
        terminal,
        now=102.0,
    )
    assert replay.action_id == action.action_id
    connection.commit()
    connection.execute(
        'UPDATE robot_actions SET '
        'confirmation_safety_policy_revision = ?',
        ('tampered-policy',),
    )
    connection.commit()
    connection.execute('BEGIN IMMEDIATE')
    with pytest.raises(ActionConflictError):
        insert_action_for_approved_confirmation(
            connection,
            terminal,
            now=103.0,
        )
    connection.rollback()
    connection.close()


def test_two_workers_have_one_winner_and_expired_claim_is_fenced(
    tmp_path,
) -> None:
    """Only one worker wins; an expired claimant cannot later dispatch."""
    path = tmp_path / 'workers.sqlite3'
    _terminal, action = _create_action(path)
    first = SQLiteActionRepository(str(path))
    second = SQLiteActionRepository(str(path))
    barrier = threading.Barrier(2)
    claims = []

    def claim(repository, worker_id):
        barrier.wait()
        claims.append(repository.claim_next(
            worker_id,
            now=102.0,
            lease_for=2.0,
        ))

    threads = [
        threading.Thread(target=claim, args=(first, 'worker-1')),
        threading.Thread(target=claim, args=(second, 'worker-2')),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    stale = winners[0]
    replacement_repo = second if stale.worker_id == 'worker-1' else first
    replacement = replacement_repo.claim_next(
        'replacement',
        now=105.0,
        lease_for=180.0,
    )
    assert replacement is not None
    assert replacement.fence == stale.fence + 1
    with pytest.raises(ActionClaimLostError):
        first.record_dispatch_intent(
            stale,
            _authorization(action, 105.0),
            now=105.0,
        )
    first.close()
    second.close()


def test_intent_is_atomic_and_persists_fresh_authorization(tmp_path) -> None:
    """Action state and outbox intent commit or roll back together."""
    path = tmp_path / 'intent.sqlite3'
    _terminal, action = _create_action(path)
    repository = SQLiteActionRepository(str(path))
    claim = repository.claim_next('worker-1', now=102.0, lease_for=180.0)
    assert claim is not None
    connection = sqlite3.connect(str(path))
    connection.execute(
        '''
        CREATE TRIGGER fail_dispatch_update
        BEFORE UPDATE OF state ON robot_actions
        WHEN NEW.state = 'DISPATCH_INTENT'
        BEGIN
            SELECT RAISE(ABORT, 'forced dispatch failure');
        END
        '''
    )
    connection.commit()
    connection.close()
    with pytest.raises(sqlite3.IntegrityError):
        repository.record_dispatch_intent(
            claim,
            _authorization(action, 103.0),
            now=103.0,
        )
    assert repository.get(action.action_id).state is ActionState.CLAIMED
    check = sqlite3.connect(str(path))
    assert check.execute(
        'SELECT COUNT(*) FROM execution_outbox'
    ).fetchone()[0] == 0
    check.execute('DROP TRIGGER fail_dispatch_update')
    check.commit()
    check.close()
    intent = repository.record_dispatch_intent(
        claim,
        _authorization(action, 104.0),
        now=104.0,
    )
    assert intent.action.dispatch_authorization.state_evidence_id.startswith(
        'fresh-action-'
    )
    repository.close()


def test_dispatch_expiry_does_not_shorten_execution_lease(tmp_path) -> None:
    """A timely send can finish after its send window under the full lease."""
    path = tmp_path / 'long-navigation.sqlite3'
    _terminal, action = _create_action(path)
    repository = SQLiteActionRepository(str(path))
    claim = repository.claim_next('worker-1', now=102.0, lease_for=180.0)
    assert claim is not None
    assert claim.lease_expires_at == 282.0
    intent = repository.record_dispatch_intent(
        claim,
        _authorization(action, 120.0),
        now=120.0,
    )
    started = repository.mark_started(intent, now=125.0)
    result = repository.finish(
        started,
        ActionState.SUCCEEDED,
        result_code='arrived',
        now=140.0,
    )
    assert action.dispatch_expires_at == 131.0
    assert result.state is ActionState.SUCCEEDED
    repository.close()


def test_recovery_waits_for_lease_then_seals_unknown_without_resend(
    tmp_path,
) -> None:
    """Competing recovery cannot invalidate an active fenced sender."""
    path = tmp_path / 'recovery.sqlite3'
    _terminal, action = _create_action(path)
    owner = SQLiteActionRepository(str(path))
    recovery = SQLiteActionRepository(str(path))
    claim = owner.claim_next('worker-1', now=102.0, lease_for=180.0)
    intent = owner.record_dispatch_intent(
        claim,
        _authorization(action, 103.0),
        now=103.0,
    )
    assert recovery.recover_uncertain_after_restart(now=104.0) == 0
    assert recovery.get(action.action_id).state is ActionState.DISPATCH_INTENT
    assert recovery.recover_uncertain_after_restart(now=283.0) == 1
    recovered = recovery.get(action.action_id)
    assert recovered.state is ActionState.UNKNOWN
    assert recovery.claim_next(
        'worker-2', now=284.0, lease_for=180.0
    ) is None
    with pytest.raises(ActionClaimLostError):
        owner.mark_started(intent, now=284.0)
    owner.close()
    recovery.close()


def test_expired_or_clock_rollback_pending_action_is_blocked(tmp_path) -> None:
    """An action outside trustworthy wall time is never leased."""
    expiry_path = tmp_path / 'expiry.sqlite3'
    _terminal, expired = _create_action(expiry_path)
    expiry_repo = SQLiteActionRepository(str(expiry_path))
    assert expiry_repo.claim_next(
        'worker-1', now=expired.dispatch_expires_at, lease_for=10.0
    ) is None
    assert expiry_repo.get(expired.action_id).result_code == 'action_expired'
    expiry_repo.close()

    rollback_path = tmp_path / 'clock.sqlite3'
    _terminal, future = _create_action(rollback_path)
    rollback_repo = SQLiteActionRepository(str(rollback_path))
    assert rollback_repo.claim_next(
        'worker-1', now=99.0, lease_for=10.0
    ) is None
    blocked = rollback_repo.get(future.action_id)
    assert blocked.state is ActionState.BLOCKED
    assert blocked.result_code == 'action_clock_rollback'
    assert blocked.updated_at >= blocked.created_at
    rollback_repo.close()


def test_definite_block_is_persisted_after_dispatch_window_crosses(
    tmp_path,
) -> None:
    """A valid owner may block after expiry instead of stranding CLAIMED."""
    path = tmp_path / 'block-after-expiry.sqlite3'
    _terminal, action = _create_action(path)
    repository = SQLiteActionRepository(str(path))
    claim = repository.claim_next('worker-1', now=102.0, lease_for=180.0)
    blocked = repository.block(
        claim,
        result_code='action_dispatch_expired',
        now=action.dispatch_expires_at,
    )
    assert blocked.state is ActionState.BLOCKED
    assert blocked.result_code == 'action_dispatch_expired'
    repository.close()


def test_intent_operations_reject_diverged_outbox_authority(tmp_path) -> None:
    """Duplicated outbox authority must exactly match the action row."""
    path = tmp_path / 'outbox-tamper.sqlite3'
    _terminal, action = _create_action(path)
    repository = SQLiteActionRepository(str(path))
    claim = repository.claim_next('worker-1', now=102.0, lease_for=180.0)
    intent = repository.record_dispatch_intent(
        claim,
        _authorization(action, 103.0),
        now=103.0,
    )
    tamper = sqlite3.connect(str(path))
    tamper.execute(
        'UPDATE execution_outbox SET state_evidence_id = ?',
        ('different-evidence',),
    )
    tamper.commit()
    tamper.close()
    with pytest.raises(ActionClaimLostError):
        repository.mark_started(intent, now=104.0)
    stored = repository.get(action.action_id)
    assert stored.state is ActionState.DISPATCH_INTENT
    repository.close()


def test_recovery_rolls_back_if_outbox_is_missing_or_inconsistent(
    tmp_path,
) -> None:
    """Recovery never commits UNKNOWN into only one half of the ledger."""
    path = tmp_path / 'recovery-corruption.sqlite3'
    _terminal, action = _create_action(path)
    repository = SQLiteActionRepository(str(path))
    claim = repository.claim_next('worker-1', now=102.0, lease_for=2.0)
    repository.record_dispatch_intent(
        claim,
        _authorization(action, 103.0),
        now=103.0,
    )
    tamper = sqlite3.connect(str(path))
    tamper.execute(
        'UPDATE execution_outbox SET safety_policy_revision = ?',
        ('different-policy',),
    )
    tamper.commit()
    tamper.close()
    with pytest.raises(ActionPersistenceError):
        repository.recover_uncertain_after_restart(now=105.0)
    stored = repository.get(action.action_id)
    assert stored.state is ActionState.DISPATCH_INTENT
    repository.close()


def test_hydration_rejects_dispatch_policy_corruption(tmp_path) -> None:
    """The domain independently verifies persisted fresh authority."""
    path = tmp_path / 'action-auth-tamper.sqlite3'
    _terminal, action = _create_action(path)
    repository = SQLiteActionRepository(str(path))
    claim = repository.claim_next('worker-1', now=102.0, lease_for=180.0)
    repository.record_dispatch_intent(
        claim,
        _authorization(action, 103.0),
        now=103.0,
    )
    tamper = sqlite3.connect(str(path))
    tamper.execute(
        'UPDATE robot_actions '
        'SET dispatch_safety_policy_revision = ?',
        ('different-policy',),
    )
    tamper.commit()
    tamper.close()
    with pytest.raises(ActionPersistenceError):
        repository.get(action.action_id)
    repository.close()


def test_standalone_repository_database_is_owner_only(tmp_path) -> None:
    """Repository creation tightens permissive process umask to 0600."""
    path = tmp_path / 'permissions.sqlite3'
    previous_umask = os.umask(0)
    try:
        repository = SQLiteActionRepository(str(path))
    finally:
        os.umask(previous_umask)
    try:
        for suffix in ('', '-wal', '-shm'):
            candidate = path.parent / f'{path.name}{suffix}'
            if candidate.exists():
                assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
    finally:
        repository.close()
