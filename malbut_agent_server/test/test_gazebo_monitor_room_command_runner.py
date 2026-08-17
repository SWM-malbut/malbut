"""Tests for the explicit Agent-side Gazebo command runner."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import threading

import pytest

import malbut_agent_server.gazebo_monitor_room_command_runner as runner_module
import test_gazebo_execution_outbox as outbox_tests
import test_monitor_room_simulation_execution as simulation_tests
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gazebo_monitor_room_command_runner import (
    GAZEBO_COMMAND_RUNNER_MAX_STEPS,
    GazeboMonitorRoomCommandRunner,
    GazeboMonitorRoomCommandRunnerError,
)
from malbut_agent_server.gazebo_monitor_room_gateway_client import (
    GazeboMonitorRoomGatewayClient,
    GazeboMonitorRoomGatewayResult,
)
from malbut_agent_server.gazebo_prepare_dispatcher import (
    GazeboPrepareDispatchResult,
)
from malbut_gazebo.gazebo_monitor_room_gateway import (
    GATEWAY_REPLAY_REQUEST_LIMIT,
    GATEWAY_REPLAY_SCHEMA_VERSION,
    GazeboMonitorRoomGatewayError,
    GazeboMonitorRoomGatewayProcessor,
    GazeboMonitorRoomGatewayReplayStore,
    GazeboMonitorRoomGatewayServer,
)
from malbut_gazebo.gazebo_monitor_room_gateway_contract import (
    GazeboMonitorRoomGatewayRequest,
    GazeboMonitorRoomGatewayResponse,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    GazeboMonitorRoomNav2Controller,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    GazeboMonitorRoomStore,
    OrderedSemanticSample,
    PrepareOperation,
)


_DIGEST = hashlib.sha256(b'command-runner-test').hexdigest()
_OTHER_DIGEST = hashlib.sha256(b'command-runner-port').hexdigest()


class _Clock:
    def __init__(self, value=2.0):
        self.value = value

    def __call__(self):
        return self.value


class _Port:
    """Deterministic fake of the already-tested narrow Nav2 port."""

    def __init__(self, *, observe_status='succeeded'):
        self.observe_status = observe_status
        self.preflights = []
        self.starts = []
        self.observes = []
        self.cancels = []

    def preflight(self, request):
        self.preflights.append(request)
        return {
            'operation_id': request.operation_id,
            'goal_uuid': request.goal_uuid,
            'binding_digest': request.binding_digest,
            'request_fingerprint': request.request_fingerprint,
            'outcome': 'ready',
            'code': 'preflight_ready',
            'evidence_digest': _DIGEST,
        }

    def ensure_started(self, request):
        self.starts.append(request)
        return {
            'operation_id': request.preflight.operation_id,
            'goal_uuid': request.preflight.goal_uuid,
            'binding_digest': request.preflight.binding_digest,
            'fence_epoch': request.fence_epoch,
            'status': 'accepted',
            'evidence_digest': _OTHER_DIGEST,
        }

    def observe_goal(self, request):
        self.observes.append(request)
        return {
            'operation_id': request.operation_id,
            'goal_uuid': request.goal_uuid,
            'binding_digest': request.binding_digest,
            'fence_epoch': request.fence_epoch,
            'status': self.observe_status,
            'evidence_digest': _OTHER_DIGEST,
        }

    def cancel_goal(self, request):
        self.cancels.append(request)
        return {
            'operation_id': request.operation_id,
            'goal_uuid': request.goal_uuid,
            'binding_digest': request.binding_digest,
            'fence_epoch': request.fence_epoch,
            'status': 'canceled',
            'evidence_digest': _OTHER_DIGEST,
        }


def _prepared():
    """Construct the old public DTO to prove it grants no authority."""
    return GazeboPrepareDispatchResult(
        outbox_id='gazebo-execution-outbox-command-runner',
        operation_id='gazebo-operation-command-runner',
        claim_fence=1,
        prepare_replayed=False,
    )


def _durably_prepared(
    tmp_path,
    monkeypatch,
    *,
    suffix,
    acknowledge=True,
):
    """Commit a real approval, outbox claim, and exact durable prepare ACK."""
    database = tmp_path / f'agent-{suffix}.sqlite3'
    store, wall, boot, target, _semantic, _robot, policy = (
        outbox_tests._configured_store(database, monkeypatch)
    )
    scenario = simulation_tests._scenario(store, wall, suffix=suffix)
    consumed = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert consumed.enqueue is not None
    claim = store.claim_gazebo_execution(
        f'gazebo-claim-{suffix}',
        lease_seconds=30,
    )
    assert claim is not None
    acknowledgement = None
    if acknowledge:
        acknowledgement = store.acknowledge_gazebo_execution(
            outbox_id=claim.outbox_id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            prepare_fingerprint=_DIGEST,
        )
    return {
        'store': store,
        'database': database,
        'wall': wall,
        'boot': boot,
        'target': target,
        'policy': policy,
        'scenario': scenario,
        'confirmation_request_id': scenario.draft.confirmation_request_id,
        'enqueue': consumed.enqueue,
        'claim': claim,
        'acknowledgement': acknowledgement,
    }


def _reopen_authority(context):
    return SQLiteConversationStore(
        str(context['database']),
        clock=context['wall'],
        simulation_execution_verifier=simulation_tests._TEST_TRUST,
        gazebo_execution_policy=context['policy'],
    )


def _runner(context, path):
    return GazeboMonitorRoomCommandRunner(
        context['store'],
        _client(path),
        user_id=context['scenario'].draft.user_id,
    )


def _prepare_operation(operation_id, samples=2):
    return PrepareOperation(
        prepare_request_id='gazebo-prepare-command-runner',
        operation_id=operation_id,
        robot_id='robot-command-runner',
        map_id='map-command-runner',
        map_revision='revision-command-runner',
        semantic_revision='semantic-command-runner',
        zones_digest=_DIGEST,
        target_binding_digest=_DIGEST,
        effects_digest=_DIGEST,
        profile_digest=_DIGEST,
        plan_digest=_DIGEST,
        ordered_semantic_samples=tuple(
            OrderedSemanticSample(
                index,
                0,
                index,
                1000 + index,
                2000 + index,
            )
            for index in range(samples)
        ),
        deadline=100.0,
    )


def _composition(
    tmp_path,
    operation_id,
    *,
    samples=2,
    observe_status='succeeded',
):
    clock = _Clock()
    store = GazeboMonitorRoomStore(tmp_path / 'operations.sqlite3')
    store.prepare(_prepare_operation(operation_id, samples), now=1.0)
    port = _Port(observe_status=observe_status)
    controller = GazeboMonitorRoomNav2Controller(
        store,
        port,
        worker_id='command-runner-worker',
        lease_seconds=20.0,
        clock=clock,
    )
    replay = GazeboMonitorRoomGatewayReplayStore(
        tmp_path / 'gateway-replay.sqlite3',
        core_store_namespace=store.store_namespace,
        clock=clock,
    )
    processor = GazeboMonitorRoomGatewayProcessor(
        store,
        controller,
        replay,
    )
    return clock, store, port, controller, replay, processor


def _client(path):
    return GazeboMonitorRoomGatewayClient(
        str(path),
        expected_server_uid=os.geteuid(),
        timeout_seconds=1.0,
    )


def _recv_exact(connection, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise AssertionError('test frame was truncated')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _scripted_socket(path, handlers):
    """Serve a fixed number of protected framed test exchanges."""
    ready = threading.Event()
    failures = []
    requests = []

    def serve():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                os.chmod(path, 0o600)
                listener.listen(len(handlers))
                ready.set()
                for handler in handlers:
                    connection, _address = listener.accept()
                    with connection:
                        size = struct.unpack(
                            '!I', _recv_exact(connection, 4)
                        )[0]
                        payload = _recv_exact(connection, size)
                        assert connection.recv(1) == b''
                        request = (
                            GazeboMonitorRoomGatewayRequest.from_wire_bytes(
                                payload
                            )
                        )
                        requests.append(request)
                        response = handler(request, payload)
                        if response is not None:
                            connection.sendall(
                                struct.pack('!I', len(response)) + response
                            )
                            connection.shutdown(socket.SHUT_WR)
        except Exception as error:  # pragma: no cover - helper assertion
            failures.append(error)
            ready.set()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2.0)
    return thread, failures, requests


def _finish(thread, failures):
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert failures == []


def _response(request, *, state='prepared'):
    terminal = state in {
        'delivery_unknown',
        'cancel_unknown',
        'succeeded',
        'failed',
        'canceled',
    }
    succeeded = state == 'succeeded'
    return GazeboMonitorRoomGatewayResponse(
        request_id=request.request_id,
        operation_id=request.operation_id,
        command=request.command,
        state=state,
        current_sample_index=0,
        navigation_samples_total=1,
        navigation_samples_reached=1 if succeeded else 0,
        terminal=terminal,
        robot_blocked=(not terminal or state in {
            'delivery_unknown',
            'cancel_unknown',
        }),
        terminal_code=(
            'all_navigation_samples_reached'
            if succeeded
            else ('test_terminal' if terminal else None)
        ),
        evidence_digest=_DIGEST,
    ).to_wire_bytes()


def test_execution_api_accepts_no_caller_authority_fields_and_rejects_forgery(
    tmp_path,
    monkeypatch,
):
    """Only a confirmation ID may select durable server-owned authority."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='selector-boundary'
    )
    socket_path = tmp_path / 'must-not-open.sock'
    runner = _runner(context, socket_path)
    parameters = inspect.signature(
        GazeboMonitorRoomCommandRunner.drive_once
    ).parameters

    assert not {
        'prepared',
        'outbox_id',
        'operation_id',
        'claim_fence',
        'owner_binding_digest',
    } & set(parameters)
    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as missing:
        runner.drive_once(
            'simulation-confirmation-does-not-exist',
            'forged-selector-run',
        )
    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as dto:
        runner.drive_once(_prepared(), 'forged-dto-run')
    cross_user = GazeboMonitorRoomCommandRunner(
        context['store'],
        _client(socket_path),
        user_id='different-authenticated-user',
    )
    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as owner:
        cross_user.drive_once(
            context['confirmation_request_id'],
            'cross-user-selector-run',
        )

    assert missing.value.code == 'gazebo_command_runner_prepared_invalid'
    assert dto.value.code == 'gazebo_command_runner_request_invalid'
    assert owner.value.code == 'gazebo_command_runner_prepared_invalid'
    assert not socket_path.exists()
    context['store'].close()


