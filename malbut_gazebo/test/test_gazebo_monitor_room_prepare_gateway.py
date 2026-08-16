"""Tests for the private Agent-to-Gazebo preparation intake."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct

import pytest

import malbut_gazebo.gazebo_monitor_room_prepare_gateway as prepare_module
from malbut_gazebo.gazebo_monitor_room_prepare_gateway import (
    PREPARE_GATEWAY_MAX_REQUEST_BYTES,
    PREPARE_GATEWAY_MAX_SAMPLES,
    GazeboMonitorRoomPrepareGatewayError,
    GazeboMonitorRoomPrepareProcessor,
    GazeboMonitorRoomPrepareRequest,
    GazeboMonitorRoomPrepareSample,
    GazeboMonitorRoomPrepareServer,
    GazeboMonitorRoomPreparedAcknowledgement,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    GazeboMonitorRoomStore,
)


_BOOT_ID = '12345678-1234-1234-1234-123456789abc'
_OTHER_BOOT_ID = 'abcdefab-cdef-abcd-efab-cdefabcdefab'
_DIGEST = hashlib.sha256(b'prepare-gateway-test').hexdigest()
_OTHER_DIGEST = hashlib.sha256(b'prepare-gateway-other').hexdigest()


def _sample(index=0, *, x_mm=314159, y_mm=-271828):
    return GazeboMonitorRoomPrepareSample(
        index=index,
        polygon_ordinal=index,
        row_ordinal=index,
        x_mm=x_mm,
        y_mm=y_mm,
    )


def _request(**changes):
    values = {
        'request_id': 'dispatch-request-1',
        'outbox_id': 'gazebo-execution-outbox-private-1',
        'operation_id': 'gazebo-operation-private-1',
        'prepare_request_id': 'gazebo-prepare-private-1',
        'host_boot_id': _BOOT_ID,
        'robot_id': 'robot-private-1',
        'map_id': 'map-private-1',
        'map_revision': 'map-revision-private-1',
        'semantic_revision': 'semantic-private-1',
        'zones_digest': _DIGEST,
        'target_binding_digest': _DIGEST,
        'effects_digest': _DIGEST,
        'profile_digest': _DIGEST,
        'plan_digest': _DIGEST,
        'ordered_semantic_samples': (_sample(),),
        'deadline_boottime_ns': 100_000_000_000,
    }
    values.update(changes)
    return GazeboMonitorRoomPrepareRequest(**values)


def _store(tmp_path):
    return GazeboMonitorRoomStore(
        tmp_path / 'operations.sqlite3',
        boot_id_reader=lambda: _BOOT_ID,
    )


def _processor(store, *, now=1.0):
    return GazeboMonitorRoomPrepareProcessor(
        store,
        expected_robot_id='robot-private-1',
        local_boot_id=_BOOT_ID,
        clock=lambda: now,
    )


def _operation_count(store):
    return store._connection.execute(
        'SELECT COUNT(*) FROM gazebo_monitor_room_operations'
    ).fetchone()[0]


def _recv_exact(connection, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise AssertionError('test response was truncated')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def test_request_is_exact_canonical_json_and_private_repr_is_redacted():
    """Requests serialize deterministically without repr disclosure."""
    request = _request()
    wire = request.to_wire_bytes()

    assert wire == json.dumps(
        request.to_dict(),
        sort_keys=True,
        separators=(',', ':'),
    ).encode('ascii')
    assert GazeboMonitorRoomPrepareRequest.from_wire_bytes(wire) == request
    rendered = repr(request)
    sample_rendered = repr(request.ordered_semantic_samples[0])
    for private in (
        'dispatch-request-1',
        'private-1',
        _BOOT_ID,
        _DIGEST,
        '314159',
        '-271828',
    ):
        assert private not in rendered
        assert private not in sample_rendered


def test_parser_accepts_the_complete_4096_sample_bound():
    """The wire bound carries the outbox planner's complete maximum."""
    samples = tuple(
        _sample(index, x_mm=index, y_mm=-index)
        for index in range(PREPARE_GATEWAY_MAX_SAMPLES)
    )
    request = _request(ordered_semantic_samples=samples)
    wire = request.to_wire_bytes()

    assert PREPARE_GATEWAY_MAX_SAMPLES == 4096
    assert len(wire) < PREPARE_GATEWAY_MAX_REQUEST_BYTES
    parsed = GazeboMonitorRoomPrepareRequest.from_wire_bytes(wire)
    assert len(parsed.ordered_semantic_samples) == 4096
    assert parsed.ordered_semantic_samples[-1].index == 4095


