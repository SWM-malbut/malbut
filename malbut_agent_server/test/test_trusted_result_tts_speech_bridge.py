"""Speech and scripted-HTTP bridge for durable result notifications."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from malbut_agent_server.http_server import make_server
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.speech import (
    SPEECH_SCHEMA_VERSION,
    SpeechActivityEvent,
    SpeechConversationCoordinator,
    SpeechTranscriptEvent,
    TTSRequest,
    TrustedSpeechBinding,
)
from test_monitor_room_simulation_execution import (
    MutableClock,
    _scenario,
    _simulation_store,
)


_AUTH_TOKEN = 'trusted-result-scripted-token-0123456789abcdef'


def _binding(scenario) -> TrustedSpeechBinding:
    return TrustedSpeechBinding.from_dict(
        {
            'user_id': scenario.draft.user_id,
            'speaker_id': 'trusted-result-speaker',
            'speech_session_id': scenario.draft.speech_session_id,
            'conversation_id': scenario.draft.conversation_id,
            'source': 'trusted-result-test',
        }
    )


def _activity(scenario, **overrides) -> SpeechActivityEvent:
    value = {
        'schema_version': SPEECH_SCHEMA_VERSION,
        'event_id': 'trusted-result-activity-1',
        'speech_session_id': scenario.draft.speech_session_id,
        'speaker_id': 'trusted-result-speaker',
        'source': 'trusted-result-test',
        'capture_epoch': 1,
        'source_timestamp_ns': 2_000_000_000,
    }
    value.update(overrides)
    return SpeechActivityEvent.from_dict(value)


def _transcript(scenario, **overrides) -> SpeechTranscriptEvent:
    value = {
        'schema_version': SPEECH_SCHEMA_VERSION,
        'utterance_id': 'trusted-result-utterance-1',
        'speech_session_id': scenario.draft.speech_session_id,
        'conversation_id': scenario.draft.conversation_id,
        'speaker_id': 'trusted-result-speaker',
        'source': 'trusted-result-test',
        'sequence': 1,
        'capture_epoch': 1,
        'source_timestamp_ns': 1_000_000_000,
        'text': '다음 질문',
        'confidence': 0.99,
        'is_final': True,
        'capture_origin': 'microphone',
        'audio_metadata': {
            'duration_ms': 500,
            'sample_rate_hz': 16000,
            'channel_count': 1,
        },
    }
    value.update(overrides)
    return SpeechTranscriptEvent.from_dict(value)


@contextmanager
def _runtime(tmp_path, suffix='bridge'):
    clock = MutableClock()
    database = tmp_path / f'{suffix}.sqlite3'
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix=suffix)
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    memory_store = SQLiteMemoryStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=MockProvider(),
        memory_store=memory_store,
        conversation_store=store,
        safety_policy=SafetyPolicy(),
    )
    coordinator = SpeechConversationCoordinator(orchestrator)
    opened = coordinator.open_session(_binding(scenario))
    assert opened.code == 'session_opened'
    try:
        yield coordinator, store, clock, scenario, memory_store
    finally:
        store.close()
        memory_store.close()


def _row(store):
    return store._connection.execute(
        'SELECT * FROM trusted_result_tts_outbox'
    ).fetchone()


def _claim(coordinator, scenario, request_id='bridge-claim-1', lease=30):
    result = coordinator.claim_trusted_result_tts(
        scenario.draft.speech_session_id,
        request_id,
        lease_seconds=lease,
    )
    assert result.status == 'ready'
    assert result.tts_request is not None
    return result, result.tts_request


def test_claim_is_exactly_bound_and_replays_without_a_second_slot(
    tmp_path,
) -> None:
    """Keep private durable identity while exposing no claim credential."""
    with _runtime(tmp_path, 'claim-replay') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        first_result, first = _claim(coordinator, scenario)
        epoch = first_result.capture_epoch
        assert first_result.physical_audio_verified is False
        replay = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'bridge-claim-1',
            lease_seconds=30,
        )

        assert replay.code == 'trusted_result_tts_claim_replayed'
        assert replay.capture_epoch == epoch
        assert replay.tts_request == first
        assert _row(store)['attempt_count'] == 1
        session = store.get(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
        )
        assert first.user_id == scenario.draft.user_id
        assert first.conversation_session_instance_id == (
            session.session_instance_id
        )
        assert first.conversation_generation == session.generation
        public = first.to_dict()
        assert public['physical_audio_verified'] is False
        assert public['physical_authorized'] is False
        assert 'claim_token' not in public
        assert 'claim_request_id' not in public
        assert 'user_id' not in public
        assert 'conversation_session_instance_id' not in public


def test_normal_work_and_notification_lane_mutually_exclude_claim(
    tmp_path,
) -> None:
    """Do not overlap normal TTS, confirmation, inference, or claims."""
    with _runtime(tmp_path, 'lane-blockers') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        state = coordinator._sessions[scenario.draft.speech_session_id]
        normal = TTSRequest(
            schema_version=1,
            request_id='normal-active-request',
            speech_session_id=scenario.draft.speech_session_id,
            conversation_id=scenario.draft.conversation_id,
            turn_id='normal-active-turn',
            source_utterance_id='normal-active-utterance',
            text='일반 응답',
        )
        with state.lock:
            state.active_tts = normal
        assert coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'blocked-by-normal',
        ).code == 'normal_tts_playback_active'
        with state.lock:
            state.active_tts = None
            state.pending_confirmation = object()
        assert coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'blocked-by-confirmation',
        ).code == 'confirmation_pending'
        with state.lock:
            state.pending_confirmation = None
            state.in_flight = object()
        assert coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'blocked-by-inference',
        ).code == 'inference_in_progress'
        with state.lock:
            state.in_flight = None

        _result, request = _claim(
            coordinator,
            scenario,
            'notification-active',
        )
        blocked = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'second-notification',
        )
        assert blocked.code == 'trusted_result_tts_playback_active'
        transcript = coordinator.handle_transcript(_transcript(scenario))
        assert transcript.code == 'trusted_result_tts_playback_active'
        generic = coordinator.mark_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
        )
        assert generic.code == 'stale_tts_result'
        assert _row(store)['state'] == 'claimed'


def test_barge_in_emits_one_cancel_then_dedicated_terminal_acks(
    tmp_path,
) -> None:
    """Treat cancel completion as terminal ACK, never audible success."""
    with _runtime(tmp_path, 'barge-terminal') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        _result, request = _claim(coordinator, scenario)
        activity = _activity(scenario)
        first = coordinator.handle_barge_in(activity)
        replay = coordinator.handle_barge_in(activity)

        assert first == replay
        assert first.code == 'trusted_result_tts_cancel_requested'
        assert first.cancel_request is not None
        assert first.cancel_request.tts_request_id == request.request_id
        state = coordinator._sessions[scenario.draft.speech_session_id]
        assert state.active_trusted_result_tts is None
        assert state.terminal_pending_trusted_result_tts is request
        blocked = coordinator.handle_transcript(
            _transcript(scenario, capture_epoch=2)
        )
        assert blocked.code == 'trusted_result_tts_terminal_pending'
        generic = coordinator.mark_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
        )
        assert generic.code == 'stale_tts_result'
        assert _row(store)['state'] == 'claimed'

        terminal = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert terminal.code == 'trusted_result_tts_terminal'
        assert terminal.capture_epoch == 2
        assert terminal.to_dict()['physical_audio_verified'] is False
        assert _row(store)['state'] == 'acknowledged'
        assert state.terminal_pending_trusted_result_tts is None
        terminal_replay = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert terminal_replay.code == (
            'trusted_result_tts_already_terminal'
        )


def test_close_cancels_once_and_storage_rejects_late_terminal(
    tmp_path,
) -> None:
    """Let durable conversation close cancel before any late ACK."""
    with _runtime(tmp_path, 'close-cancel') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        _result, request = _claim(coordinator, scenario)
        first = coordinator.close_session(
            scenario.draft.speech_session_id,
            'notification-close-1',
        )
        replay = coordinator.close_session(
            scenario.draft.speech_session_id,
            'notification-close-1',
        )
        assert first == replay
        assert first.cancel_request is not None
        assert first.cancel_request.tts_request_id == request.request_id
        assert _row(store)['state'] == 'cancelled'
        late = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert late.code == 'speech_session_closed'
        assert _row(store)['state'] == 'cancelled'


@pytest.mark.parametrize('lifecycle', ['reset', 'delete'])
def test_reset_or_delete_fences_late_notification_terminal(
    tmp_path,
    lifecycle,
) -> None:
    """Never carry a delivery credential into another generation."""
    with _runtime(tmp_path, f'lifecycle-{lifecycle}') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        _result, request = _claim(coordinator, scenario)
        if lifecycle == 'reset':
            store.reset(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
        else:
            assert store.delete(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
        late = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert late.status == 'rejected'
        assert late.code in {
            'conversation_not_found',
            'conversation_changed_during_trusted_result_tts',
        }
        assert late.cancel_request is not None
        if lifecycle == 'reset':
            assert _row(store)['state'] == 'cancelled'
        else:
            assert _row(store) is None


@pytest.mark.parametrize('lifecycle', ['reset', 'delete', 'close'])
def test_lifecycle_wins_while_terminal_store_call_is_reserved(
    tmp_path,
    monkeypatch,
    lifecycle,
) -> None:
    """Serialize reset, delete, or close before a delayed durable ACK."""
    with _runtime(tmp_path, f'terminal-race-{lifecycle}') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        _result, request = _claim(coordinator, scenario)
        original = store.acknowledge_trusted_result_tts
        entered = threading.Event()
        release = threading.Event()

        def delayed_ack(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=3)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            store,
            'acknowledge_trusted_result_tts',
            delayed_ack,
        )
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.setdefault(
                'result',
                coordinator.mark_trusted_result_tts_terminal(
                    scenario.draft.speech_session_id,
                    request.request_id,
                    request.terminal_request_id,
                ),
            )
        )
        thread.start()
        assert entered.wait(timeout=2)
        if lifecycle == 'reset':
            store.reset(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
        elif lifecycle == 'delete':
            assert store.delete(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
        else:
            closed = coordinator.close_session(
                scenario.draft.speech_session_id,
                'close-during-terminal',
            )
            assert closed.cancel_request is not None
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert outcome['result'].status == 'rejected'
        if lifecycle == 'close':
            assert outcome['result'].code == 'speech_session_closed'
            state = coordinator._sessions[
                scenario.draft.speech_session_id
            ]
            assert state.capture_epoch == closed.capture_epoch
        if lifecycle == 'delete':
            assert _row(store) is None
        else:
            assert _row(store)['state'] == 'cancelled'


def test_lease_boundary_accepts_before_and_rejects_at_expiry(
    tmp_path,
) -> None:
    """Use the durable store clock for the exact ACK lease boundary."""
    with _runtime(tmp_path, 'lease-before') as runtime:
        coordinator, store, clock, scenario, _memory = runtime
        _result, request = _claim(
            coordinator, scenario, 'lease-before', lease=1
        )
        clock.now += 0.999
        accepted = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert accepted.code == 'trusted_result_tts_terminal'
        assert _row(store)['state'] == 'acknowledged'

    with _runtime(tmp_path, 'lease-at') as runtime:
        coordinator, store, clock, scenario, _memory = runtime
        _result, request = _claim(
            coordinator, scenario, 'lease-at', lease=1
        )
        clock.now += 1.0
        rejected = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert rejected.code == 'stale_trusted_result_tts_terminal'
        assert rejected.cancel_request is not None
        assert rejected.cancel_request.reason == (
            'trusted_result_invalidated'
        )
        assert _row(store)['state'] == 'claimed'
        state = coordinator._sessions[scenario.draft.speech_session_id]
        assert state.active_trusted_result_tts is None
        assert state.terminal_pending_trusted_result_tts is request

        blocked = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'lease-at-next-claim',
        )
        assert blocked.code == 'trusted_result_tts_terminal_pending'
        cancel_terminal = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert cancel_terminal.code == (
            'trusted_result_tts_cancel_terminal'
        )
        cancel_terminal_replay = (
            coordinator.mark_trusted_result_tts_terminal(
                scenario.draft.speech_session_id,
                request.request_id,
                request.terminal_request_id,
            )
        )
        assert cancel_terminal_replay.code == (
            'trusted_result_tts_cancel_already_terminal'
        )
        reclaimed = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'lease-at-next-claim',
        )
        assert reclaimed.code == 'trusted_result_tts_claimed'
        assert reclaimed.tts_request is not None
        assert reclaimed.tts_request.claim_fence == 2


def test_expired_active_claim_requires_cancel_terminal_before_reclaim(
    tmp_path,
) -> None:
    """Do not equate lease expiry with downstream playback stopping."""
    with _runtime(tmp_path, 'lease-expiry-cancel') as runtime:
        coordinator, store, clock, scenario, _memory = runtime
        _result, request = _claim(
            coordinator,
            scenario,
            'expiring-active-claim',
            lease=1,
        )
        state = coordinator._sessions[scenario.draft.speech_session_id]
        clock.now += 1.0

        expired = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'expiring-active-claim',
            lease_seconds=1,
        )
        assert expired.status == 'cancel_pending'
        assert expired.code == (
            'trusted_result_tts_claim_expired_cancel_requested'
        )
        assert expired.tts_request is None
        assert expired.cancel_request is not None
        assert expired.cancel_request.reason == 'lease_expired'
        assert expired.cancel_request.tts_request_id == request.request_id
        assert expired.physical_audio_verified is False
        assert state.active_trusted_result_tts is None
        assert state.terminal_pending_trusted_result_tts is request
        assert _row(store)['state'] == 'claimed'

        epoch = state.capture_epoch
        replay = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'expiring-active-claim',
            lease_seconds=1,
        )
        assert replay == expired
        assert state.capture_epoch == epoch
        blocked_claim = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'new-claim-before-cancel-terminal',
        )
        assert blocked_claim.code == 'trusted_result_tts_terminal_pending'
        blocked_transcript = coordinator.handle_transcript(
            _transcript(scenario, capture_epoch=epoch)
        )
        assert blocked_transcript.code == (
            'trusted_result_tts_terminal_pending'
        )

        cancel_terminal = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert cancel_terminal.status == 'ready'
        assert cancel_terminal.code == (
            'trusted_result_tts_cancel_terminal'
        )
        assert cancel_terminal.physical_audio_verified is False
        assert state.terminal_pending_trusted_result_tts is None
        assert _row(store)['state'] == 'claimed'
        terminal_replay = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert terminal_replay.code == (
            'trusted_result_tts_cancel_already_terminal'
        )

        reclaimed = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'new-claim-after-cancel-terminal',
        )
        assert reclaimed.status == 'ready'
        assert reclaimed.tts_request is not None
        assert reclaimed.tts_request.claim_fence == 2
        assert _row(store)['attempt_count'] == 2


def test_store_calls_run_without_holding_the_speech_state_lock(
    tmp_path,
    monkeypatch,
) -> None:
    """Prove claim and ACK I/O both happen outside the state lock."""
    with _runtime(tmp_path, 'unlocked-store') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        state = coordinator._sessions[scenario.draft.speech_session_id]
        original_claim = store.claim_trusted_result_tts
        claim_entered = threading.Event()
        claim_release = threading.Event()

        def blocking_claim(*args, **kwargs):
            claim_entered.set()
            assert claim_release.wait(timeout=3)
            return original_claim(*args, **kwargs)

        monkeypatch.setattr(
            store,
            'claim_trusted_result_tts',
            blocking_claim,
        )
        claim_outcome = {}
        claim_thread = threading.Thread(
            target=lambda: claim_outcome.setdefault(
                'result',
                coordinator.claim_trusted_result_tts(
                    scenario.draft.speech_session_id,
                    'unlocked-claim',
                ),
            )
        )
        claim_thread.start()
        assert claim_entered.wait(timeout=2)
        assert state.lock.acquire(blocking=False)
        state.lock.release()
        assert coordinator.handle_transcript(
            _transcript(scenario)
        ).code == 'trusted_result_tts_claim_in_progress'
        assert coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'second-while-reserved',
        ).code == 'trusted_result_tts_claim_reserved'
        claim_release.set()
        claim_thread.join(timeout=3)
        assert not claim_thread.is_alive()
        request = claim_outcome['result'].tts_request
        assert request is not None

        original_ack = store.acknowledge_trusted_result_tts
        ack_entered = threading.Event()
        ack_release = threading.Event()

        def blocking_ack(*args, **kwargs):
            ack_entered.set()
            assert ack_release.wait(timeout=3)
            return original_ack(*args, **kwargs)

        monkeypatch.setattr(
            store,
            'acknowledge_trusted_result_tts',
            blocking_ack,
        )
        ack_outcome = {}
        ack_thread = threading.Thread(
            target=lambda: ack_outcome.setdefault(
                'result',
                coordinator.mark_trusted_result_tts_terminal(
                    scenario.draft.speech_session_id,
                    request.request_id,
                    request.terminal_request_id,
                ),
            )
        )
        ack_thread.start()
        assert ack_entered.wait(timeout=2)
        assert state.lock.acquire(blocking=False)
        state.lock.release()
        ack_release.set()
        ack_thread.join(timeout=3)
        assert not ack_thread.is_alive()
        assert ack_outcome['result'].code == (
            'trusted_result_tts_terminal'
        )


def test_sqlite_write_lock_releases_claim_and_terminal_reservations(
    tmp_path,
    monkeypatch,
) -> None:
    """Turn expected persistence failures into retryable local state."""
    with _runtime(tmp_path, 'sqlite-write-lock') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        state = coordinator._sessions[scenario.draft.speech_session_id]
        store._connection.execute('PRAGMA busy_timeout=1')
        locker = sqlite3.connect(store.database_path, timeout=0.001)
        try:
            original_claim = store.claim_trusted_result_tts

            def locked_claim(*args, **kwargs):
                locker.execute('BEGIN IMMEDIATE')
                try:
                    return original_claim(*args, **kwargs)
                finally:
                    locker.rollback()

            monkeypatch.setattr(
                store,
                'claim_trusted_result_tts',
                locked_claim,
            )
            failed_claim = coordinator.claim_trusted_result_tts(
                scenario.draft.speech_session_id,
                'sqlite-locked-claim',
            )
            assert failed_claim.status == 'retryable'
            assert failed_claim.code == (
                'trusted_result_tts_store_unavailable'
            )
            assert state.trusted_result_tts_claim_reservation is None

            monkeypatch.setattr(
                store,
                'claim_trusted_result_tts',
                original_claim,
            )
            claimed = coordinator.claim_trusted_result_tts(
                scenario.draft.speech_session_id,
                'sqlite-locked-claim',
            )
            request = claimed.tts_request
            assert request is not None

            original_terminal = store.acknowledge_trusted_result_tts

            def locked_terminal(*args, **kwargs):
                locker.execute('BEGIN IMMEDIATE')
                try:
                    return original_terminal(*args, **kwargs)
                finally:
                    locker.rollback()

            monkeypatch.setattr(
                store,
                'acknowledge_trusted_result_tts',
                locked_terminal,
            )
            failed_terminal = (
                coordinator.mark_trusted_result_tts_terminal(
                    scenario.draft.speech_session_id,
                    request.request_id,
                    request.terminal_request_id,
                )
            )
            assert failed_terminal.status == 'retryable'
            assert failed_terminal.code == (
                'trusted_result_tts_store_unavailable'
            )
            assert state.trusted_result_tts_terminal_reservation is None
            assert state.active_trusted_result_tts is request

            monkeypatch.setattr(
                store,
                'acknowledge_trusted_result_tts',
                original_terminal,
            )
            terminal = coordinator.mark_trusted_result_tts_terminal(
                scenario.draft.speech_session_id,
                request.request_id,
                request.terminal_request_id,
            )
            assert terminal.code == 'trusted_result_tts_terminal'
        finally:
            locker.close()


def test_claim_cas_loss_keeps_exact_retry_reachable(
    tmp_path,
    monkeypatch,
) -> None:
    """Expose a typed exact retry after local control wins post-commit."""
    with _runtime(tmp_path, 'claim-cas-loss') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        original = store.claim_trusted_result_tts
        committed = threading.Event()
        release = threading.Event()

        def commit_then_block(*args, **kwargs):
            claim = original(*args, **kwargs)
            committed.set()
            assert release.wait(timeout=3)
            return claim

        monkeypatch.setattr(
            store,
            'claim_trusted_result_tts',
            commit_then_block,
        )
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.setdefault(
                'result',
                coordinator.claim_trusted_result_tts(
                    scenario.draft.speech_session_id,
                    'cas-loss-claim',
                ),
            )
        )
        thread.start()
        assert committed.wait(timeout=2)
        advanced = coordinator.handle_barge_in(_activity(scenario))
        assert advanced.code == 'capture_epoch_advanced'
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert outcome['result'].code == (
            'trusted_result_tts_claim_superseded_retry_exact'
        )
        assert _row(store)['state'] == 'claimed'
        retry = coordinator.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'cas-loss-claim',
        )
        assert retry.code == 'trusted_result_tts_claimed'
        assert retry.tts_request is not None
        assert retry.tts_request.claim_fence == 1


@pytest.mark.parametrize('lifecycle', ['reset', 'delete'])
def test_post_commit_claim_is_not_published_after_lifecycle_change(
    tmp_path,
    monkeypatch,
    lifecycle,
) -> None:
    """Re-attest after commit before publishing a canceled credential."""
    with _runtime(tmp_path, f'claim-post-commit-{lifecycle}') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        original = store.claim_trusted_result_tts
        committed = threading.Event()
        release = threading.Event()

        def commit_then_block(*args, **kwargs):
            claim = original(*args, **kwargs)
            committed.set()
            assert release.wait(timeout=3)
            return claim

        monkeypatch.setattr(
            store,
            'claim_trusted_result_tts',
            commit_then_block,
        )
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.setdefault(
                'result',
                coordinator.claim_trusted_result_tts(
                    scenario.draft.speech_session_id,
                    f'post-commit-{lifecycle}',
                ),
            )
        )
        thread.start()
        assert committed.wait(timeout=2)
        if lifecycle == 'reset':
            store.reset(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
        else:
            assert store.delete(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        result = outcome['result']
        assert result.status == 'rejected'
        assert result.tts_request is None
        assert result.code in {
            'conversation_not_found',
            'conversation_changed_during_trusted_result_tts',
        }
        state = coordinator._sessions[
            scenario.draft.speech_session_id
        ]
        assert state.closed is True
        assert state.active_trusted_result_tts is None
        if lifecycle == 'reset':
            assert _row(store)['state'] == 'cancelled'
        else:
            assert _row(store) is None


def test_terminal_replay_revalidates_generation_after_reset(tmp_path) -> None:
    """Do not replay a prior-generation terminal cache as current."""
    with _runtime(tmp_path, 'terminal-replay-reset') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        _result, request = _claim(coordinator, scenario)
        terminal = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert terminal.code == 'trusted_result_tts_terminal'
        store.reset(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
        )
        replay = coordinator.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            request.request_id,
            request.terminal_request_id,
        )
        assert replay.status == 'rejected'
        assert replay.code == (
            'conversation_changed_during_trusted_result_tts'
        )
        assert replay.to_dict()['physical_audio_verified'] is False


def test_restart_reconstructs_live_claim_but_never_redelivers_after_ack(
    tmp_path,
) -> None:
    """Recover a live lease and suppress an acknowledged notification."""
    with _runtime(tmp_path, 'restart-claim') as runtime:
        coordinator, store, _clock, scenario, _memory = runtime
        _result, before_restart = _claim(
            coordinator,
            scenario,
            'restart-stable-claim',
        )

        restarted = SpeechConversationCoordinator(
            coordinator.orchestrator
        )
        assert restarted.open_session(_binding(scenario)).code == (
            'session_opened'
        )
        recovered = restarted.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'restart-stable-claim',
        )
        assert recovered.code == 'trusted_result_tts_claimed'
        assert recovered.tts_request == before_restart
        assert _row(store)['attempt_count'] == 1
        terminal = restarted.mark_trusted_result_tts_terminal(
            scenario.draft.speech_session_id,
            before_restart.request_id,
            before_restart.terminal_request_id,
        )
        assert terminal.code == 'trusted_result_tts_terminal'

        after_ack_restart = SpeechConversationCoordinator(
            coordinator.orchestrator
        )
        assert after_ack_restart.open_session(_binding(scenario)).code == (
            'session_opened'
        )
        no_redelivery = after_ack_restart.claim_trusted_result_tts(
            scenario.draft.speech_session_id,
            'restart-stable-claim',
        )
        assert no_redelivery.status == 'empty'
        assert no_redelivery.tts_request is None
        stale_terminal = (
            after_ack_restart.mark_trusted_result_tts_terminal(
                scenario.draft.speech_session_id,
                before_restart.request_id,
                before_restart.terminal_request_id,
            )
        )
        assert stale_terminal.code == 'stale_trusted_result_tts_terminal'
        assert _row(store)['state'] == 'acknowledged'


def _post(url, payload, token=''):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_scripted_http_bridge_is_opt_in_strict_and_non_authorizing(
    tmp_path,
) -> None:
    """Expose explicit authenticated claim/terminal routes only."""
    with _runtime(tmp_path, 'scripted-http') as runtime:
        coordinator, store, _clock, scenario, memory_store = runtime
        disabled = make_server(
            '127.0.0.1',
            0,
            coordinator.orchestrator,
            auth_token=_AUTH_TOKEN,
            allowed_user_id=scenario.draft.user_id,
        )
        disabled_thread = threading.Thread(target=disabled.serve_forever)
        disabled_thread.start()
        disabled_host, disabled_port = disabled.server_address
        try:
            status, error = _post(
                'http://'
                f'{disabled_host}:{disabled_port}'
                '/v1/speech/scripted/trusted-result-tts/claim',
                {
                    'speech_session_id': (
                        scenario.draft.speech_session_id
                    ),
                    'claim_request_id': 'disabled-claim',
                },
                _AUTH_TOKEN,
            )
            assert status == 404
            assert error['error']['code'] == 'not_found'
        finally:
            disabled.shutdown()
            disabled.server_close()
            disabled_thread.join(timeout=2)
        assert _row(store)['state'] == 'pending'

        server = make_server(
            '127.0.0.1',
            0,
            coordinator.orchestrator,
            auth_token=_AUTH_TOKEN,
            allowed_user_id=scenario.draft.user_id,
            speech_coordinator=coordinator,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        host, port = server.server_address
        base = f'http://{host}:{port}'
        claim_url = (
            base
            + '/v1/speech/scripted/trusted-result-tts/claim'
        )
        terminal_url = (
            base
            + '/v1/speech/scripted/trusted-result-tts/terminal'
        )
        try:
            status, error = _post(
                claim_url,
                {
                    'speech_session_id': (
                        scenario.draft.speech_session_id
                    ),
                    'claim_request_id': 'http-claim',
                },
            )
            assert status == 401
            assert error['error']['code'] == 'unauthorized'

            for forbidden in (
                'claim_token',
                'user_id',
                'conversation_id',
                'text',
                'result_code',
            ):
                status, error = _post(
                    claim_url,
                    {
                        'speech_session_id': (
                            scenario.draft.speech_session_id
                        ),
                        'claim_request_id': f'http-{forbidden}',
                        forbidden: 'untrusted',
                    },
                    _AUTH_TOKEN,
                )
                assert status == 400
                assert error['error']['code'] == 'validation_error'

            status, claimed = _post(
                claim_url,
                {
                    'speech_session_id': (
                        scenario.draft.speech_session_id
                    ),
                    'claim_request_id': 'http-claim',
                    'lease_seconds': 30,
                },
                _AUTH_TOKEN,
            )
            assert status == 200
            assert claimed['runtime'] == 'scripted_text_only'
            assert claimed['physical_authority'] is False
            assert claimed['physical_audio_verified'] is False
            result = claimed['result']
            assert result['physical_audio_verified'] is False
            request = result['tts_request']
            assert request['physical_audio_verified'] is False
            assert 'claim_token' not in request
            assert 'user_id' not in request
            assert _row(store)['state'] == 'claimed'

            for forbidden in (
                'claim_token',
                'user_id',
                'conversation_id',
                'text',
                'result_code',
            ):
                status, error = _post(
                    terminal_url,
                    {
                        'speech_session_id': (
                            scenario.draft.speech_session_id
                        ),
                        'tts_request_id': request['request_id'],
                        'terminal_request_id': (
                            request['terminal_request_id']
                        ),
                        forbidden: 'untrusted',
                    },
                    _AUTH_TOKEN,
                )
                assert status == 400
                assert error['error']['code'] == 'validation_error'

            status, terminal = _post(
                terminal_url,
                {
                    'speech_session_id': (
                        scenario.draft.speech_session_id
                    ),
                    'tts_request_id': request['request_id'],
                    'terminal_request_id': (
                        request['terminal_request_id']
                    ),
                },
                _AUTH_TOKEN,
            )
            assert status == 200
            assert terminal['physical_audio_verified'] is False
            assert terminal['result']['code'] == (
                'trusted_result_tts_terminal'
            )
            assert terminal['result']['physical_audio_verified'] is False
            assert _row(store)['state'] == 'acknowledged'
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        # The outer runtime owns these dependencies; server_close must not
        # create or drain any additional notification.
        assert memory_store is coordinator.orchestrator.memory_store


def test_scripted_http_bridge_hides_cross_user_speech_session(
    tmp_path,
) -> None:
    """Do not let one server identity access another user's session."""
    with _runtime(tmp_path, 'scripted-http-cross-user') as runtime:
        coordinator, store, _clock, scenario, _memory_store = runtime
        assert scenario.draft.user_id != 'different-scripted-user'
        server = make_server(
            '127.0.0.1',
            0,
            coordinator.orchestrator,
            auth_token=_AUTH_TOKEN,
            allowed_user_id='different-scripted-user',
            speech_coordinator=coordinator,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        host, port = server.server_address
        base = f'http://{host}:{port}'
        try:
            status, error = _post(
                base + '/v1/speech/scripted/trusted-result-tts/claim',
                {
                    'speech_session_id': (
                        scenario.draft.speech_session_id
                    ),
                    'claim_request_id': 'cross-user-http-claim',
                },
                _AUTH_TOKEN,
            )
            assert status == 404
            assert error['error']['code'] == 'conversation_not_found'
            assert _row(store)['state'] == 'pending'

            _result, request = _claim(
                coordinator,
                scenario,
                'cross-user-direct-claim',
            )
            status, error = _post(
                base + '/v1/speech/scripted/trusted-result-tts/terminal',
                {
                    'speech_session_id': (
                        scenario.draft.speech_session_id
                    ),
                    'tts_request_id': request.request_id,
                    'terminal_request_id': request.terminal_request_id,
                },
                _AUTH_TOKEN,
            )
            assert status == 404
            assert error['error']['code'] == 'conversation_not_found'
            assert _row(store)['state'] == 'claimed'

            status, error = _post(
                base + '/v1/speech/scripted/sessions/close',
                {
                    'speech_session_id': (
                        scenario.draft.speech_session_id
                    ),
                    'control_id': 'cross-user-http-close',
                },
                _AUTH_TOKEN,
            )
            assert status == 404
            assert error['error']['code'] == 'conversation_not_found'
            assert _row(store)['state'] == 'claimed'
            session = store.get(
                scenario.draft.user_id,
                scenario.draft.conversation_id,
            )
            assert session.status == 'active'
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
