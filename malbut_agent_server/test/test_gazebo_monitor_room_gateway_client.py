"""Tests for the Agent-side Gazebo monitor-room gateway client."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time

import pytest

import malbut_agent_server.gazebo_monitor_room_gateway_client as client_module
from malbut_agent_server.gazebo_monitor_room_gateway_client import (
    GAZEBO_MONITOR_ROOM_GATEWAY_MAX_RESPONSE_BYTES,
    GazeboMonitorRoomGatewayClient,
    GazeboMonitorRoomGatewayClientError,
)
from malbut_gazebo.gazebo_monitor_room_gateway_contract import (
    GazeboMonitorRoomGatewayRequest,
    GazeboMonitorRoomGatewayResponse,
)
from malbut_gazebo.gazebo_monitor_room_gateway import (
    GazeboMonitorRoomGatewayProcessor,
    GazeboMonitorRoomGatewayReplayStore,
    GazeboMonitorRoomGatewayServer,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    GazeboMonitorRoomNav2Controller,
)
from malbut_gazebo.gazebo_monitor_room_store import (
    GazeboMonitorRoomStore,
    OrderedSemanticSample,
    PrepareOperation,
)


_EVIDENCE = hashlib.sha256(b'agent-gateway-client-test').hexdigest()


class _InteropPort:
    """Fake Nav2 boundary for real gateway framing integration."""

    def __init__(self):
        self.calls = []

    def preflight(self, request):
        self.calls.append(('preflight', request))
        raise AssertionError('first durable drive must not call Nav2')

    def ensure_started(self, request):
        self.calls.append(('start', request))
        raise AssertionError('first durable drive must not call Nav2')

    def observe_goal(self, request):
        self.calls.append(('observe', request))
        raise AssertionError('first durable drive must not call Nav2')

    def cancel_goal(self, request):
        self.calls.append(('cancel', request))
        raise AssertionError('first durable drive must not call Nav2')


def _interop_prepare_request():
    return PrepareOperation(
        prepare_request_id='prepare-interop-1',
        operation_id='operation-interop-1',
        robot_id='robot-interop-1',
        map_id='map-interop-1',
        map_revision='revision-interop-1',
        semantic_revision='semantic-interop-1',
        zones_digest=_EVIDENCE,
        target_binding_digest=_EVIDENCE,
        effects_digest=_EVIDENCE,
        profile_digest=_EVIDENCE,
        plan_digest=_EVIDENCE,
        ordered_semantic_samples=(
            OrderedSemanticSample(0, 0, 0, 1000, 2000),
        ),
        deadline=100.0,
    )


def _recv_exact(connection, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise AssertionError('test request was truncated')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _valid_response(request, **changes):
    values = {
        'request_id': request.request_id,
        'operation_id': request.operation_id,
        'command': request.command,
        'state': 'prepared',
        'current_sample_index': 0,
        'navigation_samples_total': 2,
        'navigation_samples_reached': 0,
        'terminal': False,
        'robot_blocked': True,
        'terminal_code': None,
        'evidence_digest': _EVIDENCE,
    }
    values.update(changes)
    return GazeboMonitorRoomGatewayResponse(**values).to_wire_bytes()


def _raw_response(request, **changes):
    value = json.loads(_valid_response(request))
    value.update(changes)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('ascii')


def _start_server(tmp_path, handler, name='gateway.sock'):
    path = tmp_path / name
    ready = threading.Event()
    failures = []
    requests = []

    def serve():
        try:
            with socket.socket(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            ) as listener:
                listener.bind(str(path))
                os.chmod(path, 0o600)
                listener.listen(1)
                ready.set()
                connection, _address = listener.accept()
                with connection:
                    header = _recv_exact(connection, 4)
                    size = struct.unpack('!I', header)[0]
                    payload = _recv_exact(connection, size)
                    requests.append((header, payload))
                    assert connection.recv(1) == b''
                    handler(connection, payload)
        except Exception as error:  # pragma: no cover - asserted by helper
            failures.append(error)
            ready.set()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2.0)
    return path, thread, failures, requests


def _finish(thread, failures):
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert failures == []


def _send_response(connection, response):
    connection.sendall(struct.pack('!I', len(response)) + response)
    connection.shutdown(socket.SHUT_WR)


def _client(path, **changes):
    values = {
        'socket_path': str(path),
        'expected_server_uid': os.geteuid(),
        'timeout_seconds': 1.0,
    }
    values.update(changes)
    return GazeboMonitorRoomGatewayClient(**values)


@pytest.mark.parametrize('command', ('drive', 'observe', 'cancel'))
def test_exact_gazebo_contract_interop_is_coordinate_free(
    tmp_path,
    command,
):
    """The independent Agent schema is byte-compatible with Gazebo."""
    def handle(connection, payload):
        request = GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)
        _send_response(connection, _valid_response(request))

    path, thread, failures, requests = _start_server(
        tmp_path,
        handle,
        name=f'{command}.sock',
    )
    client = _client(path)

    result = client.exchange(
        request_id=f'request-{command}',
        operation_id='operation-1',
        command=command,
    )
    _finish(thread, failures)

    expected = GazeboMonitorRoomGatewayRequest(
        request_id=f'request-{command}',
        operation_id='operation-1',
        command=command,
    ).to_wire_bytes()
    assert requests == [(struct.pack('!I', len(expected)), expected)]
    assert result.request_id == f'request-{command}'
    assert result.operation_id == 'operation-1'
    assert result.command == command
    assert result.runtime_mode == 'gazebo'
    assert result.simulation is True
    assert result.physical_authorized is False
    assert result.physical_effects is False
    assert result.viewer_live is False
    assert result.camera_coverage_validated is False
    assert result.coverage_achieved is False
    result_wire = json.dumps(
        result.to_dict(),
        sort_keys=True,
        separators=(',', ':'),
    ).encode('ascii')
    assert GazeboMonitorRoomGatewayResponse.from_wire_bytes(
        result_wire
    ).response_fingerprint == result.response_fingerprint
    for private in (
        b'x_m',
        b'y_m',
        b'goal_uuid',
        b'fence_epoch',
        b'lease',
        b'map_id',
        b'worker_id',
    ):
        assert private not in requests[0][1]
        assert private not in result_wire


def test_real_gateway_server_replays_one_agent_drive_exactly_once(tmp_path):
    """Agent framing interoperates with the durable Gazebo gateway."""
    operation_store = GazeboMonitorRoomStore(
        tmp_path / 'operations.sqlite3'
    )
    operation_store.prepare(_interop_prepare_request(), now=1.0)
    port = _InteropPort()
    controller = GazeboMonitorRoomNav2Controller(
        operation_store,
        port,
        worker_id='agent-interop-worker',
        lease_seconds=20.0,
        clock=lambda: 2.0,
    )
    replay_store = GazeboMonitorRoomGatewayReplayStore(
        tmp_path / 'gateway-replay.sqlite3',
        core_store_namespace=operation_store.store_namespace,
        clock=lambda: 2.0,
    )
    processor = GazeboMonitorRoomGatewayProcessor(
        operation_store,
        controller,
        replay_store,
    )
    socket_path = tmp_path / 'real-gateway.sock'
    server = GazeboMonitorRoomGatewayServer(
        processor,
        socket_path,
        expected_agent_uid=os.geteuid(),
    )
    server.start()
    client = _client(socket_path)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_serve = pool.submit(server.serve_once)
            first = client.exchange(
                request_id='agent-drive-1',
                operation_id='operation-interop-1',
                command='drive',
            )
            first_serve.result(timeout=2.0)
            replay_serve = pool.submit(server.serve_once)
            replay = client.exchange(
                request_id='agent-drive-1',
                operation_id='operation-interop-1',
                command='drive',
            )
            replay_serve.result(timeout=2.0)
    finally:
        server.close()
        replay_store.close()
        operation_store.close()

    assert first == replay
    assert first.state == 'preflighting'
    assert first.runtime_mode == 'gazebo'
    assert first.simulation is True
    assert first.physical_authorized is False
    assert first.physical_effects is False
    assert first.viewer_live is False
    assert first.camera_coverage_validated is False
    assert first.coverage_achieved is False
    assert port.calls == []


def test_production_module_has_no_gazebo_or_ros_import():
    """The Agent client does not create a reverse package dependency."""
    source = Path(client_module.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or '')

    assert not any(name.startswith('malbut_gazebo') for name in imported)
    assert not any(name.startswith('rclpy') for name in imported)


def test_request_is_deterministic_and_rejects_authority_fields():
    """Only the three caller-supplied selectors enter canonical JSON."""
    first = GazeboMonitorRoomGatewayClient._request_bytes(
        request_id='request-1',
        operation_id='operation-1',
        command='drive',
    )
    second = GazeboMonitorRoomGatewayClient._request_bytes(
        request_id='request-1',
        operation_id='operation-1',
        command='drive',
    )

    assert first == second
    assert json.loads(first) == {
        'schema_version': 1,
        'request_id': 'request-1',
        'operation_id': 'operation-1',
        'command': 'drive',
    }
    assert GazeboMonitorRoomGatewayRequest.from_wire_bytes(
        first
    ).to_wire_bytes() == first


class _String(str):
    """String subtype for exact built-in type tests."""


@pytest.mark.parametrize(
    ('field', 'value'),
    (
        ('request_id', _String('request-1')),
        ('request_id', '../request'),
        ('operation_id', ''),
        ('operation_id', 'operation\nprivate'),
        ('command', _String('drive')),
        ('command', 'navigate'),
    ),
)
def test_invalid_request_fails_before_socket_access(
    tmp_path,
    monkeypatch,
    field,
    value,
):
    """Malformed opaque values cannot reach the local transport."""
    client = _client(tmp_path / 'missing.sock')
    monkeypatch.setattr(
        GazeboMonitorRoomGatewayClient,
        '_check_socket_path',
        lambda _client: pytest.fail('invalid request accessed socket'),
    )
    values = {
        'request_id': 'request-1',
        'operation_id': 'operation-1',
        'command': 'drive',
    }
    values[field] = value

    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        client.exchange(**values)

    assert raised.value.code == 'gazebo_gateway_client_request_invalid'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ('changed_field', 'changed_value'),
    (
        ('request_id', 'other-request'),
        ('operation_id', 'other-operation'),
        ('command', 'observe'),
    ),
)
def test_response_must_correlate_all_request_selectors(
    tmp_path,
    changed_field,
    changed_value,
):
    """A valid response for another command or operation is discarded."""
    def handle(connection, payload):
        request = GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)
        changes = {changed_field: changed_value}
        _send_response(connection, _valid_response(request, **changes))

    path, thread, failures, _requests = _start_server(tmp_path, handle)

    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        _client(path).exchange(
            request_id='request-1',
            operation_id='operation-1',
            command='drive',
        )
    _finish(thread, failures)

    assert raised.value.code == 'gazebo_gateway_client_response_mismatch'
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ('changed_field', 'changed_value'),
    (
        ('simulation', False),
        ('physical_authorized', True),
        ('physical_effects', True),
        ('viewer_live', True),
        ('camera_coverage_validated', True),
        ('coverage_achieved', True),
        ('runtime_mode', 'physical'),
        ('schema_version', True),
        ('terminal', True),
        ('robot_blocked', False),
        ('navigation_samples_reached', 1),
        ('evidence_digest', 'not-a-digest'),
    ),
)
def test_response_rejects_stronger_claims_and_invalid_state(
    tmp_path,
    changed_field,
    changed_value,
):
    """Simulation observations cannot be upgraded by wire assertions."""
    def handle(connection, payload):
        request = GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)
        response = _raw_response(
            request,
            **{changed_field: changed_value},
        )
        _send_response(connection, response)

    path, thread, failures, _requests = _start_server(tmp_path, handle)

    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        _client(path).exchange(
            request_id='request-1',
            operation_id='operation-1',
            command='observe',
        )
    _finish(thread, failures)

    assert raised.value.code == 'gazebo_gateway_client_response_invalid'
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    'response_builder',
    (
        lambda request: (
            _valid_response(request)[:-1]
            + b',"x_m":1.0}'
        ),
        lambda request: _valid_response(request).replace(
            b'"schema_version":1',
            b'"schema_version":1,"schema_version":1',
        ),
        lambda _request: b'not-json',
        lambda _request: b'[]',
        lambda _request: b'\xff\xfe',
    ),
)
def test_response_parser_rejects_extra_duplicate_and_malformed_json(
    tmp_path,
    response_builder,
):
    """Only one exact duplicate-free response object is accepted."""
    def handle(connection, payload):
        request = GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)
        _send_response(connection, response_builder(request))

    path, thread, failures, _requests = _start_server(tmp_path, handle)

    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        _client(path).exchange(
            request_id='request-1',
            operation_id='operation-1',
            command='observe',
        )
    _finish(thread, failures)

    assert raised.value.code == 'gazebo_gateway_client_response_invalid'
    assert raised.value.__context__ is None


def test_result_is_frozen_redacted_and_detects_bypass_mutation(tmp_path):
    """A validated result cannot normally be changed after acceptance."""
    def handle(connection, payload):
        request = GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)
        _send_response(connection, _valid_response(request))

    path, thread, failures, _requests = _start_server(tmp_path, handle)
    result = _client(path).exchange(
        request_id='private-request',
        operation_id='private-operation',
        command='observe',
    )
    _finish(thread, failures)

    with pytest.raises(FrozenInstanceError):
        result.state = 'succeeded'
    assert 'private-request' not in repr(result)
    assert 'private-operation' not in repr(result)
    assert _EVIDENCE not in repr(result)
    for field_name, value in (
        ('state', 'failed'),
        ('physical_authorized', True),
        ('runtime_mode', _String('gazebo')),
        ('schema_version', True),
    ):
        changed = replace(result)
        object.__setattr__(changed, field_name, value)
        with pytest.raises(GazeboMonitorRoomGatewayClientError):
            _ = changed.response_fingerprint


@pytest.mark.parametrize(
    ('frame_builder', 'expected_code'),
    (
        (
            lambda _request: struct.pack(
                '!I',
                GAZEBO_MONITOR_ROOM_GATEWAY_MAX_RESPONSE_BYTES + 1,
            ),
            'gazebo_gateway_client_response_too_large',
        ),
        (
            lambda _request: struct.pack('!I', 5) + b'{}',
            'gazebo_gateway_client_response_truncated',
        ),
        (
            lambda request: (
                lambda response: (
                    struct.pack('!I', len(response)) + response + b'x'
                )
            )(_valid_response(request)),
            'gazebo_gateway_client_response_extra_data',
        ),
    ),
)
def test_transport_rejects_oversize_truncated_and_extra_frames(
    tmp_path,
    frame_builder,
    expected_code,
):
    """The network-order four-byte frame has one bounded payload."""
    def handle(connection, payload):
        request = GazeboMonitorRoomGatewayRequest.from_wire_bytes(payload)
        connection.sendall(frame_builder(request))
        connection.shutdown(socket.SHUT_WR)

    path, thread, failures, _requests = _start_server(tmp_path, handle)

    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        _client(path).exchange(
            request_id='request-1',
            operation_id='operation-1',
            command='observe',
        )
    _finish(thread, failures)

    assert raised.value.code == expected_code
    assert raised.value.__context__ is None


def test_total_timeout_cannot_be_multiplied_by_dripped_bytes(tmp_path):
    """All connect/send/receive work shares one monotonic deadline."""
    def handle(connection, _payload):
        for byte in struct.pack('!I', 2) + b'{}':
            try:
                connection.sendall(bytes([byte]))
            except BrokenPipeError:
                return
            time.sleep(0.04)

    path, thread, failures, _requests = _start_server(tmp_path, handle)
    client = _client(path, timeout_seconds=0.06)

    started = time.monotonic()
    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        client.exchange(
            request_id='request-1',
            operation_id='operation-1',
            command='observe',
        )
    elapsed = time.monotonic() - started
    _finish(thread, failures)

    assert raised.value.code == 'gazebo_gateway_client_timeout'
    assert elapsed < 0.3
    assert raised.value.__context__ is None


def test_peer_must_have_expected_linux_uid(tmp_path, monkeypatch):
    """Path metadata cannot replace SO_PEERCRED verification."""
    path = tmp_path / 'peer.sock'
    ready = threading.Event()
    failures = []

    def serve():
        try:
            with socket.socket(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            ) as listener:
                listener.bind(str(path))
                os.chmod(path, 0o600)
                listener.listen(1)
                ready.set()
                connection, _address = listener.accept()
                with connection:
                    assert connection.recv(1) == b''
        except Exception as error:  # pragma: no cover - helper assertion
            failures.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2.0)
    client = _client(path, expected_server_uid=os.geteuid() + 1)
    snapshot = (('fixed-test-snapshot', 1, 1, 0, 0, 0),)
    monkeypatch.setattr(
        GazeboMonitorRoomGatewayClient,
        '_check_socket_path',
        lambda _client: snapshot,
    )

    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        client.exchange(
            request_id='request-1',
            operation_id='operation-1',
            command='observe',
        )
    _finish(thread, failures)

    assert raised.value.code == 'gazebo_gateway_client_peer_uid_mismatch'
    assert raised.value.__context__ is None


def test_socket_path_owner_mode_and_type_are_checked(tmp_path):
    """The fixed path must remain the protected socket being contacted."""
    regular = tmp_path / 'regular.sock'
    regular.write_bytes(b'not-a-socket')
    with pytest.raises(GazeboMonitorRoomGatewayClientError) as not_socket:
        _client(regular).exchange(
            request_id='request-1',
            operation_id='operation-1',
            command='observe',
        )
    assert not_socket.value.code == 'gazebo_gateway_client_socket_not_socket'

    target = tmp_path / 'target.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(target))
        os.chmod(target, 0o600)
        link = tmp_path / 'linked.sock'
        link.symlink_to(target)
        with pytest.raises(GazeboMonitorRoomGatewayClientError) as symlink:
            _client(link).exchange(
                request_id='request-1',
                operation_id='operation-1',
                command='observe',
            )
        assert symlink.value.code == (
            'gazebo_gateway_client_socket_path_invalid'
        )

        os.chmod(target, 0o660)
        with pytest.raises(GazeboMonitorRoomGatewayClientError) as mode:
            _client(target).exchange(
                request_id='request-1',
                operation_id='operation-1',
                command='observe',
            )
        assert mode.value.code == (
            'gazebo_gateway_client_socket_mode_invalid'
        )


@pytest.mark.parametrize(
    'changes',
    (
        {'socket_path': 'relative.sock'},
        {'socket_path': '/tmp/../tmp/gateway.sock'},
        {'socket_path': '/tmp/\ud800'},
        {'expected_server_uid': True},
        {'expected_server_uid': -1},
        {'timeout_seconds': True},
        {'timeout_seconds': 0},
        {'timeout_seconds': 31},
        {'timeout_seconds': float('nan')},
    ),
)
def test_client_configuration_is_fixed_and_strict(changes):
    """Ambiguous endpoint identities and deadlines fail at construction."""
    values = {
        'socket_path': '/run/malbut/gateway.sock',
        'expected_server_uid': os.geteuid(),
        'timeout_seconds': 1.0,
    }
    values.update(changes)

    with pytest.raises(ValueError):
        GazeboMonitorRoomGatewayClient(**values)


def test_instance_field_and_exchange_method_shadow_fail_before_transport(
    tmp_path,
):
    """A valid exact client cannot be redirected after construction."""
    client = _client(tmp_path / 'missing.sock')
    calls = []
    object.__setattr__(
        client,
        '_exchange_impl',
        lambda **_values: calls.append('shadowed'),
    )

    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        GazeboMonitorRoomGatewayClient.exchange(
            client,
            request_id='request-1',
            operation_id='operation-1',
            command='drive',
        )

    assert raised.value.code == (
        'gazebo_gateway_client_configuration_changed'
    )
    assert calls == []
    object.__delattr__(client, '_exchange_impl')
    object.__setattr__(client, '_socket_path', '/tmp/redirected.sock')

    with pytest.raises(GazeboMonitorRoomGatewayClientError) as raised:
        GazeboMonitorRoomGatewayClient.exchange(
            client,
            request_id='request-2',
            operation_id='operation-1',
            command='drive',
        )

    assert raised.value.code == (
        'gazebo_gateway_client_configuration_changed'
    )


def test_parallel_clients_have_independent_framed_exchanges(tmp_path):
    """The stateless client does not mix request correlation across calls."""
    path = tmp_path / 'parallel.sock'
    ready = threading.Event()
    failures = []

    def serve():
        try:
            with socket.socket(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            ) as listener:
                listener.bind(str(path))
                os.chmod(path, 0o600)
                listener.listen(4)
                ready.set()
                for _index in range(4):
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
                        _send_response(
                            connection,
                            _valid_response(request),
                        )
        except Exception as error:  # pragma: no cover - helper assertion
            failures.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2.0)
    client = _client(path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                client.exchange,
                request_id=f'request-{index}',
                operation_id=f'operation-{index}',
                command='observe',
            )
            for index in range(4)
        ]
        results = [future.result(timeout=2.0) for future in futures]
    _finish(thread, failures)

    assert {result.request_id for result in results} == {
        f'request-{index}' for index in range(4)
    }
    assert {result.operation_id for result in results} == {
        f'operation-{index}' for index in range(4)
    }
