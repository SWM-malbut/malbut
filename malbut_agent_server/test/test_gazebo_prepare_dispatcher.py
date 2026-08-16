"""Tests for the durable Agent-to-Gazebo prepare handoff."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import socket
import struct

import pytest

import malbut_agent_server.gazebo_prepare_dispatcher as dispatcher_module
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboExecutionClaim,
    GazeboExecutionSample,
)
from malbut_agent_server.gazebo_prepare_dispatcher import (
    GazeboPrepareClient,
    GazeboPrepareDispatcher,
    GazeboPrepareDispatcherError,
)
from malbut_gazebo.gazebo_monitor_room_prepare_gateway import (
    GazeboMonitorRoomPrepareProcessor,
    GazeboMonitorRoomPrepareServer,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    GazeboMonitorRoomStore,
)
import test_gazebo_execution_outbox as outbox_tests
import test_monitor_room_simulation_execution as simulation_tests
from test_robot_state import _BOOT_ID, _NOW_NS


_DIGEST = hashlib.sha256(b'agent-prepare-dispatcher').hexdigest()


def _claim(**changes):
    values = {
        'outbox_id': 'gazebo-execution-outbox-client-1',
        'operation_id': 'gazebo-operation-client-1',
        'prepare_request_id': 'gazebo-prepare-client-1',
        'claim_request_id': 'gazebo-dispatch-client-1',
        'claim_token': 'A' * 43,
        'claim_fence': 1,
        'attempt_number': 1,
        'robot_id': 'robot-client-1',
        'map_id': 'map-client-1',
        'map_revision': 'map-revision-client-1',
        'semantic_revision': 'semantic-revision-client-1',
        'zones_digest': _DIGEST,
        'target_binding_digest': _DIGEST,
        'effects_digest': _DIGEST,
        'profile_digest': _DIGEST,
        'plan_digest': _DIGEST,
        'host_boot_id': _BOOT_ID,
        'ordered_semantic_samples': (
            GazeboExecutionSample(
                index=0,
                polygon_ordinal=0,
                row_ordinal=0,
                x_mm=1000,
                y_mm=-500,
            ),
        ),
        'deadline_boottime_ns': _NOW_NS + 1_000_000_000,
        'claimed_boottime_ns': _NOW_NS,
        'lease_expires_boottime_ns': _NOW_NS + 500_000_000,
    }
    values.update(changes)
    return GazeboExecutionClaim(**values)


def _conversation_pending(tmp_path, monkeypatch, *, suffix):
    database = tmp_path / f'agent-{suffix}.sqlite3'
    store, wall, boot, target, _semantic, _robot, _policy = (
        outbox_tests._configured_store(database, monkeypatch)
    )
    scenario = simulation_tests._scenario(
        store, wall, suffix=f'prepare-{suffix}'
    )
    consumed = store.consume_approved_monitor_room_gazebo_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert consumed.enqueue is not None
    return store, boot, target, consumed.enqueue


def _real_endpoint(tmp_path, target, boot, *, suffix):
    operations = GazeboMonitorRoomStore(
        tmp_path / f'gazebo-{suffix}.sqlite3',
        boot_id_reader=lambda: _BOOT_ID,
    )
    processor = GazeboMonitorRoomPrepareProcessor(
        operations,
        expected_robot_id=target.device_id,
        local_boot_id=_BOOT_ID,
        clock=lambda: boot.now_ns / 1_000_000_000,
    )
    socket_path = tmp_path / f'prepare-{suffix}.sock'
    server = GazeboMonitorRoomPrepareServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()
    client = GazeboPrepareClient(
        str(socket_path),
        expected_gazebo_uid=os.geteuid(),
    )
    return operations, server, client


def _serve_once(server, call):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(server.serve_once)
        result = call()
        future.result(timeout=5)
        return result


def _operation_count(store):
    return store._connection.execute(
        'SELECT COUNT(*) FROM gazebo_monitor_room_operations'
    ).fetchone()[0]


def test_real_prepare_server_interop_dispatches_and_acks_once(
    tmp_path,
    monkeypatch,
) -> None:
    """The duplicated client contract interoperates with the real intake."""
    conversation, boot, target, enqueue = _conversation_pending(
        tmp_path, monkeypatch, suffix='interop'
    )
    operations, server, client = _real_endpoint(
        tmp_path, target, boot, suffix='interop'
    )
    dispatcher = GazeboPrepareDispatcher(conversation, client)

    result = _serve_once(
        server,
        lambda: dispatcher.dispatch_once('gazebo-dispatch-interop'),
    )

    assert result is not None
    assert result.outbox_id == enqueue.outbox_id
    assert result.operation_id == enqueue.operation_id
    assert result.state == 'prepared'
    assert result.prepare_replayed is False
    assert result.simulation is True
    assert result.physical_authorized is False
    assert result.physical_effects is False
    assert result.viewer_live is False
    assert result.camera_coverage_validated is False
    assert result.coverage_achieved is False
    assert _operation_count(operations) == 1
    assert conversation.claim_gazebo_execution(
        'gazebo-dispatch-after-prepared'
    ) is None
    rendered = repr(result)
    public = result.to_public_dict()
    for private in (
        _BOOT_ID,
        target.device_id,
        target.map_id,
        'claim_token',
        'prepare_fingerprint',
        'x_mm',
        'y_mm',
    ):
        assert private not in rendered
        assert private not in str(public)

    with pytest.raises(FrozenInstanceError):
        result.simulation = False
    object.__setattr__(result, 'simulation', False)
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        result.to_public_dict()
    assert caught.value.code == 'gazebo_prepare_result_invalid'

    object.__setattr__(
        dispatcher,
        '_dispatch_once_impl',
        lambda _request: result,
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        dispatcher.dispatch_once('gazebo-dispatch-forged-method')
    assert caught.value.code == 'gazebo_prepare_configuration_changed'
    object.__delattr__(dispatcher, '_dispatch_once_impl')
    object.__setattr__(dispatcher, '_lease_seconds', 31)
    object.__setattr__(
        dispatcher,
        '_configuration_seal',
        (conversation, client, 31, dispatcher._dispatch_lock),
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        dispatcher.dispatch_once('gazebo-dispatch-forged-seal')
    assert caught.value.code == 'gazebo_prepare_configuration_changed'
    server.close()
    operations.close()
    conversation.close()


def test_claim_commits_before_wire_and_exact_retry_survives_lost_response(
    tmp_path,
    monkeypatch,
) -> None:
    """A response loss reuses one committed claim and one prepare row."""
    conversation, boot, target, enqueue = _conversation_pending(
        tmp_path, monkeypatch, suffix='lost-response'
    )
    operations, server, client = _real_endpoint(
        tmp_path, target, boot, suffix='lost-response'
    )
    dispatcher = GazeboPrepareDispatcher(conversation, client)
    original_prepare = GazeboPrepareClient.prepare
    observed = {}
    target_binding = {
        'expected_outbox_id': enqueue.outbox_id,
        'expected_operation_id': enqueue.operation_id,
        'expected_confirmation_request_id': (
            'simulation-confirmation-prepare-lost-response'
        ),
    }

    def lose_after_response(current, claim):
        row = conversation._connection.execute(
            '''
            SELECT state, current_claim_request_id
            FROM monitor_room_gazebo_execution_outbox
            WHERE outbox_id = ?
            ''',
            (claim.outbox_id,),
        ).fetchone()
        observed['state'] = row['state']
        observed['request'] = row['current_claim_request_id']
        original_prepare(current, claim)
        raise OSError('private response loss detail')

    monkeypatch.setattr(
        GazeboPrepareClient, 'prepare', lose_after_response
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        _serve_once(
            server,
            lambda: dispatcher.dispatch_once(
                'gazebo-dispatch-lost-response',
                **target_binding,
            ),
        )
    assert caught.value.code == 'gazebo_prepare_dispatch_unavailable'
    assert str(caught.value) == (
        'Gazebo preparation handoff is unavailable'
    )
    assert 'private' not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert observed == {
        'state': 'claimed',
        'request': 'gazebo-dispatch-lost-response',
    }
    assert _operation_count(operations) == 1

    monkeypatch.setattr(
        GazeboPrepareClient, 'prepare', original_prepare
    )
    conversation.close()
    reopened, _wall, _boot, _target, _semantic, _robot, _policy = (
        outbox_tests._configured_store(
            tmp_path / 'agent-lost-response.sqlite3',
            monkeypatch,
        )
    )
    dispatcher = GazeboPrepareDispatcher(reopened, client)
    result = _serve_once(
        server,
        lambda: dispatcher.dispatch_once(
            'gazebo-dispatch-lost-response',
            **target_binding,
        ),
    )
    assert result is not None
    assert result.prepare_replayed is True
    assert _operation_count(operations) == 1
    assert reopened._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_gazebo_execution_claims'
    ).fetchone()[0] == 1
    server.close()
    operations.close()
    reopened.close()


def test_ack_failure_retries_same_prepare_without_duplicate(
    tmp_path,
    monkeypatch,
) -> None:
    """A pre-commit ACK failure leaves the exact prepare retryable."""
    conversation, boot, target, _enqueue = _conversation_pending(
        tmp_path, monkeypatch, suffix='ack-retry'
    )
    operations, server, client = _real_endpoint(
        tmp_path, target, boot, suffix='ack-retry'
    )
    dispatcher = GazeboPrepareDispatcher(conversation, client)
    store_type = type(conversation)
    original_ack = store_type.acknowledge_gazebo_execution

    def reject_ack(_store, **_values):
        raise OSError('private ack failure detail')

    monkeypatch.setattr(
        store_type, 'acknowledge_gazebo_execution', reject_ack
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        _serve_once(
            server,
            lambda: dispatcher.dispatch_once(
                'gazebo-dispatch-ack-retry'
            ),
        )
    assert caught.value.code == 'gazebo_prepare_ack_unavailable'
    assert _operation_count(operations) == 1

    monkeypatch.setattr(
        store_type, 'acknowledge_gazebo_execution', original_ack
    )
    result = _serve_once(
        server,
        lambda: dispatcher.dispatch_once(
            'gazebo-dispatch-ack-retry'
        ),
    )
    assert result is not None
    assert result.prepare_replayed is True
    assert _operation_count(operations) == 1
    server.close()
    operations.close()
    conversation.close()


def test_ack_response_loss_is_terminal_and_never_resends_prepare(
    tmp_path,
    monkeypatch,
) -> None:
    """A committed ACK remains terminal despite its caller losing return."""
    conversation, boot, target, _enqueue = _conversation_pending(
        tmp_path, monkeypatch, suffix='ack-loss'
    )
    operations, server, client = _real_endpoint(
        tmp_path, target, boot, suffix='ack-loss'
    )
    dispatcher = GazeboPrepareDispatcher(conversation, client)
    store_type = type(conversation)
    original_ack = store_type.acknowledge_gazebo_execution

    def lose_ack_response(current, **values):
        original_ack(current, **values)
        raise OSError('private committed ack response loss')

    monkeypatch.setattr(
        store_type,
        'acknowledge_gazebo_execution',
        lose_ack_response,
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        _serve_once(
            server,
            lambda: dispatcher.dispatch_once(
                'gazebo-dispatch-ack-loss'
            ),
        )
    assert caught.value.code == 'gazebo_prepare_ack_unavailable'
    monkeypatch.setattr(
        store_type, 'acknowledge_gazebo_execution', original_ack
    )
    assert dispatcher.dispatch_once('gazebo-dispatch-ack-loss') is None
    assert _operation_count(operations) == 1
    assert conversation._connection.execute(
        '''
        SELECT COUNT(*)
        FROM monitor_room_gazebo_execution_acknowledgements
        '''
    ).fetchone()[0] == 1
    server.close()
    operations.close()
    conversation.close()


def _recv_exact(connection, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise AssertionError('test request truncated')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _fake_response_server(path, mutate):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    listener.listen(1)

    def serve():
        connection, _address = listener.accept()
        try:
            size = struct.unpack('!I', _recv_exact(connection, 4))[0]
            request = json.loads(
                _recv_exact(connection, size).decode('ascii')
            )
            response = {
                'schema_version': 1,
                'request_id': request['request_id'],
                'outbox_id': request['outbox_id'],
                'operation_id': request['operation_id'],
                'state': 'prepared',
                'prepare_fingerprint': _DIGEST,
                'replayed': False,
                'runtime_mode': 'gazebo',
                'simulation': True,
                'physical_authorized': False,
                'physical_effects': False,
                'viewer_live': False,
                'camera_coverage_validated': False,
                'coverage_achieved': False,
            }
            payload = mutate(response)
            connection.sendall(struct.pack('!I', len(payload)) + payload)
        finally:
            connection.close()
            listener.close()

    return serve


@pytest.mark.parametrize(
    'mutate',
    (
        lambda value: json.dumps(
            {**value, 'physical_authorized': True},
            sort_keys=True,
            separators=(',', ':'),
        ).encode('ascii'),
        lambda value: json.dumps(
            {**value, 'private_extra': False},
            sort_keys=True,
            separators=(',', ':'),
        ).encode('ascii'),
        lambda value: b' ' + json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('ascii'),
        lambda value: json.dumps(
            {**value, 'request_id': 'different-request'},
            sort_keys=True,
            separators=(',', ':'),
        ).encode('ascii'),
    ),
)
def test_client_rejects_stronger_extra_noncanonical_and_mismatched_response(
    tmp_path,
    mutate,
) -> None:
    """Only the exact correlated false-claims response is accepted."""
    path = tmp_path / 'fake-prepare.sock'
    serve = _fake_response_server(path, mutate)
    client = GazeboPrepareClient(
        str(path), expected_gazebo_uid=os.geteuid()
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(serve)
        with pytest.raises(GazeboPrepareDispatcherError) as caught:
            client.prepare(_claim())
        future.result(timeout=5)
    assert caught.value.code in {
        'gazebo_prepare_response_invalid',
        'gazebo_prepare_response_mismatch',
    }


def test_socket_path_mode_owner_peer_and_configuration_drift_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    """The client attests the endpoint inode, mode, UID, peer, and seal."""
    ordinary = tmp_path / 'ordinary'
    ordinary.write_text('not a socket', encoding='ascii')
    client = GazeboPrepareClient(
        str(ordinary), expected_gazebo_uid=os.geteuid()
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        client.prepare(_claim())
    assert caught.value.code == 'gazebo_prepare_socket_not_socket'

    unsafe_parent = tmp_path / 'unsafe-parent'
    unsafe_parent.mkdir(mode=0o700)
    unsafe_path = unsafe_parent / 'prepare.sock'
    unsafe_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unsafe_listener.bind(str(unsafe_path))
    os.chmod(unsafe_path, 0o600)
    os.chmod(unsafe_parent, 0o770)
    unsafe_client = GazeboPrepareClient(
        str(unsafe_path), expected_gazebo_uid=os.geteuid()
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        unsafe_client.prepare(_claim())
    assert (
        caught.value.code
        == 'gazebo_prepare_socket_path_unprotected'
    )
    unsafe_listener.close()
    os.chmod(unsafe_parent, 0o700)

    socket_path = tmp_path / 'mode.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o660)
    client = GazeboPrepareClient(
        str(socket_path), expected_gazebo_uid=os.geteuid()
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        client.prepare(_claim())
    assert caught.value.code == 'gazebo_prepare_socket_mode_invalid'
    listener.close()

    target = tmp_path / 'target.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(target))
    os.chmod(target, 0o600)
    alias = tmp_path / 'alias.sock'
    alias.symlink_to(target)
    client = GazeboPrepareClient(
        str(alias), expected_gazebo_uid=os.geteuid()
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        client.prepare(_claim())
    assert caught.value.code == 'gazebo_prepare_socket_path_invalid'
    listener.close()

    object.__setattr__(client, '_socket_path', str(target))
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        client.prepare(_claim())
    assert caught.value.code == 'gazebo_prepare_configuration_changed'

    sealed_client = GazeboPrepareClient(
        str(ordinary), expected_gazebo_uid=os.geteuid()
    )
    forged_calls = []
    object.__setattr__(
        sealed_client,
        '_exchange',
        lambda _request: forged_calls.append('exchange'),
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        sealed_client.prepare(_claim())
    assert caught.value.code == 'gazebo_prepare_configuration_changed'
    assert forged_calls == []
    object.__delattr__(sealed_client, '_exchange')
    object.__setattr__(
        sealed_client,
        'prepare',
        lambda _claim_value: forged_calls.append('prepare'),
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        GazeboPrepareClient.prepare(sealed_client, _claim())
    assert caught.value.code == 'gazebo_prepare_configuration_changed'
    assert forged_calls == []
    object.__delattr__(sealed_client, 'prepare')
    object.__setattr__(sealed_client, '_timeout_seconds', 3.0)
    object.__setattr__(
        sealed_client,
        '_configuration_seal',
        (str(ordinary), os.geteuid(), 3.0),
    )
    with pytest.raises(GazeboPrepareDispatcherError) as caught:
        sealed_client.prepare(_claim())
    assert caught.value.code == 'gazebo_prepare_configuration_changed'

    peer_path = tmp_path / 'peer.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(peer_path))
    os.chmod(peer_path, 0o600)
    listener.listen(1)
    wrong_uid = os.geteuid() + 1
    peer_client = GazeboPrepareClient(
        str(peer_path), expected_gazebo_uid=wrong_uid
    )
    monkeypatch.setattr(
        GazeboPrepareClient,
        '_check_socket_path',
        lambda _self: (),
    )

    def accept_and_close():
        connection, _address = listener.accept()
        connection.close()
        listener.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(accept_and_close)
        with pytest.raises(GazeboPrepareDispatcherError) as caught:
            peer_client.prepare(_claim())
        future.result(timeout=5)
    assert caught.value.code == 'gazebo_prepare_peer_uid_mismatch'


def test_production_has_no_gazebo_ros_nav2_or_background_surface() -> None:
    """The Agent bridge remains local, one-shot, and dependency clean."""
    path = Path(dispatcher_module.__file__)
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported = set()
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or '')
        elif isinstance(node, ast.Call):
            calls.append(ast.unparse(node.func))

    assert not any(
        name == 'malbut_gazebo'
        or name.startswith('malbut_gazebo.')
        or name == 'rclpy'
        or name.startswith('rclpy.')
        or name == 'nav2'
        or name.startswith('nav2')
        for name in imported
    )
    assert 'malbut_gazebo' not in source
    assert 'rclpy' not in source
    assert 'NavigateToPose' not in source
    assert 'ActionClient' not in source
    assert not any(
        'create_task' in call
        or call.endswith('.start')
        or 'Thread' in call
        for call in calls
    )
