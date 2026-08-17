"""Tests for the same-host trusted robot-state evidence boundary."""

import copy
import json
import os
import socket
import struct
import threading
import time

import pytest

import malbut_agent_server.robot_state as robot_state_module
from malbut_agent_server.robot_state import (
    MAX_ROBOT_STATE_FRAME_BYTES,
    TrustedRobotStateError,
    UnixSocketTrustedRobotStateSource,
    parse_trusted_robot_state_envelope,
)


_BOOT_ID = '11111111-1111-4111-8111-111111111111'
_INSTANCE_A = '22222222-2222-4222-8222-222222222222'
_INSTANCE_B = '33333333-3333-4333-8333-333333333333'
_NOW_NS = 1_000_000_000_000
_NONCE_A = 'a' * 64
_NONCE_B = 'b' * 64


def _envelope(
    nonce=_NONCE_A,
    *,
    instance_id=_INSTANCE_A,
    sequence=1,
    assembled_ns=_NOW_NS - 1_000_000,
    valid_until_ns=_NOW_NS + 1_000_000_000,
    **state_updates,
):
    """Build one strict trusted-collector response."""
    state = {
        'battery_percent': 82.5,
        'navigation_available': True,
        'localization_ok': True,
        'emergency_stop': False,
        'camera_available': True,
        'privacy_mode': False,
        'docked': None,
        'forbidden_zones': [],
    }
    state.update(state_updates)
    evidence = {}
    for name, value in state.items():
        evidence[name] = (
            None
            if value is None
            else {
                'source': f'collector/{name}',
                'received_boottime_ns': str(
                    assembled_ns - 1_000_000
                ),
            }
        )
    return {
        'schema_version': 1,
        'nonce': nonce,
        'source': {
            'kind': 'trusted_ros2',
            'host_boot_id': _BOOT_ID,
            'instance_id': instance_id,
            'sequence': str(sequence),
            'physical_authority': True,
        },
        'binding': {
            'device_id': 'malbut-sim-01',
            'map_id': 'map-home',
            'map_revision': 'grid-7',
        },
        'assembled_at': '2026-08-15T11:00:00+00:00',
        'assembled_boottime_ns': str(assembled_ns),
        'valid_until_boottime_ns': str(valid_until_ns),
        'state': state,
        'evidence': evidence,
    }


def _parse(value, nonce=_NONCE_A, now_ns=_NOW_NS):
    return parse_trusted_robot_state_envelope(
        value,
        expected_nonce=nonce,
        expected_device_id='malbut-sim-01',
        expected_host_boot_id=_BOOT_ID,
        now_boottime_ns=now_ns,
    )


def test_complete_snapshot_preserves_unknown_and_builds_safety_state():
    """Only the monitor_room completeness gate may collapse docked null."""
    evidence = _parse(_envelope())

    assert evidence.docked is None
    assert evidence.is_current(now_boottime_ns=_NOW_NS)
    assert evidence.field_evidence['emergency_stop'] is not None
    with pytest.raises(TypeError):
        evidence.field_evidence['privacy_mode'] = None

    state = evidence.require_complete_for_monitor_room(
        now_boottime_ns=_NOW_NS,
    )
    assert state.battery_percent == 82.5
    assert state.navigation_available is True
    assert state.localization_ok is True
    assert state.emergency_stop is False
    assert state.camera_available is True
    assert state.privacy_mode is False
    assert state.docked is False
    assert state.forbidden_zones == ()


@pytest.mark.parametrize(
    'field',
    [
        'battery_percent',
        'navigation_available',
        'localization_ok',
        'emergency_stop',
        'camera_available',
        'privacy_mode',
        'forbidden_zones',
    ],
)
def test_unknown_required_field_never_becomes_safe_default(field):
    """Every missing monitor_room signal must remain fail-closed."""
    evidence = _parse(_envelope(**{field: None}))

    with pytest.raises(TrustedRobotStateError) as raised:
        evidence.require_complete_for_monitor_room(
            now_boottime_ns=_NOW_NS,
        )
    assert raised.value.code == 'robot_state_incomplete'


def test_nonce_and_audit_time_do_not_change_stable_evidence_digest():
    """Request freshness is checked but excluded from snapshot identity."""
    first = _parse(_envelope())
    second_value = _envelope(_NONCE_B)
    second_value['assembled_at'] = '2026-08-15T11:00:01+00:00'
    second = _parse(second_value, nonce=_NONCE_B)

    assert first.evidence_digest == second.evidence_digest