def test_claim_without_durable_prepare_ack_cannot_reach_gateway(
    tmp_path,
    monkeypatch,
):
    """Claim and dispatcher DTO state alone never grants execution."""
    context = _durably_prepared(
        tmp_path,
        monkeypatch,
        suffix='claim-only',
        acknowledge=False,
    )
    socket_path = tmp_path / 'claim-only-must-not-open.sock'
    runner = _runner(context, socket_path)

    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as raised:
        runner.drive_once(
            context['confirmation_request_id'],
            'claim-only-run',
        )

    assert raised.value.code == 'gazebo_command_runner_prepared_invalid'
    assert context['acknowledgement'] is None
    assert not socket_path.exists()
    context['store'].close()


def test_owner_binding_is_rederived_from_confirmation_and_tamper_rejected(
    tmp_path,
    monkeypatch,
):
    """The resolver recomputes per-confirmation ownership server-side."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='owner-rederive'
    )
    store = context['store']
    confirmation = context['confirmation_request_id']
    authority = store.resolve_prepared_gazebo_execution(
        confirmation_request_id=confirmation,
        expected_user_id=context['scenario'].draft.user_id,
    )
    assert authority.execution_scope == 'observe'
    row = store._connection.execute(
        '''
        SELECT user_id, conversation_id, session_instance_id,
               generation, revision, ordinal
        FROM confirmation_intents
        WHERE confirmation_request_id = ?
        ''',
        (confirmation,),
    ).fetchone()
    expected_owner = hashlib.sha256(
        json.dumps(
            {
                'user_id': row['user_id'],
                'conversation_id': row['conversation_id'],
                'session_instance_id': row['session_instance_id'],
                'generation': int(row['generation']),
                'revision': int(row['revision']),
                'ordinal': int(row['ordinal']),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('ascii')
    ).hexdigest()
    assert authority.owner_binding_digest == expected_owner

    store._connection.execute(
        '''
        UPDATE confirmation_intents SET user_id = ?
        WHERE confirmation_request_id = ?
        ''',
        ('cross-owner-user', confirmation),
    )
    store._connection.commit()
    socket_path = tmp_path / 'cross-owner-must-not-open.sock'
    runner = _runner(context, socket_path)
    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as raised:
        runner.drive_once(confirmation, 'cross-owner-run')

    assert raised.value.code == 'gazebo_command_runner_prepared_invalid'
    assert not socket_path.exists()
    store.close()


def test_manual_steps_exact_replay_and_progress_on_unchanged_observation(
    tmp_path,
    monkeypatch,
):
    """The ordinal advances a chain even when response state is unchanged."""
    socket_path = tmp_path / 'manual.sock'
    handlers = [
        lambda request, _payload: _response(request),
        lambda request, _payload: _response(request),
        lambda request, _payload: _response(request),
    ]
    thread, failures, requests = _scripted_socket(socket_path, handlers)
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='manual-chain'
    )
    runner = _runner(context, socket_path)
    confirmation = context['confirmation_request_id']

    first = runner.drive_once(confirmation, 'explicit-manual-run')
    replay = runner.drive_once(confirmation, 'explicit-manual-run')
    second = runner.drive_once(
        confirmation,
        'explicit-manual-run',
        previous=first,
    )
    _finish(thread, failures)
    context['store'].close()

    assert first.request_id == replay.request_id
    assert first.gateway_response_fingerprint == (
        replay.gateway_response_fingerprint
    )
    assert second.request_id != first.request_id
    assert second.step_index == 1
    assert second.state == first.state == 'prepared'
    assert [request.request_id for request in requests] == [
        first.request_id,
        first.request_id,
        second.request_id,
    ]


def test_restart_reconstructs_chain_from_step_zero_and_reaches_terminal(
    tmp_path,
    monkeypatch,
):
    """A fresh runner replays old IDs before deriving the next request."""
    socket_path = tmp_path / 'restart.sock'
    cache = {}
    unique_states = ['preflighting', 'navigating', 'succeeded']

    def handle(request, _payload):
        if request.request_id not in cache:
            state = unique_states[len(cache)]
            cache[request.request_id] = _response(request, state=state)
        return cache[request.request_id]

    thread, failures, requests = _scripted_socket(
        socket_path,
        [handle, handle, handle, handle, handle],
    )
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='restart-chain'
    )
    confirmation = context['confirmation_request_id']
    first_runner = _runner(context, socket_path)
    partial = first_runner.drive_until_terminal(
        confirmation,
        'restart-safe-run',
        max_steps=2,
        timeout_seconds=5.0,
    )
    context['store'].close()
    context['store'] = _reopen_authority(context)
    restarted = _runner(context, socket_path)
    completed = restarted.drive_until_terminal(
        confirmation,
        'restart-safe-run',
        max_steps=3,
        timeout_seconds=5.0,
    )
    _finish(thread, failures)
    context['store'].close()

    ids = [request.request_id for request in requests]
    assert partial.stop_reason == 'step_limit'
    assert completed.stop_reason == 'terminal'
    assert completed.last_step.state == 'succeeded'
    assert ids[0:2] == ids[2:4]
    assert ids[4] not in set(ids[:4])
    assert len(cache) == 3


def test_real_gateway_drive_lost_response_never_resends_claimed_start(
    tmp_path,
    monkeypatch,
):
    """Retrying one ambiguous drive replays one real controller result."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='lost-start'
    )
    operation_id = context['enqueue'].operation_id
    _clock, store, port, _controller, replay, processor = _composition(
        tmp_path,
        operation_id,
        samples=1,
    )
    socket_path = tmp_path / 'lost-start.sock'

    def process(_request, payload):
        return processor.handle_wire_bytes(payload)

    def process_and_drop(_request, payload):
        processor.handle_wire_bytes(payload)
        return None

    thread, failures, requests = _scripted_socket(
        socket_path,
        [process, process_and_drop, process],
    )
    runner = _runner(context, socket_path)
    confirmation = context['confirmation_request_id']
    first = runner.drive_once(confirmation, 'lost-start-run')

    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as raised:
        runner.drive_once(
            confirmation,
            'lost-start-run',
            previous=first,
        )
    retry = runner.drive_once(
        confirmation,
        'lost-start-run',
        previous=first,
    )
    _finish(thread, failures)

    assert raised.value.code == 'gazebo_command_runner_gateway_unavailable'
    assert requests[1].request_id == requests[2].request_id
    assert retry.state == 'navigating'
    assert store.observe(operation_id).state == 'navigating'
    assert len(port.preflights) == 1
    assert len(port.starts) == 1
    assert len(port.observes) == 0
    replay.close()
    store.close()
    context['store'].close()


