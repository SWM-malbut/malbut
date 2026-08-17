"""Durable, leased TTS feedback derived from trusted tool results."""

import hashlib
import sqlite3
import threading

import pytest

import malbut_agent_server.conversation as conversation_module
import malbut_agent_server.execution_ledger as execution_ledger
import malbut_agent_server.trusted_result_tts as tts_module
from malbut_agent_server.conversation import ConversationChangedError
from malbut_agent_server.trusted_result_tts import (
    TrustedResultTTSConflictError,
    TrustedResultTTSError,
)
from test_monitor_room_simulation_execution import (
    MutableClock,
    _scenario,
    _simulation_store,
)


_SUCCESS_TEXT = (
    '요청한 방의 확인 지점 계획을 시뮬레이션으로 만들었어요. '
    '로봇 이동, 카메라 촬영, 영상 재생은 아직 하지 않았어요.'
)


def _consume(store, clock, suffix):
    scenario = _scenario(store, clock, suffix=suffix)
    receipt = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    return scenario, receipt


def _event_row(store):
    return store._connection.execute(
        'SELECT * FROM trusted_result_tts_outbox'
    ).fetchone()


def test_fresh_result_atomically_appends_content_free_pending_event(
    tmp_path,
) -> None:
    """Bind one stable request without a fake turn or physical claim."""
    database = tmp_path / 'tts-pending.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='tts-pending')
    turns_before = store.list_turns(
        scenario.draft.user_id, scenario.draft.conversation_id
    )
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    trusted = store.list_trusted_tool_results(
        scenario.draft.user_id, scenario.draft.conversation_id
    )[0]
    row = _event_row(store)
    expected_digest = hashlib.sha256(
        b'tool-result-tts-v1\0'
        + trusted.trusted_result_id.encode('utf-8')
    ).hexdigest()

    assert row['event_id'] == f'trusted-result-tts-{expected_digest}'
    assert row['state'] == 'pending'
    assert row['speech_session_id'] == scenario.draft.speech_session_id
    assert row['attempt_count'] == row['claim_fence'] == 0
    assert row['simulation'] == 1
    assert row['physical_authorized'] == 0
    assert row['physical_effects'] == 0
    assert row['execution_authorized'] == 0
    assert row['physical_audio_verified'] == 0
    assert _SUCCESS_TEXT not in str(tuple(row))
    assert store.list_turns(
        scenario.draft.user_id, scenario.draft.conversation_id
    ) == turns_before
    store.close()


def test_event_id_domain_has_fixed_known_vector() -> None:
    """Freeze the byte-exact downstream idempotency domain."""
    trusted_id = 'trusted-tool-result-' + ('a' * 40)
    assert tts_module._event_id(trusted_id) == (
        'trusted-result-tts-'
        '8d47a76c34ea6744c071acaa9aacd435b9d0cd3ed088c3a7120b9684335c089f'
    )


def test_claim_replays_commit_response_but_not_acknowledged_audio(
    tmp_path,
) -> None:
    """Return one current lease exactly, then suppress post-ACK replay."""
    database = tmp_path / 'tts-claim-replay.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-claim-replay')

    assert store.claim_trusted_result_tts(
        'different-user',
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-wrong-user',
    ) is None
    assert store.claim_trusted_result_tts(
        scenario.draft.user_id,
        'different-conversation',
        scenario.draft.speech_session_id,
        'claim-wrong-conversation',
    ) is None
    assert store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        'different-speech-session',
        'claim-wrong-speech',
    ) is None
    claim = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-exact-replay',
        lease_seconds=30,
    )
    assert claim is not None
    replay = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-exact-replay',
        lease_seconds=30,
    )
    assert replay == claim
    assert replay.claim_token == claim.claim_token
    assert claim.message == _SUCCESS_TEXT
    assert claim.tts_request_id == claim.event_id
    assert claim.to_public_dict()['physical_audio_verified'] is False
    with pytest.raises(TrustedResultTTSConflictError, match='stale'):
        store.acknowledge_trusted_result_tts(
            'different-user',
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            event_id=claim.event_id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
        )
    assert store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-contention',
    ) is None

    clock.now += 1.0
    acknowledged = store.acknowledge_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        event_id=claim.event_id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
    )
    assert acknowledged.state == 'acknowledged'
    assert acknowledged.to_public_dict()['physical_audio_verified'] is False
    assert store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-exact-replay',
        lease_seconds=30,
    ) is None
    store.close()