def test_forbidden_zones_are_canonicalized_before_safety_use():
    """Unicode compatibility forms cannot create a deny-list mismatch."""
    evidence = _parse(
        _envelope(forbidden_zones=['ｌｉｖｉｎｇ＿ｒｏｏｍ'])
    )

    assert evidence.forbidden_zones == ('living_room',)
    state = evidence.require_complete_for_monitor_room(
        now_boottime_ns=_NOW_NS,
    )
    assert state.forbidden_zones == ('living_room',)


@pytest.mark.parametrize(
    ('mutate', 'expected_code'),
    [
        (
            lambda value: value.update({'nonce': 'c' * 64}),
            'robot_state_nonce_mismatch',
        ),
        (
            lambda value: value['binding'].update(
                {'device_id': 'other-device'}
            ),
            'robot_state_binding_mismatch',
        ),
        (
            lambda value: value['source'].update(
                {'host_boot_id': _INSTANCE_B}
            ),
            'robot_state_boot_mismatch',
        ),
        (
            lambda value: value['source'].update(
                {'physical_authority': False}
            ),
            'robot_state_physical_authority_missing',
        ),
        (
            lambda value: value.update(
                {'valid_until_boottime_ns': str(_NOW_NS)}
            ),
            'robot_state_stale',
        ),
    ],
)
def test_nonce_binding_boot_authority_and_deadline_are_exact(
    mutate,
    expected_code,
):
    """Each authority dimension fails with a stable content-free code."""
    value = _envelope()
    mutate(value)

    with pytest.raises(TrustedRobotStateError) as raised:
        _parse(value)
    assert raised.value.code == expected_code
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    'mutate',
    [
        lambda value: value.update({'extra': True}),
        lambda value: value['state'].update(
            {'emergency_stop': 0}
        ),
        lambda value: value['state'].update(
            {'battery_percent': float('nan')}
        ),
        lambda value: value.update(
            {'assembled_boottime_ns': str(_NOW_NS + 1)}
        ),
        lambda value: value['evidence']['camera_available'].update(
            {'received_boottime_ns': str(_NOW_NS + 1)}
        ),
        lambda value: value['evidence'].pop('privacy_mode'),
    ],
)
def test_malformed_or_future_snapshot_is_sanitized(mutate):
    """Malformed snapshots never expose the rejected value in errors."""
    value = _envelope()
    mutate(value)

    with pytest.raises(TrustedRobotStateError) as raised:
        _parse(value)
    assert raised.value.code == 'robot_state_invalid_snapshot'
    assert str(raised.value) == 'trusted robot state is unavailable'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_schema_boolean_is_not_integer_and_full_u64_sequence_is_exact():
    """Wire integers reject booleans while the documented u64 range works."""
    invalid = _envelope()
    invalid['schema_version'] = True
    with pytest.raises(TrustedRobotStateError) as schema:
        _parse(invalid)
    assert schema.value.code == 'robot_state_invalid_snapshot'

    assert _parse(_envelope(sequence=0)).sequence == 0
    maximum = (1 << 64) - 1
    assert _parse(_envelope(sequence=maximum)).sequence == maximum
    overflow = _envelope(sequence=1 << 64)
    with pytest.raises(TrustedRobotStateError) as sequence:
        _parse(overflow)
    assert sequence.value.code == 'robot_state_invalid_snapshot'


def test_clock_failure_is_typed_and_current_check_returns_false(
    monkeypatch,
):
    """CLOCK_BOOTTIME absence or failure never falls back or escapes raw."""
    evidence = _parse(_envelope())
    monkeypatch.delattr(
        robot_state_module.time,
        'CLOCK_BOOTTIME',
        raising=False,
    )
    assert evidence.is_current() is False
    with pytest.raises(TrustedRobotStateError) as parser:
        parse_trusted_robot_state_envelope(
            _envelope(),
            expected_nonce=_NONCE_A,
            expected_device_id='malbut-sim-01',
            expected_host_boot_id=_BOOT_ID,
        )
    assert parser.value.code == 'robot_state_clock_unavailable'
    assert parser.value.__context__ is None

    source = _SequenceSource([_envelope()])
    source._boottime_ns = lambda: (_ for _ in ()).throw(OSError())
    with pytest.raises(TrustedRobotStateError) as reader:
        source.read()
    assert reader.value.code == 'robot_state_clock_unavailable'
    assert reader.value.__context__ is None