@pytest.mark.parametrize(
    'mutate',
    (
        lambda value: {**value, 'extra': False},
        lambda value: {
            **value,
            'physical_authorized': True,
        },
        lambda value: {
            **value,
            'ordered_semantic_samples': [
                {
                    **value['ordered_semantic_samples'][0],
                    'private_extra': 1,
                }
            ],
        },
    ),
)
def test_extra_and_authority_fields_are_rejected(mutate):
    """Extra keys and caller authority claims fail closed."""
    value = mutate(_request().to_dict())
    wire = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('ascii')

    with pytest.raises(GazeboMonitorRoomPrepareGatewayError) as caught:
        GazeboMonitorRoomPrepareRequest.from_wire_bytes(wire)
    assert caught.value.code == 'prepare_gateway_request_invalid'


@pytest.mark.parametrize('constant', ('NaN', 'Infinity', '-Infinity'))
def test_duplicate_nonfinite_and_noncanonical_json_is_rejected(constant):
    """Aliases, non-finite values, and noncanonical bytes are invalid."""
    wire = _request().to_wire_bytes()
    duplicate = wire.replace(
        b'"request_id":"dispatch-request-1"',
        b'"request_id":"dispatch-request-1",'
        b'"request_id":"private-duplicate"',
        1,
    )
    nonfinite = wire.replace(b'100000000000', constant.encode('ascii'), 1)

    for malformed in (duplicate, nonfinite, b' ' + wire):
        with pytest.raises(GazeboMonitorRoomPrepareGatewayError) as caught:
            GazeboMonitorRoomPrepareRequest.from_wire_bytes(malformed)
        assert caught.value.code == 'prepare_gateway_request_invalid'


def test_oversize_and_cross_domain_identity_swaps_fail_closed():
    """Size overflow and typed-identity swaps cannot cross the boundary."""
    oversize = b'x' * (PREPARE_GATEWAY_MAX_REQUEST_BYTES + 1)
    with pytest.raises(GazeboMonitorRoomPrepareGatewayError) as caught:
        GazeboMonitorRoomPrepareRequest.from_wire_bytes(oversize)
    assert caught.value.code == 'prepare_gateway_request_invalid'

    with pytest.raises(GazeboMonitorRoomPrepareGatewayError):
        _request(
            operation_id='gazebo-execution-outbox-private-1',
            outbox_id='gazebo-operation-private-1',
        )
    with pytest.raises(GazeboMonitorRoomPrepareGatewayError):
        _request(
            operation_id='gazebo-prepare-private-1',
            prepare_request_id='gazebo-operation-private-1',
        )