def test_failure_codes_share_one_private_free_template(
    tmp_path,
    monkeypatch,
) -> None:
    """Collapse planner failures to one fixed non-sensitive utterance."""
    assert tts_module._template('semantic_sample_planning_failed') == (
        tts_module._template('semantic_sample_result_invalid')
    )
    database = tmp_path / 'tts-failure-template.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='tts-failure-template')

    def fail_planner(_target):
        raise RuntimeError('PRIVATE-PLANNER-MARKER')

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        fail_planner,
    )
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    claim = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-failure-template',
    )
    assert claim is not None
    assert claim.message == (
        '요청한 방의 확인 지점 계획을 시뮬레이션으로 만들지 '
        '못했어요. 로봇 이동, 카메라 촬영, 영상 재생은 하지 '
        '않았어요.'
    )
    assert 'PRIVATE-PLANNER-MARKER' not in str(claim.to_public_dict())
    store.close()


def test_restart_ack_replay_and_expired_fence_takeover(tmp_path) -> None:
    """Persist raw claim replay while fencing an expired predecessor."""
    database = tmp_path / 'tts-restart.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-restart')
    first = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-before-restart',
        lease_seconds=5,
    )
    assert first is not None
    store.close()

    reopened = _simulation_store(str(database), clock=clock)
    assert reopened.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-before-restart',
        lease_seconds=5,
    ) == first
    clock.now = first.lease_expires_at
    assert reopened.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-before-restart',
        lease_seconds=5,
    ) is None
    with pytest.raises(TrustedResultTTSConflictError, match='lease expired'):
        reopened.acknowledge_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            event_id=first.event_id,
            claim_token=first.claim_token,
            claim_fence=first.claim_fence,
        )
    second = reopened.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-after-expiry',
        lease_seconds=5,
    )
    assert second is not None
    assert second.event_id == first.event_id
    assert second.claim_fence == first.claim_fence + 1
    assert second.claim_token != first.claim_token
    with pytest.raises(TrustedResultTTSConflictError, match='stale'):
        reopened.acknowledge_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            event_id=first.event_id,
            claim_token=first.claim_token,
            claim_fence=first.claim_fence,
        )
    clock.now += 1.0
    terminal = reopened.acknowledge_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        event_id=second.event_id,
        claim_token=second.claim_token,
        claim_fence=second.claim_fence,
    )
    reopened.close()

    clock.now += 100.0
    final = _simulation_store(str(database), clock=clock)
    assert final.acknowledge_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        event_id=second.event_id,
        claim_token=second.claim_token,
        claim_fence=second.claim_fence,
    ) == terminal
    final.close()


def test_attempt_bound_cancels_after_fifth_expired_claim(tmp_path) -> None:
    """Bound poison-event retries and preserve the stable request id."""
    database = tmp_path / 'tts-attempt-bound.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-attempt-bound')
    event_ids = set()
    for attempt in range(1, 6):
        claim = store.claim_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            f'claim-attempt-{attempt}',
            lease_seconds=1,
        )
        assert claim is not None
        assert claim.attempt_number == attempt
        event_ids.add(claim.event_id)
        clock.now = claim.lease_expires_at
    assert store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-after-exhaustion',
        lease_seconds=1,
    ) is None
    row = _event_row(store)
    assert event_ids == {row['event_id']}
    assert row['state'] == 'cancelled'
    assert row['cancellation_code'] == 'delivery_attempts_exhausted'
    assert row['attempt_count'] == 5
    store.close()