class _SequenceSource(UnixSocketTrustedRobotStateSource):
    def __init__(self, values):
        self._values = iter(values)
        self._initialize(
            '/run/malbut/test-state.sock',
            os.getuid(),
            'malbut-sim-01',
            1.0,
            boottime_ns=lambda: _NOW_NS,
            nonce_factory=lambda: _NONCE_A,
            expected_host_boot_id=_BOOT_ID,
        )

    def _read_payload(self, nonce):
        value = copy.deepcopy(next(self._values))
        value['nonce'] = nonce
        return value


def test_sequence_regression_and_same_sequence_mutation_are_rejected():
    """One collector instance has a monotonic immutable sequence stream."""
    changed = _envelope(sequence=2, privacy_mode=True)
    source = _SequenceSource(
        [
            _envelope(sequence=2),
            _envelope(sequence=1),
        ]
    )
    source.read()
    with pytest.raises(TrustedRobotStateError) as regression:
        source.read()
    assert regression.value.code == 'robot_state_replay_regression'

    source = _SequenceSource([_envelope(sequence=2), changed])
    source.read()
    with pytest.raises(TrustedRobotStateError) as conflict:
        source.read()
    assert conflict.value.code == 'robot_state_replay_conflict'


def test_retired_collector_instance_cannot_return_after_restart():
    """Observed instance A to B to A transitions are fenced in-process."""
    source = _SequenceSource(
        [
            _envelope(instance_id=_INSTANCE_A, sequence=10),
            _envelope(instance_id=_INSTANCE_B, sequence=1),
            _envelope(instance_id=_INSTANCE_A, sequence=11),
        ]
    )

    source.read()
    source.read()
    with pytest.raises(TrustedRobotStateError) as retired:
        source.read()
    assert retired.value.code == 'robot_state_retired_instance'


def _recv_exact(connection, size):
    result = b''
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise AssertionError('client request was truncated')
        result += chunk
    return result


def _serve_once(path, ready, response_builder):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        os.chmod(path, 0o660)
        listener.listen(1)
        ready.set()
        connection, _address = listener.accept()
        with connection:
            size = struct.unpack('!I', _recv_exact(connection, 4))[0]
            request = json.loads(_recv_exact(connection, size))
            response = response_builder(request)
            connection.sendall(response)


def _start_server(tmp_path, response_builder):
    path = tmp_path / 'robot-state.sock'
    ready = threading.Event()
    thread = threading.Thread(
        target=_serve_once,
        args=(path, ready, response_builder),
    )
    thread.start()
    assert ready.wait(timeout=2)
    return path, thread