def test_real_gateway_rejects_durable_operation_mismatch_without_nav_start(
    tmp_path,
    monkeypatch,
):
    """A valid confirmation cannot drive a different prepared operation."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='operation-mismatch'
    )
    agent_operation = context['enqueue'].operation_id
    gazebo_operation = 'gazebo-operation-different-prepared-operation'
    assert agent_operation != gazebo_operation
    _clock, store, port, _controller, replay, processor = _composition(
        tmp_path,
        gazebo_operation,
        samples=1,
    )
    socket_path = tmp_path / 'operation-mismatch.sock'
    server = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()
    runner = _runner(context, socket_path)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            serving = pool.submit(server.serve_once)
            with pytest.raises(GazeboMonitorRoomCommandRunnerError) as raised:
                runner.drive_once(
                    context['confirmation_request_id'],
                    'operation-mismatch-run',
                )
            with pytest.raises(GazeboMonitorRoomGatewayError):
                serving.result(timeout=5.0)
    finally:
        server.close()
        replay.close()
        store.close()
        context['store'].close()

    assert raised.value.code == 'gazebo_command_runner_gateway_unavailable'
    assert port.preflights == []
    assert port.starts == []
    assert port.observes == []
    assert port.cancels == []


def test_expired_scope_blocks_drive_but_allows_exact_observe_and_cancel(
    tmp_path,
    monkeypatch,
):
    """Expiry cannot start work, but cannot strand an existing exact goal."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='expired-scopes'
    )
    operation_id = context['enqueue'].operation_id
    _clock, store, port, controller, replay, processor = _composition(
        tmp_path,
        operation_id,
        samples=1,
    )
    assert controller.drive_once(operation_id).state == 'preflighting'
    assert controller.drive_once(operation_id).state == 'navigating'
    starts_before_expiry = len(port.starts)
    preflights_before_expiry = len(port.preflights)
    context['boot'].now_ns = context['claim'].deadline_boottime_ns

    missing_path = tmp_path / 'expired-drive-must-not-open.sock'
    expired_runner = _runner(context, missing_path)
    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as drive:
        expired_runner.drive_once(
            context['confirmation_request_id'],
            'expired-drive-run',
        )
    wrong_user = GazeboMonitorRoomCommandRunner(
        context['store'],
        _client(missing_path),
        user_id='different-authenticated-user',
    )
    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as owner:
        wrong_user.cancel_once(
            context['confirmation_request_id'],
            'expired-cross-user-cancel',
        )
    assert drive.value.code == 'gazebo_command_runner_prepared_invalid'
    assert owner.value.code == 'gazebo_command_runner_prepared_invalid'
    assert not missing_path.exists()

    socket_path = tmp_path / 'expired-reconcile.sock'
    server = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()
    runner = _runner(context, socket_path)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            serving = pool.submit(
                lambda: [server.serve_once() for _index in range(2)]
            )
            observed = runner.observe_once(
                context['confirmation_request_id'],
                'expired-observe-run',
            )
            canceled = runner.cancel_once(
                context['confirmation_request_id'],
                'expired-cancel-run',
            )
            serving.result(timeout=5.0)
    finally:
        server.close()
        replay.close()
        store.close()
        context['store'].close()

    assert observed.state == 'navigating'
    assert canceled.state == 'canceled'
    assert len(port.preflights) == preflights_before_expiry
    assert len(port.starts) == starts_before_expiry
    assert port.observes == []
    assert len(port.cancels) == 1