def test_processor_prepares_once_and_exact_retry_never_duplicates(tmp_path):
    """A lost response can be retried without duplicating durable rows."""
    store = _store(tmp_path)
    processor = _processor(store)
    request = _request()

    first_wire = processor.handle_wire_bytes(request.to_wire_bytes())
    replay_wire = processor.handle_wire_bytes(request.to_wire_bytes())
    first = GazeboMonitorRoomPreparedAcknowledgement.from_wire_bytes(
        first_wire
    )
    replay = GazeboMonitorRoomPreparedAcknowledgement.from_wire_bytes(
        replay_wire
    )

    assert first.prepare_fingerprint == replay.prepare_fingerprint
    assert first.replayed is False
    assert replay.replayed is True
    assert first.state == replay.state == 'prepared'
    assert _operation_count(store) == 1
    assert len(store.events(request.operation_id)) == 2
    assert store.private_operation_binding(
        request.operation_id
    ).prepare_fingerprint == first.prepare_fingerprint

    for response in (first, replay):
        assert response.runtime_mode == 'gazebo'
        assert response.simulation is True
        assert response.physical_authorized is False
        assert response.physical_effects is False
        assert response.viewer_live is False
        assert response.camera_coverage_validated is False
        assert response.coverage_achieved is False
        rendered = repr(response)
        for private in (
            request.robot_id,
            request.map_id,
            request.prepare_request_id,
            request.host_boot_id,
            str(request.ordered_semantic_samples[0].x_mm),
            str(request.ordered_semantic_samples[0].y_mm),
            response.prepare_fingerprint,
        ):
            assert private not in rendered

    for private in (
        b'robot-private-1',
        b'map-private-1',
        b'gazebo-prepare-private-1',
        _BOOT_ID.encode('ascii'),
        b'ordered_semantic_samples',
        b'x_mm',
        b'y_mm',
        b'314159',
        b'-271828',
    ):
        assert private not in first_wire
        assert private not in replay_wire
    store.close()


def test_conflict_deadline_robot_and_boot_mismatch_are_chain_free(tmp_path):
    """Binding conflicts and stale authority produce closed errors."""
    store = _store(tmp_path)
    processor = _processor(store)
    request = _request()
    processor.prepare(request)

    cases = (
        (
            replace(request, plan_digest=_OTHER_DIGEST),
            'prepare_gateway_conflict',
        ),
        (
            replace(
                request,
                request_id='dispatch-expired',
                outbox_id='gazebo-execution-outbox-expired',
                operation_id='gazebo-operation-expired',
                prepare_request_id='gazebo-prepare-expired',
                deadline_boottime_ns=1_000_000_000,
            ),
            'prepare_gateway_deadline_expired',
        ),
        (
            replace(
                request,
                request_id='dispatch-wrong-robot',
                outbox_id='gazebo-execution-outbox-wrong-robot',
                operation_id='gazebo-operation-wrong-robot',
                prepare_request_id='gazebo-prepare-wrong-robot',
                robot_id='robot-private-wrong',
            ),
            'prepare_gateway_robot_mismatch',
        ),
        (
            replace(
                request,
                request_id='dispatch-wrong-boot',
                outbox_id='gazebo-execution-outbox-wrong-boot',
                operation_id='gazebo-operation-wrong-boot',
                prepare_request_id='gazebo-prepare-wrong-boot',
                host_boot_id=_OTHER_BOOT_ID,
            ),
            'prepare_gateway_boot_mismatch',
        ),
    )
    for invalid, code in cases:
        with pytest.raises(GazeboMonitorRoomPrepareGatewayError) as caught:
            processor.prepare(invalid)
        assert caught.value.code == code
        assert str(caught.value) == code
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert caught.value.__traceback__ is None
        rendered = repr(caught.value)
        for private in (
            invalid.robot_id,
            invalid.map_id,
            invalid.prepare_request_id,
            invalid.host_boot_id,
            str(invalid.ordered_semantic_samples[0].x_mm),
        ):
            assert private not in rendered
    assert _operation_count(store) == 1
    store.close()


def test_processor_configuration_is_fixed_and_detects_object_drift(
    tmp_path,
):
    """Callers cannot replace constructor-fixed authority collaborators."""
    store = _store(tmp_path)
    processor = _processor(store)
    request = _request()

    with pytest.raises(TypeError):
        processor.prepare(
            request,
            expected_robot_id='robot-private-wrong',
        )
    object.__setattr__(processor, '_expected_robot_id', 'robot-tampered')
    with pytest.raises(GazeboMonitorRoomPrepareGatewayError) as caught:
        processor.prepare(request)
    assert caught.value.code == 'prepare_gateway_configuration_invalid'
    assert _operation_count(store) == 0
    store.close()