def _valid_response(request):
    payload = json.dumps(
        _envelope(request['nonce']),
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')
    return struct.pack('!I', len(payload)) + payload


def test_unix_socket_transport_checks_peer_nonce_and_bounded_frame(
    tmp_path,
):
    """The fixed UDS one-shot protocol returns one validated snapshot."""
    path, thread = _start_server(tmp_path, _valid_response)
    source = UnixSocketTrustedRobotStateSource._for_test(
        str(path),
        os.getuid(),
        'malbut-sim-01',
        boottime_ns=lambda: _NOW_NS,
        nonce_factory=lambda: _NONCE_A,
        expected_host_boot_id=_BOOT_ID,
    )

    evidence = source.read()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert evidence.device_id == 'malbut-sim-01'
    assert evidence.sequence == 1


def test_valid_length_frame_does_not_require_peer_eof(tmp_path):
    """One complete frame is sufficient even if the peer stays connected."""
    release = threading.Event()

    def serve(path, ready):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            os.chmod(path, 0o660)
            listener.listen(1)
            ready.set()
            connection, _address = listener.accept()
            with connection:
                size = struct.unpack('!I', _recv_exact(connection, 4))[0]
                request = json.loads(_recv_exact(connection, size))
                connection.sendall(_valid_response(request))
                release.wait(timeout=1)

    path = tmp_path / 'open-peer.sock'
    ready = threading.Event()
    thread = threading.Thread(target=serve, args=(path, ready))
    thread.start()
    assert ready.wait(timeout=2)
    source = UnixSocketTrustedRobotStateSource._for_test(
        str(path),
        os.getuid(),
        'malbut-sim-01',
        timeout_seconds=0.05,
        boottime_ns=lambda: _NOW_NS,
        nonce_factory=lambda: _NONCE_A,
        expected_host_boot_id=_BOOT_ID,
    )

    started = time.monotonic()
    evidence = source.read()
    elapsed = time.monotonic() - started
    release.set()
    thread.join(timeout=2)

    assert evidence.sequence == 1
    assert elapsed < 0.2


def test_transport_timeout_is_one_total_deadline_not_per_byte(tmp_path):
    """A slow byte stream cannot multiply the configured timeout."""
    def serve(path, ready):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            os.chmod(path, 0o660)
            listener.listen(1)
            ready.set()
            connection, _address = listener.accept()
            with connection:
                size = struct.unpack('!I', _recv_exact(connection, 4))[0]
                _recv_exact(connection, size)
                for byte in struct.pack('!I', 2) + b'{}':
                    try:
                        connection.sendall(bytes([byte]))
                    except BrokenPipeError:
                        break
                    time.sleep(0.04)

    path = tmp_path / 'drip.sock'
    ready = threading.Event()
    thread = threading.Thread(target=serve, args=(path, ready))
    thread.start()
    assert ready.wait(timeout=2)
    source = UnixSocketTrustedRobotStateSource._for_test(
        str(path),
        os.getuid(),
        'malbut-sim-01',
        timeout_seconds=0.05,
        boottime_ns=lambda: _NOW_NS,
        nonce_factory=lambda: _NONCE_A,
        expected_host_boot_id=_BOOT_ID,
    )

    started = time.monotonic()
    with pytest.raises(TrustedRobotStateError) as raised:
        source.read()
    elapsed = time.monotonic() - started
    thread.join(timeout=2)

    assert raised.value.code == 'robot_state_response_timeout'
    assert elapsed < 0.2


@pytest.mark.parametrize(
    'clock_value',
    [
        lambda: (_ for _ in ()).throw(OverflowError('raw-clock')),
        lambda: float('nan'),
        lambda: True,
    ],
)
def test_transport_clock_failure_is_typed_and_chain_free(
    tmp_path,
    monkeypatch,
    clock_value,
):
    """The total-deadline clock cannot leak or become an implicit timeout."""
    path = tmp_path / 'clock.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        os.chmod(path, 0o660)
        source = UnixSocketTrustedRobotStateSource._for_test(
            str(path),
            os.getuid(),
            'malbut-sim-01',
            boottime_ns=lambda: _NOW_NS,
            nonce_factory=lambda: _NONCE_A,
            expected_host_boot_id=_BOOT_ID,
        )
        monkeypatch.setattr(
            robot_state_module.time,
            'monotonic',
            clock_value,
        )

        with pytest.raises(TrustedRobotStateError) as raised:
            source.read()

    assert raised.value.code == 'robot_state_clock_unavailable'
    assert str(raised.value) == 'trusted robot state is unavailable'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ('response_builder', 'expected_code'),
    [
        (
            lambda _request: struct.pack(
                '!I',
                MAX_ROBOT_STATE_FRAME_BYTES + 1,
            ),
            'robot_state_response_too_large',
        ),
        (
            lambda _request: struct.pack('!I', 5) + b'{}',
            'robot_state_response_truncated',
        ),
        (
            lambda _request: struct.pack('!I', 2) + b'\xff\xfe',
            'robot_state_invalid_utf8',
        ),
        (
            lambda _request: struct.pack('!I', 8) + b'not-json',
            'robot_state_invalid_json',
        ),
        (
            lambda _request: (
                lambda payload: struct.pack('!I', len(payload)) + payload
            )((b'[' * 1000) + b'0' + (b']' * 1000)),
            'robot_state_invalid_json',
        ),
    ],
)
def test_transport_rejects_oversize_truncated_and_invalid_payloads(
    tmp_path,
    response_builder,
    expected_code,
):
    """Framing and decoding failures are typed and content-free."""
    path, thread = _start_server(tmp_path, response_builder)
    source = UnixSocketTrustedRobotStateSource._for_test(
        str(path),
        os.getuid(),
        'malbut-sim-01',
        boottime_ns=lambda: _NOW_NS,
        nonce_factory=lambda: _NONCE_A,
        expected_host_boot_id=_BOOT_ID,
    )

    with pytest.raises(TrustedRobotStateError) as raised:
        source.read()
    thread.join(timeout=2)
    assert raised.value.code == expected_code
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ('payload', 'expected_code'),
    [
        (
            b'{"schema_version":1,"schema_version":1}',
            'robot_state_invalid_json',
        ),
        (
            b'{} trailing',
            'robot_state_invalid_json',
        ),
    ],
)
def test_transport_rejects_duplicate_keys_and_json_extra_data(
    tmp_path,
    payload,
    expected_code,
):
    """The JSON decoder accepts exactly one duplicate-free value."""
    path, thread = _start_server(
        tmp_path,
        lambda _request: struct.pack('!I', len(payload)) + payload,
    )
    source = UnixSocketTrustedRobotStateSource._for_test(
        str(path),
        os.getuid(),
        'malbut-sim-01',
        boottime_ns=lambda: _NOW_NS,
        nonce_factory=lambda: _NONCE_A,
        expected_host_boot_id=_BOOT_ID,
    )

    with pytest.raises(TrustedRobotStateError) as raised:
        source.read()
    thread.join(timeout=2)

    assert raised.value.code == expected_code
    assert raised.value.__context__ is None


