"""Tests for the durable coordinate-free Gazebo command gateway."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import socket
import sqlite3
import stat
import struct

import pytest

from malbut_gazebo.gazebo_monitor_room_gateway import (
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


_DIGEST = hashlib.sha256(b'gateway-test').hexdigest()
_OTHER_DIGEST = hashlib.sha256(b'gateway-port').hexdigest()


class _Clock:
    def __init__(self, value=2.0):
        self.value = value

    def __call__(self):
        return self.value


class _Port:
    def __init__(self):
        self.preflights = []
        self.sends = []
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
        self.sends.append(request)
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
            'status': 'active',
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


def _prepare_request():
    return PrepareOperation(
        prepare_request_id='prepare-1',
        operation_id='operation-1',
        robot_id='robot-1',
        map_id='map-1',
        map_revision='revision-1',
        semantic_revision='semantic-1',
        zones_digest=_DIGEST,
        target_binding_digest=_DIGEST,
        effects_digest=_DIGEST,
        profile_digest=_DIGEST,
        plan_digest=_DIGEST,
        ordered_semantic_samples=(
            OrderedSemanticSample(0, 0, 0, 1000, 2000),
            OrderedSemanticSample(1, 0, 1, 3000, 4000),
        ),
        deadline=100.0,
    )


def _wire(request_id, command='drive'):
    return GazeboMonitorRoomGatewayRequest(
        request_id=request_id,
        operation_id='operation-1',
        command=command,
    ).to_wire_bytes()


def _composition(tmp_path):
    clock = _Clock()
    store = GazeboMonitorRoomStore(tmp_path / 'operations.sqlite3')
    store.prepare(_prepare_request(), now=1.0)
    port = _Port()
    controller = GazeboMonitorRoomNav2Controller(
        store,
        port,
        worker_id='gateway-worker-1',
        lease_seconds=20.0,
        clock=clock,
    )
    replay = GazeboMonitorRoomGatewayReplayStore(
        tmp_path / 'gateway.sqlite3',
        core_store_namespace=store.store_namespace,
        clock=clock,
    )
    processor = GazeboMonitorRoomGatewayProcessor(
        store, controller, replay
    )
    return clock, store, port, controller, replay, processor


def _recv_exact(connection, size):
    chunks = []
    while sum(len(chunk) for chunk in chunks) < size:
        chunk = connection.recv(
            size - sum(len(item) for item in chunks)
        )
        if not chunk:
            raise RuntimeError('test response truncated')
        chunks.append(chunk)
    return b''.join(chunks)


def _exchange(socket_path, payload):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(2.0)
    try:
        connection.connect(str(socket_path))
        connection.sendall(struct.pack('!I', len(payload)) + payload)
        connection.shutdown(socket.SHUT_WR)
        size = struct.unpack('!I', _recv_exact(connection, 4))[0]
        response = _recv_exact(connection, size)
        assert connection.recv(1) == b''
        return response
    finally:
        connection.close()


def test_construction_creates_only_replay_schema_and_sends_nothing(tmp_path):
    """Opening the bridge never starts or cancels navigation."""
    _clock, store, port, _controller, replay, _processor = _composition(
        tmp_path
    )

    assert store.observe('operation-1').state == 'prepared'
    assert port.preflights == []
    assert port.sends == []
    assert port.cancels == []
    connection = sqlite3.connect(tmp_path / 'gateway.sqlite3')
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name LIKE 'gazebo_monitor_room_gateway_%'"
        )
    }
    assert 'gazebo_monitor_room_gateway_claims' in names
    assert 'gazebo_monitor_room_gateway_completions' in names
    replay.close()


def test_observe_is_coordinate_free_and_exact_replayed(tmp_path):
    """Repeated request IDs return byte-identical stored observations."""
    _clock, _store, port, _controller, _replay, processor = _composition(
        tmp_path
    )

    first = processor.handle_wire_bytes(_wire('observe-1', 'observe'))
    second = processor.handle_wire_bytes(_wire('observe-1', 'observe'))
    response = GazeboMonitorRoomGatewayResponse.from_wire_bytes(first)

    assert second == first
    assert response.state == 'prepared'
    assert response.physical_effects is False
    assert port.preflights == []
    for private in (b'x_m', b'y_m', b'goal_uuid', b'fence_epoch'):
        assert private not in first


def test_drive_exact_replay_does_not_advance_operation_twice(tmp_path):
    """A lost response cannot turn one drive request into two state steps."""
    clock, store, port, _controller, _replay, processor = _composition(
        tmp_path
    )

    first = processor.handle_wire_bytes(_wire('drive-1'))
    replay = processor.handle_wire_bytes(_wire('drive-1'))

    assert replay == first
    assert store.observe('operation-1').state == 'preflighting'
    assert port.preflights == []
    clock.value = 3.0
    navigating = GazeboMonitorRoomGatewayResponse.from_wire_bytes(
        processor.handle_wire_bytes(_wire('drive-2'))
    )
    assert navigating.state == 'navigating'
    assert len(port.preflights) == 1
    assert len(port.sends) == 1


def test_claimed_request_after_crash_recovers_by_observation_only(tmp_path):
    """A committed request claim is never re-executed after restart."""
    _clock, store, port, _controller, replay, processor = _composition(
        tmp_path
    )
    request = GazeboMonitorRoomGatewayRequest(
        request_id='drive-crash',
        operation_id='operation-1',
        command='drive',
    )
    assert replay.claim(request).first is True

    recovered = GazeboMonitorRoomGatewayResponse.from_wire_bytes(
        processor.handle_wire_bytes(request.to_wire_bytes())
    )

    assert recovered.state == 'prepared'
    assert store.observe('operation-1').state == 'prepared'
    assert port.preflights == []


def test_crash_after_state_change_never_applies_a_second_step(tmp_path):
    """Recovery observes a prior side effect instead of invoking drive."""
    _clock, store, port, controller, replay, processor = _composition(
        tmp_path
    )
    request = GazeboMonitorRoomGatewayRequest(
        request_id='drive-crash-after',
        operation_id='operation-1',
        command='drive',
    )
    replay.claim(request)
    assert controller.drive_once('operation-1').state == 'preflighting'

    recovered = GazeboMonitorRoomGatewayResponse.from_wire_bytes(
        processor.handle_wire_bytes(request.to_wire_bytes())
    )

    assert recovered.state == 'preflighting'
    assert store.observe('operation-1').state == 'preflighting'
    assert port.preflights == []


def test_cancel_uses_server_derived_identity_and_exact_replay(tmp_path):
    """The wire caller cannot choose a fence, goal, or cancellation ID."""
    clock, store, port, _controller, _replay, processor = _composition(
        tmp_path
    )
    processor.handle_wire_bytes(_wire('drive-1'))
    clock.value = 3.0
    processor.handle_wire_bytes(_wire('drive-2'))
    clock.value = 4.0

    first = processor.handle_wire_bytes(_wire('cancel-1', 'cancel'))
    replay = processor.handle_wire_bytes(_wire('cancel-1', 'cancel'))
    response = GazeboMonitorRoomGatewayResponse.from_wire_bytes(first)

    assert response.state == 'canceled'
    assert replay == first
    assert store.observe('operation-1').state == 'canceled'
    assert len(port.cancels) == 1
    assert port.cancels[0].cancel_request_id.startswith('gateway-cancel-')


def test_reopen_replays_completion_without_touching_controller(tmp_path):
    """A committed response survives process-local gateway reconstruction."""
    clock, store, port, controller, replay, processor = _composition(
        tmp_path
    )
    expected = processor.handle_wire_bytes(_wire('drive-1'))
    replay.close()
    reopened = GazeboMonitorRoomGatewayReplayStore(
        tmp_path / 'gateway.sqlite3',
        core_store_namespace=store.store_namespace,
        clock=clock,
    )
    restarted = GazeboMonitorRoomGatewayProcessor(
        store, controller, reopened
    )

    actual = restarted.handle_wire_bytes(_wire('drive-1'))

    assert actual == expected
    assert store.observe('operation-1').state == 'preflighting'
    assert port.preflights == []


def test_same_request_id_with_changed_command_is_conflict(tmp_path):
    """Idempotency keys cannot be rebound to a different command."""
    _clock, store, port, _controller, _replay, processor = _composition(
        tmp_path
    )
    processor.handle_wire_bytes(_wire('request-1', 'observe'))

    with pytest.raises(GazeboMonitorRoomGatewayError) as error:
        processor.handle_wire_bytes(_wire('request-1', 'drive'))

    assert error.value.code == 'gateway_replay_conflict'
    assert store.observe('operation-1').state == 'prepared'
    assert port.preflights == []


def test_concurrent_exact_drive_has_one_transition(tmp_path):
    """The processor serializes exact duplicates around the side effect."""
    _clock, store, port, _controller, _replay, processor = _composition(
        tmp_path
    )
    payload = _wire('drive-concurrent')

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _index: processor.handle_wire_bytes(payload),
            range(8),
        ))

    assert len(set(results)) == 1
    assert store.observe('operation-1').state == 'preflighting'
    assert port.preflights == []


def test_negative_zero_clock_is_canonical_across_restart(tmp_path):
    """Database zero normalization cannot invalidate a legitimate claim."""
    clock, store, _port, _controller, replay, processor = _composition(
        tmp_path
    )
    clock.value = -0.0
    expected = processor.handle_wire_bytes(_wire('zero-time', 'observe'))
    replay.close()
    reopened = GazeboMonitorRoomGatewayReplayStore(
        tmp_path / 'gateway.sqlite3',
        core_store_namespace=store.store_namespace,
        clock=clock,
    )
    restarted = GazeboMonitorRoomGatewayProcessor(
        store,
        GazeboMonitorRoomNav2Controller(
            store,
            _Port(),
            worker_id='gateway-worker-1',
            lease_seconds=20.0,
            clock=clock,
        ),
        reopened,
    )

    assert restarted.handle_wire_bytes(
        _wire('zero-time', 'observe')
    ) == expected


def test_raw_sqlite_failure_is_projected_without_private_exception_chain(
    tmp_path,
):
    """A broken local database never leaks its raw SQLite exception chain."""
    _clock, _store, _port, _controller, replay, _processor = _composition(
        tmp_path
    )
    replay._connection.close()
    request = GazeboMonitorRoomGatewayRequest(
        request_id='broken-database',
        operation_id='operation-1',
        command='observe',
    )

    with pytest.raises(GazeboMonitorRoomGatewayError) as error:
        replay.claim(request)

    assert error.value.code == 'gateway_replay_invalid'
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_append_only_claim_and_completion_reject_direct_changes(tmp_path):
    """Replay history cannot be updated or deleted through normal SQL."""
    _clock, _store, _port, _controller, replay, processor = _composition(
        tmp_path
    )
    processor.handle_wire_bytes(_wire('observe-1', 'observe'))
    connection = sqlite3.connect(tmp_path / 'gateway.sqlite3')

    for statement in (
        'UPDATE gazebo_monitor_room_gateway_claims '
        "SET command = 'drive' WHERE request_id = 'observe-1'",
        'DELETE FROM gazebo_monitor_room_gateway_claims '
        "WHERE request_id = 'observe-1'",
        'UPDATE gazebo_monitor_room_gateway_completions '
        "SET completed_at = 99 WHERE request_id = 'observe-1'",
        'DELETE FROM gazebo_monitor_room_gateway_completions '
        "WHERE request_id = 'observe-1'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
    replay.close()


def test_reopen_rejects_schema_or_row_tamper(tmp_path):
    """Restored-looking data must still match request and response digests."""
    clock, store, _port, _controller, replay, processor = _composition(
        tmp_path
    )
    processor.handle_wire_bytes(_wire('observe-1', 'observe'))
    replay.close()
    connection = sqlite3.connect(tmp_path / 'gateway.sqlite3')
    connection.execute(
        'DROP TRIGGER gazebo_monitor_room_gateway_completion_no_update'
    )
    connection.execute(
        "UPDATE gazebo_monitor_room_gateway_completions "
        "SET response_fingerprint = ? WHERE request_id = 'observe-1'",
        ('f' * 64,),
    )
    connection.execute(
        '''
        CREATE TRIGGER gazebo_monitor_room_gateway_completion_no_update
        BEFORE UPDATE ON gazebo_monitor_room_gateway_completions
        BEGIN SELECT RAISE(ABORT, 'gateway completion is immutable'); END
        '''
    )
    connection.commit()
    connection.close()

    with pytest.raises(GazeboMonitorRoomGatewayError):
        GazeboMonitorRoomGatewayReplayStore(
            tmp_path / 'gateway.sqlite3',
            core_store_namespace=store.store_namespace,
            clock=clock,
        )


def test_reopen_rejects_unexpected_user_schema_objects(tmp_path):
    """A dedicated replay database cannot silently host shadow tables."""
    clock, store, _port, _controller, replay, _processor = _composition(
        tmp_path
    )
    replay.close()
    connection = sqlite3.connect(tmp_path / 'gateway.sqlite3')
    connection.execute('CREATE TABLE shadow_gateway_state (value TEXT)')
    connection.commit()
    connection.close()

    with pytest.raises(GazeboMonitorRoomGatewayError) as error:
        GazeboMonitorRoomGatewayReplayStore(
            tmp_path / 'gateway.sqlite3',
            core_store_namespace=store.store_namespace,
            clock=clock,
        )

    assert error.value.code == 'gateway_replay_schema_invalid'


def test_nonempty_database_cannot_reactivate_after_full_schema_drop(tmp_path):
    """Erasing all owned objects never turns an old file into a fresh DB."""
    clock, store, _port, _controller, replay, processor = _composition(
        tmp_path
    )
    processor.handle_wire_bytes(_wire('observe-1', 'observe'))
    replay.close()
    connection = sqlite3.connect(tmp_path / 'gateway.sqlite3')
    for trigger in (
        'gazebo_monitor_room_gateway_metadata_no_update',
        'gazebo_monitor_room_gateway_metadata_no_delete',
        'gazebo_monitor_room_gateway_claim_no_update',
        'gazebo_monitor_room_gateway_claim_no_delete',
        'gazebo_monitor_room_gateway_completion_no_update',
        'gazebo_monitor_room_gateway_completion_no_delete',
    ):
        connection.execute(f'DROP TRIGGER {trigger}')
    for table in (
        'gazebo_monitor_room_gateway_completions',
        'gazebo_monitor_room_gateway_claims',
        'gazebo_monitor_room_gateway_metadata',
    ):
        connection.execute(f'DROP TABLE {table}')
    connection.commit()
    connection.close()

    with pytest.raises(GazeboMonitorRoomGatewayError) as error:
        GazeboMonitorRoomGatewayReplayStore(
            tmp_path / 'gateway.sqlite3',
            core_store_namespace=store.store_namespace,
            clock=clock,
        )

    assert error.value.code == 'gateway_replay_schema_invalid'


def test_store_namespace_and_private_path_are_fixed(tmp_path):
    """A replay DB cannot be moved to another core store or symlink path."""
    clock, store, _port, _controller, replay, _processor = _composition(
        tmp_path
    )
    replay.close()
    other = GazeboMonitorRoomStore(tmp_path / 'other-operations.sqlite3')
    with pytest.raises(GazeboMonitorRoomGatewayError):
        GazeboMonitorRoomGatewayReplayStore(
            tmp_path / 'gateway.sqlite3',
            core_store_namespace=other.store_namespace,
            clock=clock,
        )
    link = tmp_path / 'gateway-link.sqlite3'
    os.symlink(tmp_path / 'gateway.sqlite3', link)
    with pytest.raises(GazeboMonitorRoomGatewayError):
        GazeboMonitorRoomGatewayReplayStore(
            link,
            core_store_namespace=store.store_namespace,
            clock=clock,
        )


def test_processor_rejects_controller_from_another_operation_store(tmp_path):
    """A valid controller cannot be rebound to a different durable ledger."""
    clock, store, port, _controller, replay, _processor = _composition(
        tmp_path
    )
    other = GazeboMonitorRoomStore(tmp_path / 'other-store.sqlite3')
    other.prepare(_prepare_request(), now=1.0)
    wrong_controller = GazeboMonitorRoomNav2Controller(
        other,
        port,
        worker_id='gateway-worker-1',
        lease_seconds=20.0,
        clock=clock,
    )

    with pytest.raises(GazeboMonitorRoomGatewayError) as error:
        GazeboMonitorRoomGatewayProcessor(
            store, wrong_controller, replay
        )

    assert error.value.code == 'gateway_configuration_invalid'


def test_uds_server_start_is_side_effect_free_and_exchange_is_framed(
    tmp_path,
):
    """The fixed local bridge serves one minimal request and no ROS work."""
    _clock, store, port, _controller, _replay, processor = _composition(
        tmp_path
    )
    socket_path = tmp_path / 'gateway.sock'
    server = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()

    assert stat.S_IMODE(os.lstat(socket_path).st_mode) == 0o600
    assert store.observe('operation-1').state == 'prepared'
    assert port.preflights == []
    with ThreadPoolExecutor(max_workers=1) as pool:
        served = pool.submit(server.serve_once)
        response = _exchange(
            socket_path, _wire('socket-observe', 'observe')
        )
        served.result(timeout=5.0)

    parsed = GazeboMonitorRoomGatewayResponse.from_wire_bytes(response)
    assert parsed.state == 'prepared'
    assert parsed.command == 'observe'
    server.close()
    assert not socket_path.exists()


def test_uds_server_rejects_wrong_peer_uid_without_running_command(tmp_path):
    """Kernel peer credentials gate every command before payload handling."""
    _clock, store, port, _controller, _replay, processor = _composition(
        tmp_path
    )
    socket_path = tmp_path / 'gateway.sock'
    server = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid() + 1,
    )
    server.start()

    with ThreadPoolExecutor(max_workers=1) as pool:
        served = pool.submit(server.serve_once)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.0)
        connection.connect(str(socket_path))
        connection.sendall(
            struct.pack('!I', len(_wire('wrong-peer', 'drive')))
            + _wire('wrong-peer', 'drive')
        )
        connection.shutdown(socket.SHUT_WR)
        try:
            closed = connection.recv(1) == b''
        except ConnectionResetError:
            closed = True
        assert closed is True
        with pytest.raises(GazeboMonitorRoomGatewayError) as error:
            served.result(timeout=5.0)

    assert error.value.code == 'gateway_socket_peer_rejected'
    assert store.observe('operation-1').state == 'prepared'
    assert port.preflights == []
    server.close()


def test_uds_server_never_unlinks_preexisting_or_replaced_path(tmp_path):
    """Crash residue and post-bind replacement remain supervisor concerns."""
    _clock, _store, _port, _controller, _replay, processor = _composition(
        tmp_path
    )
    residue = tmp_path / 'residue.sock'
    residue.write_bytes(b'owned-by-supervisor')
    server = GazeboMonitorRoomGatewayServer(
        processor,
        residue,
        expected_agent_uid=os.geteuid(),
    )
    with pytest.raises(GazeboMonitorRoomGatewayError) as error:
        server.start()
    assert error.value.code == 'gateway_socket_exists'
    assert residue.read_bytes() == b'owned-by-supervisor'

    socket_path = tmp_path / 'gateway.sock'
    live = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    live.start()
    os.unlink(socket_path)
    socket_path.write_bytes(b'replacement')
    live.close()
    assert socket_path.read_bytes() == b'replacement'


def test_uds_server_rejects_path_replacement_before_accept(tmp_path):
    """The server re-attests its own socket inode on every serve boundary."""
    _clock, store, port, _controller, _replay, processor = _composition(
        tmp_path
    )
    socket_path = tmp_path / 'gateway.sock'
    server = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()
    os.unlink(socket_path)
    socket_path.write_bytes(b'replacement')

    with pytest.raises(GazeboMonitorRoomGatewayError) as error:
        server.serve_once()

    assert error.value.code == 'gateway_socket_invalid'
    assert store.observe('operation-1').state == 'prepared'
    assert port.preflights == []
    server.close()
    assert socket_path.read_bytes() == b'replacement'


def test_uds_server_rejects_oversize_frame_before_processor(tmp_path):
    """A bounded length prefix prevents oversized command allocation."""
    _clock, store, port, _controller, _replay, processor = _composition(
        tmp_path
    )
    socket_path = tmp_path / 'gateway.sock'
    server = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()

    with ThreadPoolExecutor(max_workers=1) as pool:
        served = pool.submit(server.serve_once)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.0)
        connection.connect(str(socket_path))
        connection.sendall(struct.pack('!I', 2049))
        connection.shutdown(socket.SHUT_WR)
        assert connection.recv(1) == b''
        with pytest.raises(GazeboMonitorRoomGatewayError) as error:
            served.result(timeout=5.0)

    assert error.value.code == 'gateway_socket_invalid'
    assert store.observe('operation-1').state == 'prepared'
    assert port.preflights == []
    server.close()