def test_expired_cancel_flow_can_observe_then_drive_only_to_reconcile(
    tmp_path,
    monkeypatch,
):
    """The cancel scope keeps its chain while using closed reconcile verbs."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='expired-cancel-reconcile'
    )
    context['boot'].now_ns = context['claim'].deadline_boottime_ns
    socket_path = tmp_path / 'expired-cancel-reconcile.sock'
    handlers = [
        lambda request, _payload: _response(
            request, state='cancel_requested'
        ),
        lambda request, _payload: _response(request, state='canceled'),
    ]
    thread, failures, requests = _scripted_socket(socket_path, handlers)
    runner = _runner(context, socket_path)

    result = runner.cancel_until_terminal(
        context['confirmation_request_id'],
        'expired-cancel-reconcile-run',
        max_steps=2,
        timeout_seconds=5.0,
    )
    _finish(thread, failures)
    context['store'].close()

    assert result.stop_reason == 'terminal'
    assert [request.command for request in requests] == ['cancel', 'drive']
    assert result.last_step.state == 'canceled'


def test_cancel_claim_crash_observes_before_a_fresh_cancel(
    tmp_path,
    monkeypatch,
):
    """A claim-only cancel is reconciled before a new cancel ID is sent."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='cancel-reconcile'
    )
    operation_id = context['enqueue'].operation_id
    _clock, store, port, controller, replay, processor = _composition(
        tmp_path,
        operation_id,
        samples=1,
    )
    assert controller.drive_once(operation_id).state == 'preflighting'
    assert controller.drive_once(operation_id).state == 'navigating'
    socket_path = tmp_path / 'cancel-reconcile.sock'

    def claim_and_drop(request, _payload):
        assert replay.claim(request).first is True
        return None

    def process(_request, payload):
        return processor.handle_wire_bytes(payload)

    thread, failures, requests = _scripted_socket(
        socket_path,
        [claim_and_drop, process, process, process],
    )
    runner = _runner(context, socket_path)
    confirmation = context['confirmation_request_id']

    with pytest.raises(GazeboMonitorRoomCommandRunnerError):
        runner.cancel_once(confirmation, 'cancel-reconcile-run')
    recovered = runner.cancel_once(
        confirmation,
        'cancel-reconcile-run',
    )
    observed = runner.cancel_once(
        confirmation,
        'cancel-reconcile-run',
        previous=recovered,
    )
    terminal = runner.cancel_once(
        confirmation,
        'cancel-reconcile-run',
        previous=observed,
    )
    _finish(thread, failures)

    assert [request.command for request in requests] == [
        'cancel',
        'cancel',
        'observe',
        'cancel',
    ]
    assert requests[0].request_id == requests[1].request_id
    assert requests[3].request_id != requests[1].request_id
    assert recovered.state == observed.state == 'navigating'
    assert terminal.state == 'canceled'
    assert len(port.cancels) == 1
    replay.close()
    store.close()
    context['store'].close()