def test_reset_close_expiry_and_delete_apply_explicit_policy(tmp_path) -> None:
    """Cancel nonterminal lifecycle work and cascade only on delete."""
    database = tmp_path / 'tts-lifecycle.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock, ttl_seconds=60)
    reset_scenario, _receipt = _consume(store, clock, 'tts-reset')
    store.reset(
        reset_scenario.draft.user_id,
        reset_scenario.draft.conversation_id,
    )
    reset_row = store._connection.execute(
        '''
        SELECT * FROM trusted_result_tts_outbox
        WHERE conversation_id = ?
        ''',
        (reset_scenario.draft.conversation_id,),
    ).fetchone()
    assert reset_row['state'] == 'cancelled'
    assert reset_row['cancellation_code'] == 'conversation_reset'

    close_scenario, _receipt = _consume(store, clock, 'tts-close')
    store.close_session(
        close_scenario.draft.user_id,
        close_scenario.draft.conversation_id,
    )
    close_row = store._connection.execute(
        '''
        SELECT * FROM trusted_result_tts_outbox
        WHERE conversation_id = ?
        ''',
        (close_scenario.draft.conversation_id,),
    ).fetchone()
    assert close_row['cancellation_code'] == 'conversation_inactive'

    expire_scenario, _receipt = _consume(store, clock, 'tts-expire')
    clock.now += 60.0
    store.purge_expired()
    expire_row = store._connection.execute(
        '''
        SELECT * FROM trusted_result_tts_outbox
        WHERE conversation_id = ?
        ''',
        (expire_scenario.draft.conversation_id,),
    ).fetchone()
    assert expire_row['cancellation_code'] == 'conversation_inactive'

    delete_scenario, _receipt = _consume(store, clock, 'tts-delete')
    assert store.delete(
        delete_scenario.draft.user_id,
        delete_scenario.draft.conversation_id,
    )
    replay = store.consume_approved_monitor_room_simulation(
        approval=delete_scenario.approval,
        request=delete_scenario.request,
    )
    assert replay.replayed is True
    assert store._connection.execute(
        '''
        SELECT COUNT(*) FROM trusted_result_tts_outbox
        WHERE conversation_id = ?
        ''',
        (delete_scenario.draft.conversation_id,),
    ).fetchone()[0] == 0
    store.close()


def test_outbox_failure_rolls_back_receipt_result_and_revision(
    tmp_path,
    monkeypatch,
) -> None:
    """Keep terminal, trusted result, outbox, and revision one commit."""
    database = tmp_path / 'tts-atomic.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='tts-atomic')
    before = store.get(
        scenario.draft.user_id, scenario.draft.conversation_id
    )
    original = conversation_module.record_or_verify_trusted_result_tts_locked

    def fail_outbox(*_args, **_kwargs):
        raise RuntimeError('injected TTS outbox failure')

    monkeypatch.setattr(
        conversation_module,
        'record_or_verify_trusted_result_tts_locked',
        fail_outbox,
    )
    with pytest.raises(RuntimeError, match='injected TTS outbox failure'):
        store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
    for table in (
        'monitor_room_simulation_ledger',
        'conversation_trusted_tool_results',
        'trusted_result_tts_outbox',
    ):
        assert store._connection.execute(
            f'SELECT COUNT(*) FROM {table}'
        ).fetchone()[0] == 0
    assert store.get(
        scenario.draft.user_id, scenario.draft.conversation_id
    ).revision == before.revision
    monkeypatch.setattr(
        conversation_module,
        'record_or_verify_trusted_result_tts_locked',
        original,
    )
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert _event_row(store)['state'] == 'pending'
    store.close()


def test_upgrade_tombstones_existing_result_without_stale_speech(
    tmp_path,
    monkeypatch,
) -> None:
    """Snapshot pre-feature results as permanently unclaimable events."""
    database = tmp_path / 'tts-upgrade.sqlite3'
    clock = MutableClock()
    original_prepare = (
        conversation_module.prepare_trusted_result_tts_schema_locked
    )
    original_record = (
        conversation_module.record_or_verify_trusted_result_tts_locked
    )
    monkeypatch.setattr(
        conversation_module,
        'prepare_trusted_result_tts_schema_locked',
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        conversation_module,
        'record_or_verify_trusted_result_tts_locked',
        lambda *_args, **_kwargs: None,
    )
    old = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(old, clock, 'tts-upgrade')
    old.close()
    monkeypatch.setattr(
        conversation_module,
        'prepare_trusted_result_tts_schema_locked',
        original_prepare,
    )
    monkeypatch.setattr(
        conversation_module,
        'record_or_verify_trusted_result_tts_locked',
        original_record,
    )

    upgraded = _simulation_store(str(database), clock=clock)
    row = _event_row(upgraded)
    assert row['state'] == 'cancelled'
    assert row['cancellation_code'] == 'preactivation'
    assert upgraded.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-old-result',
    ) is None
    upgraded.close()