def test_transport_rejects_bytes_after_the_declared_frame(tmp_path):
    """A peer cannot smuggle a second value behind one length frame."""
    path, thread = _start_server(
        tmp_path,
        lambda request: _valid_response(request) + b'x',
    )
    source = UnixSocketTrustedRobotStateSource._for_test(
        str(path),
        os.getuid(),
        'malbut-sim-01',
        boottime_ns=lambda: _NOW_NS,
        nonce_factory=lambda: _NONCE_A,
        expected_host_boot_id=_BOOT_ID,
    )

    with pytest.raises(TrustedRobotStateError) as raised:
        source.read()
    thread.join(timeout=2)

    assert raised.value.code == 'robot_state_response_extra_data'
    assert raised.value.__context__ is None


def test_socket_path_type_owner_mode_and_symlink_fail_closed(tmp_path):
    """Path metadata is checked before contacting the configured peer."""
    regular = tmp_path / 'regular'
    regular.write_text('not a socket', encoding='utf-8')
    source = UnixSocketTrustedRobotStateSource(
        str(regular),
        os.getuid(),
        'malbut-sim-01',
    )
    with pytest.raises(TrustedRobotStateError) as not_socket:
        source.read()
    assert not_socket.value.code == 'robot_state_socket_not_socket'

    target = tmp_path / 'target.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(target))
        link = tmp_path / 'link.sock'
        link.symlink_to(target)
        source = UnixSocketTrustedRobotStateSource(
            str(link),
            os.getuid(),
            'malbut-sim-01',
        )
        with pytest.raises(TrustedRobotStateError) as symlink:
            source.read()
        assert symlink.value.code == 'robot_state_socket_path_invalid'

        os.chmod(target, 0o666)
        source = UnixSocketTrustedRobotStateSource(
            str(target),
            os.getuid(),
            'malbut-sim-01',
        )
        with pytest.raises(TrustedRobotStateError) as mode:
            source.read()
        assert mode.value.code == 'robot_state_socket_mode_insecure'


def test_parent_directory_symlink_is_rejected(tmp_path):
    """Every configured path component is bound without symlink traversal."""
    real_directory = tmp_path / 'real'
    real_directory.mkdir()
    linked_directory = tmp_path / 'linked'
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    target = real_directory / 'state.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(target))
        os.chmod(target, 0o660)
        source = UnixSocketTrustedRobotStateSource(
            str(linked_directory / 'state.sock'),
            os.getuid(),
            'malbut-sim-01',
        )

        with pytest.raises(TrustedRobotStateError) as raised:
            source.read()

    assert raised.value.code == 'robot_state_socket_path_invalid'