def test_real_protected_gateway_completes_two_samples_in_five_steps(
    tmp_path,
    monkeypatch,
):
    """The foreground runner interoperates with the real UDS gateway."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='real-protected'
    )
    operation_id = context['enqueue'].operation_id
    _clock, store, port, _controller, replay, processor = _composition(
        tmp_path,
        operation_id,
        samples=2,
    )
    socket_path = tmp_path / 'real-gateway.sock'
    server = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()
    runner = _runner(context, socket_path)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            serving = pool.submit(
                lambda: [server.serve_once() for _index in range(5)]
            )
            result = runner.drive_until_terminal(
                context['confirmation_request_id'],
                'real-two-sample-run',
                max_steps=5,
                timeout_seconds=5.0,
            )
            serving.result(timeout=10.0)
    finally:
        server.close()
        replay.close()
        store.close()
        context['store'].close()

    assert result.stop_reason == 'terminal'
    assert result.requests_made == 5
    assert result.last_step.state == 'succeeded'
    assert result.last_step.navigation_samples_reached == 2
    assert len(port.preflights) == 2
    assert len(port.starts) == 2
    assert len(port.observes) == 2
    public = result.to_public_dict()
    rendered = repr(result)
    assert public['simulation'] is True
    assert public['physical_authorized'] is False
    assert public['physical_effects'] is False
    assert public['viewer_live'] is False
    assert public['camera_coverage_validated'] is False
    assert public['coverage_achieved'] is False
    for private in (
        operation_id,
        context['enqueue'].outbox_id,
        'real-two-sample-run',
        _DIGEST,
        'request_id',
        'evidence_digest',
    ):
        assert private not in str(public)
        assert private not in rendered


def test_runner_and_results_reject_instance_and_frozen_bypass_mutation(
    tmp_path,
    monkeypatch,
):
    """Client, runner, prepared proof, and cursors remain sealed."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='sealed-runner'
    )
    missing = tmp_path / 'missing.sock'
    client = _client(missing)
    runner = GazeboMonitorRoomCommandRunner(
        context['store'],
        client,
        user_id=context['scenario'].draft.user_id,
    )
    calls = []
    object.__setattr__(
        runner,
        '_invoke_locked',
        lambda **_values: calls.append('shadowed'),
    )
    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as raised:
        GazeboMonitorRoomCommandRunner.drive_once(
            runner,
            context['confirmation_request_id'],
            'sealed-run',
        )
    assert raised.value.code == (
        'gazebo_command_runner_configuration_changed'
    )
    assert calls == []
    object.__delattr__(runner, '_invoke_locked')

    socket_path = tmp_path / 'result.sock'
    thread, failures, _requests = _scripted_socket(
        socket_path,
        [lambda request, _payload: _response(request)],
    )
    step = GazeboMonitorRoomCommandRunner(
        context['store'],
        _client(socket_path),
        user_id=context['scenario'].draft.user_id,
    ).observe_once(context['confirmation_request_id'], 'result-run')
    _finish(thread, failures)
    with pytest.raises(FrozenInstanceError):
        step.physical_authorized = True
    object.__setattr__(step, 'physical_authorized', True)
    with pytest.raises(GazeboMonitorRoomCommandRunnerError):
        step.to_public_dict()
    context['store'].close()