def test_server_start_is_side_effect_free_for_store_and_socket_is_0600(
    tmp_path,
):
    """Start binds a private socket but never prepares store state."""
    store = _store(tmp_path)
    processor = _processor(store)
    path = tmp_path / 'prepare.sock'
    server = GazeboMonitorRoomPrepareServer(
        processor,
        path,
        expected_agent_uid=os.geteuid(),
    )

    assert _operation_count(store) == 0
    server.start()
    assert _operation_count(store) == 0
    assert stat.S_ISSOCK(os.lstat(path).st_mode)
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600

    request = _request()
    with ThreadPoolExecutor(max_workers=1) as pool:
        served = pool.submit(server.serve_once)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(path))
            wire = request.to_wire_bytes()
            client.sendall(struct.pack('!I', len(wire)) + wire)
            client.shutdown(socket.SHUT_WR)
            size = struct.unpack('!I', _recv_exact(client, 4))[0]
            response_wire = _recv_exact(client, size)
            assert client.recv(1) == b''
        served.result(timeout=2.0)

    response = GazeboMonitorRoomPreparedAcknowledgement.from_wire_bytes(
        response_wire
    )
    assert response.state == 'prepared'
    assert _operation_count(store) == 1
    server.close()
    assert not path.exists()
    store.close()


def test_server_rejects_wrong_peer_uid_before_parsing(tmp_path):
    """SO_PEERCRED rejects an unexpected Linux UID before request input."""
    store = _store(tmp_path)
    processor = _processor(store)
    path = tmp_path / 'wrong-peer.sock'
    server = GazeboMonitorRoomPrepareServer(
        processor,
        path,
        expected_agent_uid=os.geteuid() + 1,
    )
    server.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            served = pool.submit(server.serve_once)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))
                client.shutdown(socket.SHUT_WR)
            with pytest.raises(
                GazeboMonitorRoomPrepareGatewayError
            ) as caught:
                served.result(timeout=2.0)
        assert caught.value.code == 'prepare_gateway_socket_peer_rejected'
        assert caught.value.__cause__ is None
        assert _operation_count(store) == 0
    finally:
        server.close()
        store.close()


def test_relative_and_symlink_parent_socket_paths_are_rejected(tmp_path):
    """Only canonical absolute paths under protected parents are valid."""
    store = _store(tmp_path)
    processor = _processor(store)
    with pytest.raises(GazeboMonitorRoomPrepareGatewayError) as caught:
        GazeboMonitorRoomPrepareServer(
            processor,
            'relative.sock',
            expected_agent_uid=os.geteuid(),
        )
    assert caught.value.code == 'prepare_gateway_socket_invalid'

    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    linked = tmp_path / 'linked'
    linked.symlink_to(private, target_is_directory=True)
    server = GazeboMonitorRoomPrepareServer(
        processor,
        linked / 'prepare.sock',
        expected_agent_uid=os.geteuid(),
    )
    with pytest.raises(GazeboMonitorRoomPrepareGatewayError) as caught:
        server.start()
    assert caught.value.code == 'prepare_gateway_socket_invalid'
    assert _operation_count(store) == 0
    server.close()
    store.close()


def test_production_module_has_no_ros_or_navigation_surface():
    """The intake module cannot create ROS or navigation side effects."""
    source = Path(prepare_module.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported = []
    methods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or '')
        elif isinstance(node, ast.FunctionDef):
            methods.add(node.name)

    assert not any(name.startswith('rclpy') for name in imported)
    assert not any('nav2' in name.lower() for name in imported)
    assert {'drive', 'observe', 'cancel'}.isdisjoint(methods)