def test_socket_path_swap_between_check_and_connect_is_rejected(
    tmp_path,
    monkeypatch,
):
    """Connect must use the same component and socket inodes inspected."""
    configured_directory = tmp_path / 'configured'
    configured_directory.mkdir()
    alternate_directory = tmp_path / 'alternate'
    alternate_directory.mkdir()
    configured_path = configured_directory / 'state.sock'
    alternate_path = alternate_directory / 'state.sock'
    retired_directory = tmp_path / 'retired'

    ready = threading.Event()

    def accept_once():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(alternate_path))
            os.chmod(alternate_path, 0o660)
            listener.listen(1)
            ready.set()
            connection, _address = listener.accept()
            connection.close()

    thread = threading.Thread(target=accept_once)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as original:
        original.bind(str(configured_path))
        os.chmod(configured_path, 0o660)
        thread.start()
        assert ready.wait(timeout=2)
        source = UnixSocketTrustedRobotStateSource._for_test(
            str(configured_path),
            os.getuid(),
            'malbut-sim-01',
            boottime_ns=lambda: _NOW_NS,
            nonce_factory=lambda: _NONCE_A,
            expected_host_boot_id=_BOOT_ID,
        )
        original_check = source._check_socket_path
        swapped = False

        def check_and_swap():
            nonlocal swapped
            snapshot = original_check()
            if not swapped:
                configured_directory.rename(retired_directory)
                alternate_directory.rename(configured_directory)
                swapped = True
            return snapshot

        monkeypatch.setattr(source, '_check_socket_path', check_and_swap)

        with pytest.raises(TrustedRobotStateError) as raised:
            source.read()

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert raised.value.code == 'robot_state_socket_path_changed'
    assert raised.value.__context__ is None


def test_socket_owner_and_connected_peer_uid_are_independent(
    tmp_path,
    monkeypatch,
):
    """A matching path owner cannot substitute for the connected process."""
    wrong_uid = os.getuid() + 1
    owner_path = tmp_path / 'owner.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(owner_path))
        os.chmod(owner_path, 0o660)
        owner_source = UnixSocketTrustedRobotStateSource._for_test(
            str(owner_path),
            wrong_uid,
            'malbut-sim-01',
            boottime_ns=lambda: _NOW_NS,
            nonce_factory=lambda: _NONCE_A,
            expected_host_boot_id=_BOOT_ID,
        )

        with pytest.raises(TrustedRobotStateError) as owner:
            owner_source.read()
    assert owner.value.code == 'robot_state_socket_owner_mismatch'

    path = tmp_path / 'peer.sock'
    ready = threading.Event()

    def accept_once():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            os.chmod(path, 0o660)
            listener.listen(1)
            ready.set()
            connection, _address = listener.accept()
            connection.close()

    peer_thread = threading.Thread(target=accept_once)
    peer_thread.start()
    assert ready.wait(timeout=2)
    peer_source = UnixSocketTrustedRobotStateSource._for_test(
        str(path),
        wrong_uid,
        'malbut-sim-01',
        boottime_ns=lambda: _NOW_NS,
        nonce_factory=lambda: _NONCE_A,
        expected_host_boot_id=_BOOT_ID,
    )
    monkeypatch.setattr(peer_source, '_check_socket_path', lambda: None)

    with pytest.raises(TrustedRobotStateError) as peer:
        peer_source.read()
    peer_thread.join(timeout=2)

    assert peer.value.code == 'robot_state_peer_uid_mismatch'
    assert peer.value.__context__ is None


def test_source_serializes_concurrent_reads_and_public_binding_is_read_only():
    """Protect replay state and expose no mutable public config."""
    class CountingSource(_SequenceSource):
        def __init__(self):
            super().__init__([
                _envelope(sequence=1),
                _envelope(sequence=2),
            ])
            self.counter_lock = threading.Lock()
            self.active = 0
            self.maximum_active = 0

        def _read_payload(self, nonce):
            with self.counter_lock:
                self.active += 1
                self.maximum_active = max(
                    self.maximum_active,
                    self.active,
                )
            try:
                time.sleep(0.02)
                return super()._read_payload(nonce)
            finally:
                with self.counter_lock:
                    self.active -= 1

    source = CountingSource()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(source.read()))
        for _index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert source.maximum_active == 1
    assert sorted(item.sequence for item in results) == [1, 2]
    with pytest.raises(AttributeError):
        source.socket_path = '/tmp/other.sock'
    with pytest.raises(AttributeError):
        source.expected_uid = 0


@pytest.mark.parametrize(
    'kwargs',
    [
        {'socket_path': 'relative.sock'},
        {'socket_path': '/tmp/\ud800'},
        {'expected_uid': -1},
        {'expected_device_id': '../device'},
        {'timeout_seconds': 0},
        {'timeout_seconds': 6},
    ],
)
def test_source_configuration_is_strict(kwargs):
    """Partial or ambiguous local trust configuration is rejected."""
    values = {
        'socket_path': '/run/malbut/robot-state.sock',
        'expected_uid': os.getuid(),
        'expected_device_id': 'malbut-sim-01',
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        UnixSocketTrustedRobotStateSource(**values)