def test_cursor_is_detached_before_concurrent_caller_mutation(
    tmp_path,
    monkeypatch,
):
    """A post-validation object.__setattr__ race cannot redirect the chain."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='cursor-detach'
    )
    confirmation = context['confirmation_request_id']
    first_path = tmp_path / 'cursor-first.sock'
    first_thread, first_failures, _first_requests = _scripted_socket(
        first_path,
        [lambda request, _payload: _response(request)],
    )
    first = _runner(context, first_path).drive_once(
        confirmation,
        'cursor-detach-run',
    )
    _finish(first_thread, first_failures)

    copied = threading.Event()
    release = threading.Event()
    original = GazeboMonitorRoomCommandRunner._canonical_previous

    def barrier(authority, run_request_id, flow, previous):
        canonical = original(
            authority,
            run_request_id,
            flow,
            previous,
        )
        copied.set()
        assert release.wait(timeout=2.0)
        return canonical

    monkeypatch.setattr(
        GazeboMonitorRoomCommandRunner,
        '_canonical_previous',
        staticmethod(barrier),
    )
    second_path = tmp_path / 'cursor-second.sock'
    second_thread, second_failures, _second_requests = _scripted_socket(
        second_path,
        [lambda request, _payload: _response(request)],
    )
    runner = _runner(context, second_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.drive_once,
            confirmation,
            'cursor-detach-run',
            previous=first,
        )
        assert copied.wait(timeout=2.0)
        object.__setattr__(first, 'step_index', 17)
        release.set()
        second = future.result(timeout=5.0)
    _finish(second_thread, second_failures)

    assert second.step_index == 1
    assert second.previous_request_id != second.request_id
    context['store'].close()


def test_gateway_result_is_detached_before_concurrent_transport_mutation(
    tmp_path,
    monkeypatch,
):
    """Only the fingerprinted response snapshot feeds the public cursor."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='gateway-detach'
    )
    copied = threading.Event()
    release = threading.Event()
    returned = []
    original_canonical = runner_module._canonical_gateway_result

    def exchange(
        _client_value,
        *,
        request_id,
        operation_id,
        command,
        timeout_seconds,
    ):
        assert timeout_seconds > 0.0
        result = GazeboMonitorRoomGatewayResult(
            request_id=request_id,
            operation_id=operation_id,
            command=command,
            state='prepared',
            current_sample_index=0,
            navigation_samples_total=1,
            navigation_samples_reached=0,
            terminal=False,
            robot_blocked=True,
            terminal_code=None,
            evidence_digest=_DIGEST,
        )
        returned.append(result)
        return result

    def canonical_barrier(value):
        canonical = original_canonical(value)
        copied.set()
        assert release.wait(timeout=2.0)
        return canonical

    monkeypatch.setattr(
        GazeboMonitorRoomGatewayClient,
        'exchange',
        exchange,
    )
    monkeypatch.setattr(
        runner_module,
        '_canonical_gateway_result',
        canonical_barrier,
    )
    runner = _runner(context, tmp_path / 'unused.sock')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.drive_once,
            context['confirmation_request_id'],
            'gateway-detach-run',
        )
        assert copied.wait(timeout=2.0)
        object.__setattr__(returned[0], 'state', 'succeeded')
        release.set()
        step = future.result(timeout=5.0)

    assert step.state == 'prepared'
    assert returned[0].state == 'succeeded'
    context['store'].close()