def test_missing_activation_anchor_fails_when_an_event_exists(
    tmp_path,
) -> None:
    """Reject sentinel removal once any feedback authority exists."""
    database = tmp_path / 'tts-anchor.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    _consume(store, clock, 'tts-anchor')
    store.close()

    connection = sqlite3.connect(str(database))
    connection.execute(
        'DROP TRIGGER monitor_room_simulation_preactivation_no_delete'
    )
    connection.execute(
        '''
        DELETE FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (tts_module.TRUSTED_RESULT_TTS_ACTIVATION_SENTINEL,),
    )
    connection.execute(
        execution_ledger.SIMULATION_PREACTIVATION_NO_DELETE_TRIGGER_SQL
    )
    connection.commit()
    connection.close()

    with pytest.raises(TrustedResultTTSError, match='anchor is missing'):
        _simulation_store(str(database), clock=clock)


def test_restored_trigger_cannot_hide_late_ack_time_tamper(
    tmp_path,
) -> None:
    """Recompute the immutable claim lease when validating terminal ACK."""
    database = tmp_path / 'tts-ack-tamper.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-ack-tamper')
    claim = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-ack-tamper',
        lease_seconds=5,
    )
    assert claim is not None
    clock.now += 1.0
    store.acknowledge_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        event_id=claim.event_id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
    )
    store._connection.execute(
        'DROP TRIGGER trusted_result_tts_transition_guard'
    )
    late = claim.lease_expires_at + 1.0
    store._connection.execute(
        '''
        UPDATE trusted_result_tts_outbox
        SET acknowledged_at = ?, last_transition_at = ?
        WHERE event_id = ?
        ''',
        (late, late, claim.event_id),
    )
    store._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL
    )
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultTTSError, match='claim|ACK'):
        _simulation_store(str(database), clock=clock)


def test_restored_trigger_cannot_forge_early_attempt_exhaustion(
    tmp_path,
) -> None:
    """Require the fifth immutable lease before exhaustion cancellation."""
    database = tmp_path / 'tts-exhaustion-tamper.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-exhaustion-tamper')
    claim = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-exhaustion-tamper',
        lease_seconds=5,
    )
    assert claim is not None
    store._connection.execute(
        'DROP TRIGGER trusted_result_tts_transition_guard'
    )
    store._connection.execute(
        '''
        UPDATE trusted_result_tts_outbox
        SET state = 'cancelled', lease_expires_at = NULL,
            cancelled_at = ?, last_transition_at = ?,
            cancellation_code = 'delivery_attempts_exhausted'
        WHERE event_id = ?
        ''',
        (
            claim.lease_expires_at,
            claim.lease_expires_at,
            claim.event_id,
        ),
    )
    store._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL
    )
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultTTSError, match='claim|exhaustion'):
        _simulation_store(str(database), clock=clock)


def test_claim_fingerprint_and_missing_event_tamper_fail_closed(
    tmp_path,
) -> None:
    """Detect restored-trigger claim drift and a directly deleted event."""
    database = tmp_path / 'tts-claim-tamper.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-claim-tamper')
    assert store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-fingerprint-tamper',
    ) is not None
    store._connection.execute(
        'DROP TRIGGER trusted_result_tts_claim_no_update'
    )
    store._connection.execute(
        '''
        UPDATE trusted_result_tts_claims
        SET claim_request_fingerprint = ?
        ''',
        ('0' * 64,),
    )
    store._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_CLAIM_NO_UPDATE_SQL
    )
    store._connection.commit()
    store.close()
    with pytest.raises(TrustedResultTTSError, match='claim is incompatible'):
        _simulation_store(str(database), clock=clock)

    missing_database = tmp_path / 'tts-missing-event.sqlite3'
    clean = _simulation_store(str(missing_database), clock=clock)
    _consume(clean, clock, 'tts-missing-event')
    clean._connection.execute('DELETE FROM trusted_result_tts_outbox')
    clean._connection.commit()
    clean.close()
    with pytest.raises(TrustedResultTTSError, match='event is missing'):
        _simulation_store(str(missing_database), clock=clock)


def test_external_anchor_binds_recomputed_metadata_digest(tmp_path) -> None:
    """Reject metadata drift even when its internal digest is recomputed."""
    database = tmp_path / 'tts-metadata-anchor.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    _consume(store, clock, 'tts-metadata-anchor')
    metadata = store._connection.execute(
        'SELECT * FROM trusted_result_tts_schema_metadata'
    ).fetchone()
    changed_at = float(metadata['activated_at']) + 1.0
    changed_epoch = 'a' * 64
    changed_digest = tts_module._canonical_hash(
        {
            'schema_version': 1,
            'activated_at': changed_at,
            'activation_epoch': changed_epoch,
            'sources': [],
        }
    )
    store._connection.execute(
        'DROP TRIGGER trusted_result_tts_metadata_no_update'
    )
    store._connection.execute(
        '''
        UPDATE trusted_result_tts_schema_metadata
        SET activated_at = ?, activation_epoch = ?,
            preactivation_digest = ?
        WHERE singleton = 1
        ''',
        (changed_at, changed_epoch, changed_digest),
    )
    store._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_METADATA_NO_UPDATE_SQL
    )
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultTTSError, match='anchor'):
        _simulation_store(str(database), clock=clock)


@pytest.mark.parametrize(
    'mutation',
    (
        'DROP TRIGGER trusted_result_tts_no_replace',
        '''
        CREATE TRIGGER trusted_result_tts_unexpected
        AFTER UPDATE ON trusted_result_tts_outbox
        BEGIN
            SELECT 1;
        END
        ''',
    ),
)
def test_missing_or_extra_schema_object_fails_closed(
    tmp_path,
    mutation,
) -> None:
    """Require the exact outbox trigger and index set on every reopen."""
    database = tmp_path / (
        'tts-schema-extra.sqlite3'
        if mutation.lstrip().startswith('CREATE')
        else 'tts-schema-missing.sqlite3'
    )
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    _consume(store, clock, database.stem)
    store._connection.execute(mutation)
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultTTSError, match='schema'):
        _simulation_store(str(database), clock=clock)


def test_raw_update_replace_and_authority_forgery_are_blocked(
    tmp_path,
) -> None:
    """Let only the guarded state machine mutate a pending event."""
    database = tmp_path / 'tts-raw-write.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    _consume(store, clock, 'tts-raw-write')
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            '''
            UPDATE trusted_result_tts_outbox
            SET template_key = template_key
            '''
        )
    store._connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            '''
            UPDATE trusted_result_tts_outbox
            SET physical_authorized = 1
            '''
        )
    store._connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        store._connection.execute(
            '''
            INSERT OR REPLACE INTO trusted_result_tts_outbox
            SELECT * FROM trusted_result_tts_outbox
            '''
        )
    store._connection.rollback()
    assert _event_row(store)['state'] == 'pending'
    store.close()


@pytest.mark.parametrize(
    ('assignment', 'forged_token'),
    (
        ("current_claim_token = 'A_valid_forged_claim_token_1234567890'", (
            'A_valid_forged_claim_token_1234567890'
        )),
        ("current_claim_request_id = 'forged-current-request'", None),
        ('attempt_count = 2, claim_fence = 2', None),
    ),
)
def test_live_path_revalidates_restored_trigger_claim_tamper(
    tmp_path,
    assignment,
    forged_token,
) -> None:
    """Reject current token, request, or fence drift before reopen."""
    database = tmp_path / (
        'tts-live-claim-tamper-'
        + hashlib.sha256(assignment.encode()).hexdigest()[:8]
        + '.sqlite3'
    )
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, database.stem)
    claim = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-live-tamper',
    )
    assert claim is not None
    store._connection.execute(
        'DROP TRIGGER trusted_result_tts_transition_guard'
    )
    store._connection.execute(
        'UPDATE trusted_result_tts_outbox SET ' + assignment
    )
    store._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL
    )
    store._connection.commit()

    with pytest.raises(TrustedResultTTSError, match='claim'):
        store.claim_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            'claim-live-tamper',
        )
    with pytest.raises(TrustedResultTTSError, match='claim'):
        store.acknowledge_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            event_id=claim.event_id,
            claim_token=forged_token or claim.claim_token,
            claim_fence=(2 if 'claim_fence' in assignment else 1),
        )
    store.close()


def test_append_only_ack_receipt_blocks_terminal_rewind(tmp_path) -> None:
    """Never redeliver an acknowledged event rewound to claimed."""
    database = tmp_path / 'tts-ack-rewind.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-ack-rewind')
    claim = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-ack-rewind',
        lease_seconds=30,
    )
    assert claim is not None
    clock.now += 1.0
    store.acknowledge_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        event_id=claim.event_id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
    )
    assert store._connection.execute(
        'SELECT COUNT(*) FROM trusted_result_tts_acknowledgements'
    ).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        store._connection.execute(
            '''
            UPDATE trusted_result_tts_acknowledgements
            SET acknowledged_at = acknowledged_at
            '''
        )
    store._connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        store._connection.execute(
            '''
            INSERT OR REPLACE INTO trusted_result_tts_acknowledgements
            SELECT * FROM trusted_result_tts_acknowledgements
            '''
        )
    store._connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        store._connection.execute(
            'DELETE FROM trusted_result_tts_acknowledgements'
        )
    store._connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        store._connection.execute(
            'DELETE FROM trusted_result_tts_claims'
        )
    store._connection.rollback()
    store._connection.execute(
        'DROP TRIGGER trusted_result_tts_transition_guard'
    )
    store._connection.execute(
        '''
        UPDATE trusted_result_tts_outbox
        SET state = 'claimed', lease_expires_at = ?,
            acknowledged_at = NULL, cancelled_at = NULL,
            cancellation_code = NULL, last_transition_at = claimed_at
        WHERE event_id = ?
        ''',
        (claim.lease_expires_at, claim.event_id),
    )
    store._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL
    )
    store._connection.commit()

    with pytest.raises(TrustedResultTTSError, match='ACK receipt'):
        store.claim_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            'claim-ack-rewind',
            lease_seconds=30,
        )
    store.close()
    with pytest.raises(TrustedResultTTSError, match='ACK receipt'):
        _simulation_store(str(database), clock=clock)


def test_conversation_delete_cascades_claim_and_ack_receipts(
    tmp_path,
) -> None:
    """Allow child deletion only through the trusted source cascade."""
    database = tmp_path / 'tts-ack-delete-cascade.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-ack-delete-cascade')
    claim = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-ack-delete-cascade',
    )
    assert claim is not None
    clock.now += 1.0
    store.acknowledge_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        event_id=claim.event_id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
    )
    assert store.delete(
        scenario.draft.user_id, scenario.draft.conversation_id
    )
    for table in (
        'trusted_result_tts_outbox',
        'trusted_result_tts_claims',
        'trusted_result_tts_acknowledgements',
    ):
        assert store._connection.execute(
            f'SELECT COUNT(*) FROM {table}'
        ).fetchone()[0] == 0
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
    ).fetchone()[0] == 1
    store.close()


def test_live_path_blocks_preactivation_cancel_rewind(
    tmp_path,
    monkeypatch,
) -> None:
    """Never claim an old result rewound from its activation tombstone."""
    database = tmp_path / 'tts-preactivation-rewind.sqlite3'
    clock = MutableClock()
    original_prepare = (
        conversation_module.prepare_trusted_result_tts_schema_locked
    )
    original_record = (
        conversation_module.record_or_verify_trusted_result_tts_locked
    )
    monkeypatch.setattr(
        conversation_module,
        'prepare_trusted_result_tts_schema_locked',
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        conversation_module,
        'record_or_verify_trusted_result_tts_locked',
        lambda *_args, **_kwargs: None,
    )
    old = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(old, clock, 'tts-preactivation-rewind')
    old.close()
    monkeypatch.setattr(
        conversation_module,
        'prepare_trusted_result_tts_schema_locked',
        original_prepare,
    )
    monkeypatch.setattr(
        conversation_module,
        'record_or_verify_trusted_result_tts_locked',
        original_record,
    )
    upgraded = _simulation_store(str(database), clock=clock)
    upgraded._connection.execute(
        'DROP TRIGGER trusted_result_tts_transition_guard'
    )
    upgraded._connection.execute(
        '''
        UPDATE trusted_result_tts_outbox
        SET state = 'pending', last_transition_at = created_at,
            cancelled_at = NULL, cancellation_code = NULL
        '''
    )
    upgraded._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL
    )
    upgraded._connection.commit()

    with pytest.raises(TrustedResultTTSError, match='activation state'):
        upgraded.claim_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            'claim-preactivation-rewind',
        )
    upgraded.close()


def test_live_and_reopen_reject_overlapping_claim_history(tmp_path) -> None:
    """Require every takeover to start at or after its prior lease."""
    database = tmp_path / 'tts-overlapping-claims.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, 'tts-overlapping-claims')
    first = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-overlap-first',
        lease_seconds=5,
    )
    assert first is not None
    clock.now = first.lease_expires_at
    second = store.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        'claim-overlap-second',
        lease_seconds=5,
    )
    assert second is not None
    second_row = dict(store._connection.execute(
        '''
        SELECT * FROM trusted_result_tts_claims
        WHERE claim_request_id = 'claim-overlap-second'
        '''
    ).fetchone())
    second_row['claimed_at'] = first.lease_expires_at - 1.0
    second_row['lease_expires_at'] = second_row['claimed_at'] + 5
    second_row['claim_request_fingerprint'] = (
        tts_module._claim_request_fingerprint(second_row)
    )
    store._connection.execute(
        'DROP TRIGGER trusted_result_tts_claim_no_update'
    )
    store._connection.execute(
        'DROP TRIGGER trusted_result_tts_transition_guard'
    )
    store._connection.execute(
        '''
        UPDATE trusted_result_tts_claims
        SET claim_request_fingerprint = ?, claimed_at = ?,
            lease_expires_at = ?
        WHERE claim_request_id = 'claim-overlap-second'
        ''',
        (
            second_row['claim_request_fingerprint'],
            second_row['claimed_at'],
            second_row['lease_expires_at'],
        ),
    )
    store._connection.execute(
        '''
        UPDATE trusted_result_tts_outbox
        SET current_claim_request_fingerprint = ?,
            claimed_at = ?, lease_expires_at = ?,
            last_transition_at = ?
        ''',
        (
            second_row['claim_request_fingerprint'],
            second_row['claimed_at'],
            second_row['lease_expires_at'],
            second_row['claimed_at'],
        ),
    )
    store._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_CLAIM_NO_UPDATE_SQL
    )
    store._connection.execute(
        tts_module.TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL
    )
    store._connection.commit()

    with pytest.raises(TrustedResultTTSError, match='claim'):
        store.claim_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            'claim-overlap-second',
            lease_seconds=5,
        )
    store.close()
    with pytest.raises(TrustedResultTTSError, match='claim'):
        _simulation_store(str(database), clock=clock)


def test_close_session_if_current_fences_reset_generation(tmp_path) -> None:
    """Never let a checked old speech generation close a reset session."""
    database = tmp_path / 'close-current.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    first = store.create('close-user', 'close-conversation')
    current = store.reset('close-user', 'close-conversation')

    with pytest.raises(ConversationChangedError):
        store.close_session_if_current(
            'close-user',
            'close-conversation',
            expected_session_instance_id=first.session_instance_id,
            expected_generation=first.generation,
        )
    still_current = store.get('close-user', 'close-conversation')
    assert still_current.status == 'active'
    assert still_current.generation == current.generation
    closed = store.close_session_if_current(
        'close-user',
        'close-conversation',
        expected_session_instance_id=current.session_instance_id,
        expected_generation=current.generation,
    )
    assert closed.status == 'closed'
    store.close()


@pytest.mark.parametrize(
    ('operation', 'cancellation_code'),
    (
        ('reset', 'conversation_reset'),
        ('close_session', 'conversation_inactive'),
    ),
)
def test_lifecycle_samples_clock_after_write_lock(
    tmp_path,
    operation,
    cancellation_code,
) -> None:
    """A queued lifecycle call must not cancel with a stale timestamp."""

    class ThreadBlockingClock(MutableClock):
        def __init__(self) -> None:
            super().__init__()
            self.block_lifecycle = False
            self.sampled = threading.Event()
            self.release = threading.Event()

        def __call__(self) -> float:
            sampled = self.now
            if (
                self.block_lifecycle
                and threading.current_thread().name == 'lifecycle-worker'
            ):
                self.sampled.set()
                self.release.wait(timeout=2.0)
            return sampled

    database = tmp_path / f'tts-{operation}-clock-race.sqlite3'
    clock = ThreadBlockingClock()
    store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(store, clock, f'tts-{operation}-race')
    errors = []

    def lifecycle_worker():
        try:
            getattr(store, operation)(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    clock.block_lifecycle = True
    with store._lock:
        worker = threading.Thread(
            target=lifecycle_worker,
            name='lifecycle-worker',
        )
        worker.start()
        clock.sampled.wait(timeout=0.1)
        clock.now = 102.0
        claim = store.claim_trusted_result_tts(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
            scenario.draft.speech_session_id,
            f'claim-before-{operation}',
        )
        assert claim is not None
        clock.release.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert errors == []
    row = _event_row(store)
    assert row['state'] == 'cancelled'
    assert row['cancellation_code'] == cancellation_code
    store.close()


def test_concurrent_claimers_create_one_fence_and_token(tmp_path) -> None:
    """Serialize independent connections on one oldest pending event."""
    database = tmp_path / 'tts-concurrent-claim.sqlite3'
    clock = MutableClock()
    first_store = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(
        first_store, clock, 'tts-concurrent-claim'
    )
    stores = [first_store] + [
        _simulation_store(str(database), clock=clock)
        for _index in range(7)
    ]
    barrier = threading.Barrier(len(stores))
    results = []
    errors = []

    def claim_worker(index, store):
        try:
            barrier.wait(timeout=3.0)
            results.append(
                store.claim_trusted_result_tts(
                    scenario.draft.user_id,
                    scenario.draft.conversation_id,
                    scenario.draft.speech_session_id,
                    f'concurrent-claim-{index}',
                )
            )
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    workers = [
        threading.Thread(target=claim_worker, args=(index, store))
        for index, store in enumerate(stores)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5.0)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    claims = [result for result in results if result is not None]
    assert len(claims) == 1
    assert claims[0].claim_fence == claims[0].attempt_number == 1
    claim_rows = first_store._connection.execute(
        'SELECT * FROM trusted_result_tts_claims'
    ).fetchall()
    assert len(claim_rows) == 1
    assert claim_rows[0]['claim_token'] == claims[0].claim_token
    for store in stores:
        store.close()


@pytest.mark.parametrize('lifecycle', ('reset', 'close_session'))
@pytest.mark.parametrize('winner', ('ack', 'lifecycle'))
def test_ack_and_lifecycle_are_serializable(
    tmp_path,
    monkeypatch,
    lifecycle,
    winner,
) -> None:
    """Preserve the transaction winner and fence the later operation."""
    database = tmp_path / f'tts-{winner}-{lifecycle}.sqlite3'
    clock = MutableClock()
    producer = _simulation_store(str(database), clock=clock)
    scenario, _receipt = _consume(
        producer, clock, f'tts-{winner}-{lifecycle}'
    )
    claim = producer.claim_trusted_result_tts(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
        scenario.draft.speech_session_id,
        f'claim-{winner}-{lifecycle}',
    )
    assert claim is not None
    producer.close()
    ack_store = _simulation_store(str(database), clock=clock)
    lifecycle_store = _simulation_store(str(database), clock=clock)
    entered = threading.Event()
    release = threading.Event()
    outcomes = {}

    def run_ack():
        try:
            outcomes['ack'] = ack_store.acknowledge_trusted_result_tts(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
                scenario.draft.speech_session_id,
                event_id=claim.event_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
            )
        except Exception as error:  # pragma: no cover - asserted below
            outcomes['ack_error'] = error

    def run_lifecycle():
        try:
            outcomes['lifecycle'] = getattr(lifecycle_store, lifecycle)(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
        except Exception as error:  # pragma: no cover - asserted below
            outcomes['lifecycle_error'] = error

    if winner == 'ack':
        original = conversation_module.acknowledge_trusted_result_tts_locked

        def pause_ack(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=3.0)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            conversation_module,
            'acknowledge_trusted_result_tts_locked',
            pause_ack,
        )
        first = threading.Thread(target=run_ack)
        second = threading.Thread(target=run_lifecycle)
    else:
        original = conversation_module.cancel_trusted_result_tts_locked

        def pause_lifecycle(*args, **kwargs):
            if threading.current_thread().name == 'lifecycle-winner':
                entered.set()
                assert release.wait(timeout=3.0)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            conversation_module,
            'cancel_trusted_result_tts_locked',
            pause_lifecycle,
        )
        first = threading.Thread(
            target=run_lifecycle,
            name='lifecycle-winner',
        )
        second = threading.Thread(target=run_ack)
    first.start()
    assert entered.wait(timeout=3.0)
    second.start()
    release.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)

    assert not first.is_alive() and not second.is_alive()
    assert 'lifecycle_error' not in outcomes
    row = lifecycle_store._connection.execute(
        'SELECT * FROM trusted_result_tts_outbox'
    ).fetchone()
    if winner == 'ack':
        assert 'ack_error' not in outcomes
        assert outcomes['ack'].state == 'acknowledged'
        assert row['state'] == 'acknowledged'
    else:
        assert isinstance(
            outcomes.get('ack_error'), TrustedResultTTSConflictError
        )
        assert row['state'] == 'cancelled'
    ack_store.close()
    lifecycle_store.close()
