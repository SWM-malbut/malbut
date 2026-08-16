"""Focused tests for server-owned durable Gazebo command progress."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading

import pytest

import test_gazebo_execution_outbox as outbox_tests
import test_monitor_room_simulation_execution as simulation_tests
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gazebo_command_progress import (
    GAZEBO_COMMAND_PROGRESS_ACTIVATION_SENTINEL,
    GazeboCommandProgressError,
    GazeboCommandProgressService,
)
from malbut_agent_server.gazebo_monitor_room_command_runner import (
    GazeboMonitorRoomCommandRunner,
)
from malbut_agent_server.gazebo_monitor_room_gateway_client import (
    GazeboMonitorRoomGatewayClient,
    GazeboMonitorRoomGatewayResult,
)


_DIGEST = hashlib.sha256(b'gazebo-command-progress-test').hexdigest()


def _client(tmp_path):
    return GazeboMonitorRoomGatewayClient(
        str(tmp_path / 'g.sock'),
        expected_server_uid=os.geteuid(),
        timeout_seconds=1.0,
    )


def _activate_and_prepare(tmp_path, monkeypatch, *, suffix):
    database = tmp_path / f'p-{suffix}.sqlite3'
    store, wall, boot, _target, _semantic, _robot, policy = (
        outbox_tests._configured_store(database, monkeypatch)
    )
    runner = GazeboMonitorRoomCommandRunner(
        store,
        _client(tmp_path),
        user_id='simulation-user',
    )
    progress = GazeboCommandProgressService(runner, timeout_seconds=1.0)
    scenario = simulation_tests._scenario(store, wall, suffix=suffix)
    consumed = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert consumed.enqueue is not None
    claim = store.claim_gazebo_execution(
        f'gazebo-progress-claim-{suffix}',
        lease_seconds=30,
    )
    assert claim is not None
    store.acknowledge_gazebo_execution(
        outbox_id=claim.outbox_id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        prepare_fingerprint=_DIGEST,
    )
    return {
        'database': database,
        'store': store,
        'wall': wall,
        'boot': boot,
        'policy': policy,
        'runner': runner,
        'progress': progress,
        'scenario': scenario,
        'confirmation': scenario.draft.confirmation_request_id,
    }


def _result(request_id, operation_id, command, state='navigating'):
    terminal = state in {
        'delivery_unknown', 'cancel_unknown', 'succeeded', 'failed',
        'canceled',
    }
    succeeded = state == 'succeeded'
    return GazeboMonitorRoomGatewayResult(
        request_id=request_id,
        operation_id=operation_id,
        command=command,
        state=state,
        current_sample_index=0,
        navigation_samples_total=1,
        navigation_samples_reached=1 if succeeded else 0,
        terminal=terminal,
        robot_blocked=state != 'succeeded' and state != 'failed'
        and state != 'canceled',
        terminal_code=f'{state}_code' if terminal else None,
        evidence_digest=_DIGEST,
    )


def _script_exchange(monkeypatch, states, calls):
    queue = list(states)

    def exchange(
        _client_value,
        *,
        request_id,
        operation_id,
        command,
        timeout_seconds,
    ):
        assert 0.0 < timeout_seconds <= 1.0
        calls.append((request_id, operation_id, command))
        state = queue.pop(0)
        if isinstance(state, BaseException):
            raise state
        return _result(request_id, operation_id, command, state)

    monkeypatch.setattr(
        GazeboMonitorRoomGatewayClient, 'exchange', exchange
    )


def test_status_issues_one_redacted_intent_pair_and_exact_replays(
    tmp_path, monkeypatch
):
    context = _activate_and_prepare(
        tmp_path, monkeypatch, suffix='exact-replay'
    )
    calls = []
    _script_exchange(monkeypatch, ['navigating'], calls)
    initial = context['progress'].status(context['confirmation'])
    repeated_status = context['progress'].status(context['confirmation'])
    assert repeated_status.next_intent_id == initial.next_intent_id
    assert repeated_status.cancel_intent_id == initial.cancel_intent_id
    result = context['progress'].advance(
        context['confirmation'], initial.next_intent_id
    )
    replay = context['progress'].advance(
        context['confirmation'], initial.next_intent_id
    )
    assert replay.to_public_dict() == result.to_public_dict()
    assert len(calls) == 1
    public = json.dumps(result.to_public_dict(), sort_keys=True)
    for private in (
        'outbox', 'operation', 'fence', 'owner', 'coordinate',
        'evidence', 'cursor',
    ):
        assert private not in public
    assert result.physical_authorized is False
    assert result.physical_effects is False
    assert result.viewer_live is False
    assert result.camera_coverage_validated is False
    assert result.coverage_achieved is False
    assert initial.next_intent_id not in repr(initial)
    context['store'].close()


def test_pending_retry_after_restart_reuses_exact_gateway_request(
    tmp_path, monkeypatch
):
    context = _activate_and_prepare(
        tmp_path, monkeypatch, suffix='pending-restart'
    )
    initial = context['progress'].status(context['confirmation'])
    calls = []
    _script_exchange(monkeypatch, [RuntimeError('lost')], calls)
    with pytest.raises(GazeboCommandProgressError) as raised:
        context['progress'].advance(
            context['confirmation'], initial.next_intent_id
        )
    assert raised.value.code == 'gazebo_command_progress_gateway_unavailable'
    first_request_id = calls[0][0]
    context['store'].close()

    reopened = SQLiteConversationStore(
        str(context['database']),
        clock=context['wall'],
        simulation_execution_verifier=simulation_tests._TEST_TRUST,
        gazebo_execution_policy=context['policy'],
    )
    runner = GazeboMonitorRoomCommandRunner(
        reopened,
        _client(tmp_path),
        user_id='simulation-user',
    )
    progress = GazeboCommandProgressService(runner, timeout_seconds=1.0)
    _script_exchange(monkeypatch, ['navigating'], calls)
    result = progress.advance(context['confirmation'], initial.next_intent_id)
    assert result.drive_steps == 1
    assert calls[1][0] == first_request_id
    reopened.close()


def test_cancel_wins_race_and_late_drive_cannot_overwrite_terminal(
    tmp_path, monkeypatch
):
    context = _activate_and_prepare(
        tmp_path, monkeypatch, suffix='cancel-race'
    )
    initial = context['progress'].status(context['confirmation'])
    drive_started = threading.Event()
    release_drive = threading.Event()
    calls = []

    def exchange(
        _client_value,
        *,
        request_id,
        operation_id,
        command,
        timeout_seconds,
    ):
        calls.append((request_id, operation_id, command))
        if command == 'drive':
            drive_started.set()
            assert release_drive.wait(timeout=5.0)
            return _result(
                request_id, operation_id, command, 'succeeded'
            )
        return _result(request_id, operation_id, command, 'canceled')

    monkeypatch.setattr(
        GazeboMonitorRoomGatewayClient, 'exchange', exchange
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        drive = pool.submit(
            context['progress'].advance,
            context['confirmation'],
            initial.next_intent_id,
        )
        assert drive_started.wait(timeout=5.0)
        canceled = context['progress'].cancel(
            context['confirmation'], initial.cancel_intent_id
        )
        assert canceled.state == 'canceled'
        release_drive.set()
        late = drive.result(timeout=5.0)
    assert late.state == 'canceling'
    final = context['progress'].status(context['confirmation'])
    assert final.state == 'canceled'
    assert final.drive_steps == 0
    assert final.cancel_steps == 1
    assert [call[2] for call in calls] == ['drive', 'cancel']
    context['store'].close()


def test_delete_forbids_drive_but_deletion_safe_anchor_allows_cancel(
    tmp_path, monkeypatch
):
    context = _activate_and_prepare(
        tmp_path, monkeypatch, suffix='delete-cancel'
    )
    initial = context['progress'].status(context['confirmation'])
    scenario = context['scenario']
    assert context['store'].delete(
        scenario.draft.user_id, scenario.draft.conversation_id
    )
    calls = []
    _script_exchange(monkeypatch, ['canceled'], calls)
    with pytest.raises(GazeboCommandProgressError):
        context['progress'].advance(
            context['confirmation'], initial.next_intent_id
        )
    assert calls == []
    result = context['progress'].cancel(
        context['confirmation'], initial.cancel_intent_id
    )
    assert result.state == 'canceled'
    assert len(calls) == 1
    assert context['progress'].status(
        context['confirmation']
    ).state == 'canceled'
    context['store'].close()


def test_unknown_results_rotate_into_bounded_cancellation_recovery(
    tmp_path, monkeypatch
):
    context = _activate_and_prepare(
        tmp_path, monkeypatch, suffix='unknown-recovery'
    )
    calls = []
    _script_exchange(
        monkeypatch,
        ['delivery_unknown', 'cancel_unknown', 'canceled'],
        calls,
    )
    initial = context['progress'].status(context['confirmation'])
    unknown = context['progress'].advance(
        context['confirmation'], initial.next_intent_id
    )
    assert unknown.state == 'cancel_required'
    first_cancel = context['progress'].cancel(
        context['confirmation'], unknown.cancel_intent_id
    )
    assert first_cancel.state == 'cancel_required'
    assert first_cancel.cancel_intent_id != unknown.cancel_intent_id
    terminal = context['progress'].cancel(
        context['confirmation'], first_cancel.cancel_intent_id
    )
    assert terminal.state == 'canceled'
    assert len({call[0] for call in calls}) == 3
    context['store'].close()


def test_preactivation_prepared_source_can_never_be_promoted(
    tmp_path, monkeypatch
):
    database = tmp_path / 'preactivation.sqlite3'
    store, wall, _boot, _target, _semantic, _robot, _policy = (
        outbox_tests._configured_store(database, monkeypatch)
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix='preactivation'
    )
    store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    claim = store.claim_gazebo_execution(
        'gazebo-progress-claim-preactivation', lease_seconds=30
    )
    store.acknowledge_gazebo_execution(
        outbox_id=claim.outbox_id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        prepare_fingerprint=_DIGEST,
    )
    runner = GazeboMonitorRoomCommandRunner(
        store, _client(tmp_path), user_id='simulation-user'
    )
    progress = GazeboCommandProgressService(runner, timeout_seconds=1.0)
    with pytest.raises(GazeboCommandProgressError) as raised:
        progress.status(scenario.draft.confirmation_request_id)
    assert raised.value.code == 'gazebo_command_progress_preactivation_denied'
    sentinel = store._connection.execute(
        '''
        SELECT 1 FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (GAZEBO_COMMAND_PROGRESS_ACTIVATION_SENTINEL,),
    ).fetchone()
    assert sentinel is not None
    store.close()


def test_terminal_anchor_is_stable_and_no_new_intent_is_issued(
    tmp_path, monkeypatch
):
    context = _activate_and_prepare(
        tmp_path, monkeypatch, suffix='terminal-anchor'
    )
    calls = []
    _script_exchange(monkeypatch, ['succeeded'], calls)
    initial = context['progress'].status(context['confirmation'])
    terminal = context['progress'].advance(
        context['confirmation'], initial.next_intent_id
    )
    assert terminal.terminal is True
    assert terminal.next_intent_id is None
    assert terminal.cancel_intent_id is None
    first = context['progress'].get_terminal_anchor(
        context['confirmation']
    )
    second = context['progress'].get_terminal_anchor(
        context['confirmation']
    )
    assert first == second
    assert first.record_digest not in repr(first)
    assert context['progress'].advance(
        context['confirmation'], initial.next_intent_id
    ).terminal is True
    assert len(calls) == 1
    context['store'].close()