@pytest.mark.parametrize(
    ('max_steps', 'timeout_seconds', 'expected'),
    (
        (0, 1.0, 'gazebo_command_runner_step_limit_invalid'),
        (GAZEBO_COMMAND_RUNNER_MAX_STEPS + 1, 1.0,
         'gazebo_command_runner_step_limit_invalid'),
        (1, True, 'gazebo_command_runner_deadline_invalid'),
        (1, 0.0, 'gazebo_command_runner_deadline_invalid'),
        (1, float('nan'), 'gazebo_command_runner_deadline_invalid'),
    ),
)
def test_run_bounds_fail_before_socket_access(
    tmp_path,
    monkeypatch,
    max_steps,
    timeout_seconds,
    expected,
):
    """Every foreground loop has strict caller and construction bounds."""
    context = _durably_prepared(
        tmp_path, monkeypatch, suffix='bounded-run'
    )
    runner = _runner(context, tmp_path / 'missing')
    with pytest.raises(GazeboMonitorRoomCommandRunnerError) as raised:
        runner.drive_until_terminal(
            context['confirmation_request_id'],
            'bounded-run',
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
        )
    assert raised.value.code == expected
    context['store'].close()


def test_replay_capacity_matches_full_4096_sample_command_chain(tmp_path):
    """Schema v2 can retain more than the 8,193-command happy path."""
    _clock, store, _port, _controller, replay, _processor = _composition(
        tmp_path,
        'gazebo-operation-replay-capacity',
        samples=1,
    )
    connection = sqlite3.connect(tmp_path / 'gateway-replay.sqlite3')
    metadata = connection.execute(
        '''
        SELECT schema_version, request_limit
        FROM gazebo_monitor_room_gateway_metadata
        '''
    ).fetchone()
    connection.close()
    replay.close()
    store.close()

    full_happy_path = 1 + (2 * 4096)
    assert GATEWAY_REPLAY_SCHEMA_VERSION == 2
    assert GATEWAY_REPLAY_REQUEST_LIMIT == 65536
    assert metadata == (2, 65536)
    assert full_happy_path < GATEWAY_REPLAY_REQUEST_LIMIT
    assert GAZEBO_COMMAND_RUNNER_MAX_STEPS >= full_happy_path


def test_production_module_has_no_ros_gazebo_or_background_imports():
    """The Agent runner remains manual and package-dependency clean."""
    source = Path(runner_module.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    imports = []
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or '')
        elif isinstance(node, ast.Call):
            calls.append(ast.unparse(node.func))
    assert not any(name.startswith('malbut_gazebo') for name in imports)
    assert not any(name.startswith('rclpy') for name in imports)
    assert not any(
        'create_task' in call
        or call.endswith('.start')
        or 'Thread' in call
        for call in calls
    )
