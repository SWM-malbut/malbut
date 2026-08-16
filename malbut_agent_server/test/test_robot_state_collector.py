"""Tests for the ROS-independent trusted robot-state collector core."""

import copy
import json
import os
import socket
import stat
import struct
import threading
import time
from datetime import datetime, timezone

import pytest

import malbut_agent_server.robot_state_collector as collector_module
from malbut_agent_server.robot_state import (
    MAX_ROBOT_STATE_FRAME_BYTES,
    MAX_ROBOT_STATE_SEQUENCE,
    TrustedRobotStateError,
    UnixSocketTrustedRobotStateSource,
    parse_trusted_robot_state_envelope,
)
from malbut_agent_server.robot_state_collector import (
    RobotStateCollectorError,
    RobotStateCollectorServer,
    RobotStateBindingToken,
    RobotStateFieldUpdate,
    RobotStateSnapshotStore,
)


_BOOT_ID = '11111111-1111-4111-8111-111111111111'
_INSTANCE_ID = '22222222-2222-4222-8222-222222222222'
_OTHER_INSTANCE_ID = '33333333-3333-4333-8333-333333333333'
_NOW_NS = 1_000_000_000_000
_NONCE_A = 'a' * 64
_NONCE_B = 'b' * 64
_UTC_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, value=_NOW_NS):
        self.value = value

    def __call__(self):
        return self.value


class _IncrementingClock:
    def __init__(self, value=_NOW_NS):
        self._value = value
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self._value += 1
            return self._value


def _store(
    *,
    clock=None,
    authority=True,
    initial_sequence=0,
    instance_id=_INSTANCE_ID,
    ttl_seconds=1.0,
):
    """Build one deterministic private-seam collector store."""
    return RobotStateSnapshotStore._for_test(
        'malbut-sim-01',
        'map-home',
        'grid-7',
        ttl_seconds=ttl_seconds,
        physical_authority=authority,
        host_boot_id=_BOOT_ID,
        instance_id=instance_id,
        boottime_ns=clock or _Clock(),
        utc_now=lambda: _UTC_NOW,
        initial_sequence=initial_sequence,
    )


def _parse(store, value, *, nonce=_NONCE_A, now_ns=_NOW_NS):
    """Parse a collector envelope through the production Agent parser."""
    return parse_trusted_robot_state_envelope(
        value,
        expected_nonce=nonce,
        expected_device_id=store.device_id,
        expected_host_boot_id=store.host_boot_id,
        now_boottime_ns=now_ns,
    )


def _thread_call(function):
    """Run one blocking server call and retain any raised exception."""
    failures = []

    def target():
        try:
            function()
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, failures


def _request(path, payload, *, declared_size=None, extra=b'', shutdown=True):
    """Send one raw framed collector request and return response bytes."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(2.0)
    try:
        connection.connect(path)
        size = len(payload) if declared_size is None else declared_size
        connection.sendall(struct.pack('!I', size) + payload + extra)
        if shutdown:
            connection.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                chunk = connection.recv(4096)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        connection.close()


def _assert_code(failures, code):
    """Assert one content-free collector failure code."""
    assert len(failures) == 1
    error = failures[0]
    assert isinstance(error, RobotStateCollectorError)
    assert error.code == code
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(error) == 'robot state collector is unavailable'


def test_initial_snapshot_is_stable_unknown_and_parser_compatible():
    """Reads never mutate sequence and unknown never becomes a safe value."""
    clock = _Clock()
    store = _store(clock=clock)

    first = store.snapshot(_NONCE_A)
    clock.value += 100_000_000
    second = store.snapshot(_NONCE_B)
    first_body = copy.deepcopy(first)
    second_body = copy.deepcopy(second)
    first_body.pop('nonce')
    second_body.pop('nonce')

    assert first_body == second_body
    assert store.sequence == 0
    assert set(first['state'].values()) == {None}
    assert set(first['evidence'].values()) == {None}
    evidence = _parse(store, first)
    assert evidence.battery_percent is None
    assert evidence.emergency_stop is None
    with pytest.raises(TrustedRobotStateError) as incomplete:
        evidence.require_complete_for_monitor_room(
            now_boottime_ns=_NOW_NS,
        )
    assert incomplete.value.code == 'robot_state_incomplete'


def test_physical_authority_is_explicit_and_defaults_fail_closed():
    """A pure collector store does not claim provenance by construction."""
    store = _store(authority=False)

    assert store.physical_authority is False
    with pytest.raises(TrustedRobotStateError) as rejected:
        _parse(store, store.snapshot(_NONCE_A))
    assert rejected.value.code == 'robot_state_physical_authority_missing'


def test_atomic_batch_changes_one_sequence_and_exact_replay_does_not():
    """One correlated observation publishes one immutable sequence."""
    clock = _Clock()
    store = _store(clock=clock)
    token = store.binding_token()
    updates = {
        'battery_percent': RobotStateFieldUpdate(
            75.0,
            'battery/status',
            _NOW_NS,
        ),
        'navigation_available': RobotStateFieldUpdate(
            True,
            'nav/status',
            _NOW_NS,
        ),
        'localization_ok': RobotStateFieldUpdate(
            True,
            'localization/status',
            _NOW_NS,
        ),
        'forbidden_zones': RobotStateFieldUpdate(
            ['ｌｉｖｉｎｇ＿ｒｏｏｍ'],
            'map/zones',
            _NOW_NS,
        ),
    }

    assert store.update_fields(updates, binding_token=token) == 1
    first = store.snapshot(_NONCE_A)
    clock.value += 1
    assert store.update_fields(updates, binding_token=token) == 1
    second = store.snapshot(_NONCE_A)

    assert first == second
    assert first['state']['forbidden_zones'] == ['living_room']
    evidence = _parse(store, first, now_ns=clock.value)
    assert evidence.battery_percent == 75.0
    assert evidence.navigation_available is True


def test_map_change_invalidates_scoped_fields_and_old_tokens():
    """Map updates clear scoped evidence and fence A-to-B-to-A replay."""
    clock = _Clock()
    store = _store(clock=clock)
    token_a = store.binding_token()
    store.update_fields(
        {
            'battery_percent': RobotStateFieldUpdate(
                80.0,
                'battery/status',
                _NOW_NS,
            ),
            'navigation_available': RobotStateFieldUpdate(
                True,
                'nav/status',
                _NOW_NS,
            ),
            'localization_ok': RobotStateFieldUpdate(
                True,
                'localization/status',
                _NOW_NS,
            ),
            'forbidden_zones': RobotStateFieldUpdate(
                [],
                'map/zones',
                _NOW_NS,
            ),
        },
        binding_token=token_a,
    )
    clock.value += 100

    assert store.update_binding('map-away', 'grid-8') == 2
    changed = store.snapshot(_NONCE_A)
    assert changed['state']['battery_percent'] == 80.0
    for name in (
        'navigation_available',
        'localization_ok',
        'forbidden_zones',
    ):
        assert changed['state'][name] is None
        assert changed['evidence'][name] is None
    with pytest.raises(RobotStateCollectorError) as old_binding:
        store.update_field(
            'navigation_available',
            True,
            source='nav/status',
            received_boottime_ns=clock.value,
            binding_token=token_a,
        )
    assert old_binding.value.code == 'robot_state_collector_binding_mismatch'

    token_b = store.binding_token()
    clock.value += 100
    store.update_binding('map-home', 'grid-7')
    with pytest.raises(RobotStateCollectorError) as returned_a:
        store.update_field(
            'navigation_available',
            True,
            source='nav/status',
            received_boottime_ns=clock.value,
            binding_token=token_a,
        )
    assert returned_a.value.code == 'robot_state_collector_binding_mismatch'
    assert token_b != store.binding_token()


def test_binding_tokens_are_store_scoped_and_required_for_map_fields():
    """A token from another process cannot authorize a map-scoped update."""
    first = _store()
    second = _store(instance_id=_OTHER_INSTANCE_ID)

    with pytest.raises(RobotStateCollectorError) as missing:
        first.update_field(
            'localization_ok',
            True,
            source='localization/status',
            received_boottime_ns=_NOW_NS,
        )
    assert missing.value.code == 'robot_state_collector_binding_mismatch'
    with pytest.raises(RobotStateCollectorError) as foreign:
        second.update_field(
            'localization_ok',
            True,
            source='localization/status',
            received_boottime_ns=_NOW_NS,
            binding_token=first.binding_token(),
        )
    assert foreign.value.code == 'robot_state_collector_binding_mismatch'
    with pytest.raises(ValueError):
        RobotStateBindingToken(
            device_id=first.device_id,
            instance_id=first.instance_id,
            map_id=first.map_id,
            map_revision=first.map_revision,
            generation=False,
        )


def test_binding_token_validation_fences_read_only_adapters():
    """A no-op adapter must still validate the exact map generation."""
    store = _store()
    current = store.binding_token()

    assert store.validate_binding_token(current) == store.sequence
    store.update_binding('map-away', 'grid-8')

    with pytest.raises(RobotStateCollectorError) as stale:
        store.validate_binding_token(current)
    assert stale.value.code == 'robot_state_collector_binding_mismatch'
    with pytest.raises(TypeError, match='binding_token is required'):
        store.validate_binding_token(None)


def test_receipt_high_water_survives_clear_expiry_and_binding_change():
    """Delayed observations cannot resurrect evidence after a tombstone."""
    clock = _Clock()
    store = _store(clock=clock)
    store.update_field(
        'battery_percent',
        90.0,
        source='battery/status',
        received_boottime_ns=_NOW_NS,
    )
    clock.value += 100
    store.update_field(
        'battery_percent',
        None,
        received_boottime_ns=clock.value,
    )

    with pytest.raises(RobotStateCollectorError) as replay:
        store.update_field(
            'battery_percent',
            90.0,
            source='battery/status',
            received_boottime_ns=_NOW_NS,
        )
    assert replay.value.code == 'robot_state_collector_receipt_regression'
    sequence = store.sequence
    assert store.update_field(
        'battery_percent',
        None,
        received_boottime_ns=clock.value,
    ) == sequence

    token = store.binding_token()
    store.update_field(
        'navigation_available',
        True,
        source='nav/status',
        received_boottime_ns=clock.value,
        binding_token=token,
    )
    clock.value += 100
    store.update_binding('map-away', 'grid-8')
    with pytest.raises(RobotStateCollectorError) as delayed:
        store.update_field(
            'navigation_available',
            True,
            source='nav/status',
            received_boottime_ns=clock.value - 50,
            binding_token=store.binding_token(),
        )
    assert delayed.value.code == 'robot_state_collector_receipt_regression'


def test_expired_fields_are_atomically_unknown_on_next_material_update():
    """A new snapshot cannot retain a receipt past its configured TTL."""
    clock = _Clock()
    store = _store(clock=clock, ttl_seconds=1.0)
    store.update_field(
        'battery_percent',
        50.0,
        source='battery/status',
        received_boottime_ns=_NOW_NS - 500_000_000,
    )
    first = store.snapshot(_NONCE_A)
    assert first['valid_until_boottime_ns'] == str(
        _NOW_NS + 500_000_000
    )

    clock.value += 500_000_000
    store.update_field(
        'camera_available',
        True,
        source='camera/status',
        received_boottime_ns=clock.value,
    )
    second = store.snapshot(_NONCE_A)
    assert second['state']['battery_percent'] is None
    assert second['evidence']['battery_percent'] is None
    assert second['state']['camera_available'] is True


def test_per_field_ttl_bounds_snapshot_and_expires_independently():
    """The shortest trusted field lifetime bounds the whole envelope."""
    clock = _Clock()
    store = _store(clock=clock, ttl_seconds=5.0)
    store.update_fields(
        {
            'battery_percent': RobotStateFieldUpdate(
                60.0,
                'battery/status',
                _NOW_NS,
                valid_for_ns=4_000_000_000,
            ),
            'emergency_stop': RobotStateFieldUpdate(
                False,
                'safety/controller',
                _NOW_NS,
                valid_for_ns=500_000_000,
            ),
        },
    )
    first = store.snapshot(_NONCE_A)
    assert first['valid_until_boottime_ns'] == str(
        _NOW_NS + 500_000_000
    )

    clock.value += 500_000_000
    store.update_field(
        'camera_available',
        True,
        source='camera/status',
        received_boottime_ns=clock.value,
        valid_for_ns=1_000_000_000,
    )
    second = store.snapshot(_NONCE_A)
    assert second['state']['battery_percent'] == 60.0
    assert second['state']['emergency_stop'] is None
    assert second['state']['camera_available'] is True
    assert second['valid_until_boottime_ns'] == str(
        clock.value + 1_000_000_000
    )


@pytest.mark.parametrize(
    'value',
    [
        pytest.param(True, id='bool'),
        pytest.param(500_000_000.0, id='float'),
        pytest.param(0, id='zero'),
        pytest.param(-1, id='negative'),
        pytest.param(10**10000, id='huge-integer'),
        pytest.param(5_000_000_001, id='above-store-bound'),
    ],
)
def test_per_field_ttl_rejects_invalid_or_overlong_values(value):
    """A field cannot outlive the collector's configured trust bound."""
    store = _store(ttl_seconds=5.0)
    original = store.snapshot(_NONCE_A)

    with pytest.raises(ValueError, match='valid_for_ns'):
        store.update_field(
            'battery_percent',
            60.0,
            source='battery/status',
            received_boottime_ns=_NOW_NS,
            valid_for_ns=value,
        )

    assert store.snapshot(_NONCE_A) == original


def test_per_field_ttl_is_bound_to_receipt_and_unknown_has_no_lifetime():
    """TTL mutation conflicts at one receipt and null carries no evidence."""
    store = _store(ttl_seconds=5.0)
    store.update_field(
        'emergency_stop',
        False,
        source='safety/controller',
        received_boottime_ns=_NOW_NS,
        valid_for_ns=500_000_000,
    )
    original = store.snapshot(_NONCE_A)

    with pytest.raises(RobotStateCollectorError) as conflict:
        store.update_field(
            'emergency_stop',
            False,
            source='safety/controller',
            received_boottime_ns=_NOW_NS,
            valid_for_ns=1_000_000_000,
        )
    assert conflict.value.code == 'robot_state_collector_receipt_conflict'
    with pytest.raises(ValueError, match='unknown field evidence'):
        store.update_field(
            'camera_available',
            None,
            received_boottime_ns=_NOW_NS,
            valid_for_ns=500_000_000,
        )
    assert store.snapshot(_NONCE_A) == original


def test_clock_receipt_and_sequence_edges_fail_without_partial_commit():
    """Rollback, replay, conflict, and uint64 overflow are fail-closed."""
    clock = _Clock()
    store = _store(clock=clock)
    original = store.snapshot(_NONCE_A)
    clock.value -= 1
    with pytest.raises(RobotStateCollectorError) as rollback:
        store.update_field(
            'battery_percent',
            20.0,
            source='battery/status',
        )
    assert rollback.value.code == 'robot_state_collector_clock_unavailable'
    assert store.snapshot(_NONCE_A) == original

    maximum = _store(initial_sequence=MAX_ROBOT_STATE_SEQUENCE)
    maximum_body = maximum.snapshot(_NONCE_A)
    with pytest.raises(RobotStateCollectorError) as overflow:
        maximum.update_field(
            'battery_percent',
            20.0,
            source='battery/status',
            received_boottime_ns=_NOW_NS,
        )
    assert overflow.value.code == 'robot_state_collector_sequence_exhausted'
    assert maximum.snapshot(_NONCE_A) == maximum_body


def test_noop_clock_observation_still_fences_later_rollback():
    """A no-op cannot hide a later CLOCK_BOOTTIME regression."""
    clock = _Clock()
    store = _store(clock=clock)
    store.update_field(
        'battery_percent',
        20.0,
        source='battery/status',
        received_boottime_ns=_NOW_NS,
    )
    clock.value += 200
    sequence = store.sequence
    assert store.update_field(
        'battery_percent',
        20.0,
        source='battery/status',
        received_boottime_ns=_NOW_NS,
    ) == sequence
    clock.value -= 50

    with pytest.raises(RobotStateCollectorError) as rollback:
        store.update_field(
            'camera_available',
            True,
            source='camera/status',
            received_boottime_ns=clock.value,
        )
    assert rollback.value.code == 'robot_state_collector_clock_unavailable'


def test_production_identity_bootstrap_failure_is_chain_free(monkeypatch):
    """Raw host identity errors cannot escape the public constructor."""
    def fail_boot():
        raise OSError('/secret/boot/path payload')

    monkeypatch.setattr(collector_module, '_read_boot_id', fail_boot)
    with pytest.raises(RobotStateCollectorError) as raised:
        RobotStateSnapshotStore('device-1', 'map-1', 'revision-1')

    assert raised.value.code == 'robot_state_collector_identity_unavailable'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert '/secret/boot/path' not in str(raised.value)


def test_wire_size_and_uint64_time_overflow_are_rejected_precommit():
    """No committed snapshot can poison the bounded Agent response."""
    store = _store()
    original = store.snapshot(_NONCE_A)
    zones = [('방' * 126) + f'{index:02d}' for index in range(50)]
    with pytest.raises(RobotStateCollectorError) as oversized:
        store.update_field(
            'forbidden_zones',
            zones,
            source='map/zones',
            received_boottime_ns=_NOW_NS,
            binding_token=store.binding_token(),
        )
    assert oversized.value.code == 'robot_state_collector_snapshot_too_large'
    assert store.snapshot(_NONCE_A) == original

    with pytest.raises(RobotStateCollectorError) as time_overflow:
        _store(
            clock=_Clock(MAX_ROBOT_STATE_SEQUENCE),
            ttl_seconds=0.000000001,
        )
    assert time_overflow.value.code == (
        'robot_state_collector_clock_unavailable'
    )


@pytest.mark.parametrize(
    'value',
    [
        pytest.param(True, id='bool'),
        pytest.param(float('nan'), id='nan'),
        pytest.param(float('inf'), id='infinity'),
        pytest.param(10**10000, id='huge-integer'),
    ],
)
def test_extreme_numeric_inputs_are_normalized_without_raw_overflow(
    tmp_path,
    value,
):
    """Huge, boolean, and non-finite numbers stay at typed boundaries."""
    store = _store()
    with pytest.raises(ValueError, match='battery_percent is invalid'):
        store.update_field(
            'battery_percent',
            value,
            source='battery/status',
        )
    with pytest.raises(ValueError, match='collector TTL is invalid'):
        _store(ttl_seconds=value)
    with pytest.raises(ValueError, match='collector timeout is invalid'):
        RobotStateCollectorServer(
            store,
            str(tmp_path / 'numeric.sock'),
            os.geteuid(),
            timeout_seconds=value,
        )


def test_transport_clock_overflow_is_chain_free(monkeypatch, tmp_path):
    """A broken runtime transport clock cannot expose OverflowError."""
    server = RobotStateCollectorServer(
        _store(),
        str(tmp_path / 'clock.sock'),
        os.geteuid(),
    )
    server.start()
    monkeypatch.setattr(collector_module.time, 'monotonic', lambda: 10**10000)

    with pytest.raises(RobotStateCollectorError) as raised:
        server.serve_once()

    assert raised.value.code == 'robot_state_collector_clock_unavailable'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    server.close()


def test_concurrent_reads_and_updates_keep_each_sequence_body_stable():
    """The store lock prevents torn bodies under concurrent access."""
    store = _store(clock=_IncrementingClock())
    failures = []
    bodies = {}
    bodies_lock = threading.Lock()

    def writer(offset):
        try:
            for index in range(25):
                store.update_field(
                    'battery_percent',
                    float((offset + index) % 101),
                    source='battery/status',
                )
        except BaseException as error:
            failures.append(error)

    def reader(offset):
        try:
            for index in range(100):
                snapshot = store.snapshot(
                    f'{offset * 100 + index:064x}'
                )
                sequence = snapshot['source']['sequence']
                snapshot.pop('nonce')
                body = json.dumps(
                    snapshot,
                    sort_keys=True,
                    separators=(',', ':'),
                )
                with bodies_lock:
                    previous = bodies.setdefault(sequence, body)
                    assert previous == body
        except BaseException as error:
            failures.append(error)

    threads = [
        threading.Thread(target=writer, args=(index * 25,))
        for index in range(4)
    ] + [
        threading.Thread(target=reader, args=(index,))
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert store.sequence == 100


def test_real_uds_round_trip_uses_agent_parser_and_exact_socket_mode(tmp_path):
    """The Agent client accepts one nonce-bound local collector response."""
    store = _store()
    path = str(tmp_path / 'robot-state.sock')
    server = RobotStateCollectorServer(
        store,
        path,
        os.geteuid(),
        timeout_seconds=1.0,
    )
    server.start()
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o660
    thread, failures = _thread_call(server.serve_once)
    source = UnixSocketTrustedRobotStateSource._for_test(
        path,
        os.geteuid(),
        store.device_id,
        timeout_seconds=1.0,
        boottime_ns=lambda: _NOW_NS,
        nonce_factory=lambda: _NONCE_A,
        expected_host_boot_id=store.host_boot_id,
    )

    evidence = source.read()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert failures == []
    assert evidence.sequence == 0
    assert evidence.instance_id == store.instance_id
    assert evidence.battery_percent is None
    server.close()
    assert not os.path.lexists(path)


def test_concurrent_uds_clients_receive_one_stable_snapshot(tmp_path):
    """Bounded sequential serving safely handles concurrent local clients."""
    store = _store()
    path = str(tmp_path / 'robot-state.sock')
    server = RobotStateCollectorServer(
        store,
        path,
        os.geteuid(),
        timeout_seconds=1.0,
    )
    server.start()
    server_thread, server_failures = _thread_call(server.serve_forever)
    barrier = threading.Barrier(9)
    results = []
    client_failures = []

    def read(index):
        try:
            source = UnixSocketTrustedRobotStateSource._for_test(
                path,
                os.geteuid(),
                store.device_id,
                timeout_seconds=2.0,
                boottime_ns=lambda: _NOW_NS,
                nonce_factory=lambda: f'{index:064x}',
                expected_host_boot_id=store.host_boot_id,
            )
            barrier.wait(timeout=2.0)
            results.append(source.read())
        except BaseException as error:
            client_failures.append(error)

    clients = [
        threading.Thread(target=read, args=(index,), daemon=True)
        for index in range(1, 9)
    ]
    for client in clients:
        client.start()
    barrier.wait(timeout=2.0)
    for client in clients:
        client.join(timeout=3.0)
    server.close()
    server_thread.join(timeout=1.0)

    assert all(not client.is_alive() for client in clients)
    assert client_failures == []
    assert len(results) == 8
    assert {result.sequence for result in results} == {0}
    assert len({result.evidence_digest for result in results}) == 1
    assert not server_thread.is_alive()
    assert server_failures == []


@pytest.mark.parametrize(
    ('payload', 'extra', 'expected_code'),
    [
        (b'\xff', b'', 'robot_state_collector_request_invalid_utf8'),
        (b'{', b'', 'robot_state_collector_request_invalid_json'),
        (
            (
                b'{"schema_version":1,"schema_version":1,"nonce":"'
                + (b'a' * 64)
                + b'"}'
            ),
            b'',
            'robot_state_collector_request_invalid_json',
        ),
        (
            json.dumps(
                {'schema_version': True, 'nonce': _NONCE_A}
            ).encode(),
            b'',
            'robot_state_collector_request_invalid',
        ),
        (
            json.dumps(
                {'schema_version': 1, 'nonce': _NONCE_A}
            ).encode(),
            b'x',
            'robot_state_collector_request_extra_data',
        ),
    ],
)
def test_malformed_requests_are_closed_with_chain_free_errors(
    tmp_path,
    payload,
    extra,
    expected_code,
):
    """Invalid UTF-8, JSON, shape, and trailing bytes expose no payload."""
    path = str(tmp_path / 'robot-state.sock')
    server = RobotStateCollectorServer(
        _store(),
        path,
        os.geteuid(),
        timeout_seconds=0.5,
    )
    server.start()
    thread, failures = _thread_call(server.serve_once)

    response = _request(path, payload, extra=extra)
    thread.join(timeout=1.0)

    assert response == b''
    assert not thread.is_alive()
    _assert_code(failures, expected_code)
    server.close()


def test_oversize_wrong_uid_and_timeout_requests_fail_closed(tmp_path):
    """Frame bounds, peer credentials, and total deadlines are enforced."""
    oversize_path = str(tmp_path / 'oversize.sock')
    oversize = RobotStateCollectorServer(
        _store(),
        oversize_path,
        os.geteuid(),
        timeout_seconds=0.5,
    )
    oversize.start()
    thread, failures = _thread_call(oversize.serve_once)
    assert _request(
        oversize_path,
        b'',
        declared_size=MAX_ROBOT_STATE_FRAME_BYTES + 1,
    ) == b''
    thread.join(timeout=1.0)
    _assert_code(failures, 'robot_state_collector_request_too_large')
    oversize.close()

    uid_path = str(tmp_path / 'uid.sock')
    wrong_uid = RobotStateCollectorServer(
        _store(),
        uid_path,
        os.geteuid() + 1,
        timeout_seconds=0.5,
    )
    wrong_uid.start()
    thread, failures = _thread_call(wrong_uid.serve_once)
    assert _request(uid_path, b'{}') == b''
    thread.join(timeout=1.0)
    _assert_code(failures, 'robot_state_collector_peer_uid_mismatch')
    wrong_uid.close()

    timeout_path = str(tmp_path / 'timeout.sock')
    timeout_server = RobotStateCollectorServer(
        _store(),
        timeout_path,
        os.geteuid(),
        timeout_seconds=0.05,
    )
    timeout_server.start()
    thread, failures = _thread_call(timeout_server.serve_once)
    idle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    idle.connect(timeout_path)
    thread.join(timeout=1.0)
    idle.close()
    _assert_code(failures, 'robot_state_collector_request_timeout')
    timeout_server.close()


def test_paths_reject_symlinks_insecure_parents_and_existing_nodes(tmp_path):
    """Start never follows parents or removes an existing filesystem node."""
    real = tmp_path / 'real'
    real.mkdir(mode=0o700)
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    symlink_server = RobotStateCollectorServer(
        _store(),
        str(link / 'state.sock'),
        os.geteuid(),
    )
    with pytest.raises(RobotStateCollectorError) as symlinked:
        symlink_server.start()
    assert symlinked.value.code == (
        'robot_state_collector_socket_parent_invalid'
    )

    insecure = tmp_path / 'insecure'
    insecure.mkdir(mode=0o700)
    insecure.chmod(0o770)
    try:
        insecure_server = RobotStateCollectorServer(
            _store(),
            str(insecure / 'state.sock'),
            os.geteuid(),
        )
        with pytest.raises(RobotStateCollectorError) as unsafe:
            insecure_server.start()
        assert unsafe.value.code == (
            'robot_state_collector_socket_parent_insecure'
        )
    finally:
        insecure.chmod(0o700)

    existing_path = tmp_path / 'existing.sock'
    existing_path.write_text('supervisor-owned residue')
    existing_server = RobotStateCollectorServer(
        _store(),
        str(existing_path),
        os.geteuid(),
    )
    with pytest.raises(RobotStateCollectorError) as existing:
        existing_server.start()
    assert existing.value.code == 'robot_state_collector_socket_exists'
    assert existing_path.read_text() == 'supervisor-owned residue'

    stale_path = tmp_path / 'stale.sock'
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(stale_path))
    stale.close()
    stale_inode = os.lstat(stale_path).st_ino
    stale_server = RobotStateCollectorServer(
        _store(),
        str(stale_path),
        os.geteuid(),
    )
    with pytest.raises(RobotStateCollectorError) as residue:
        stale_server.start()
    assert residue.value.code == 'robot_state_collector_socket_exists'
    assert os.lstat(stale_path).st_ino == stale_inode
    os.unlink(stale_path)


def test_socket_path_surrogate_is_normalized_to_generic_value_error():
    """Filesystem encoding failures never expose UnicodeEncodeError."""
    with pytest.raises(ValueError, match='collector socket path is invalid'):
        RobotStateCollectorServer(
            _store(),
            '/tmp/\ud800',
            os.geteuid(),
        )


def test_close_unlinks_only_own_inode_and_stops_active_or_idle_serve(tmp_path):
    """Close is prompt, idempotent, and preserves replacement path nodes."""
    active_path = str(tmp_path / 'active.sock')
    active = RobotStateCollectorServer(
        _store(),
        active_path,
        os.geteuid(),
        timeout_seconds=5.0,
    )
    active.start()
    thread, failures = _thread_call(active.serve_once)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(active_path)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with active._lifecycle_lock:
            if active._active_connections:
                break
        time.sleep(0.005)
    active.close()
    thread.join(timeout=0.5)
    client.close()
    assert not thread.is_alive()
    _assert_code(failures, 'robot_state_collector_closed')
    active.close()

    idle_path = str(tmp_path / 'idle.sock')
    idle = RobotStateCollectorServer(
        _store(),
        idle_path,
        os.geteuid(),
        timeout_seconds=5.0,
    )
    idle.start()
    thread, failures = _thread_call(idle.serve_forever)
    time.sleep(0.02)
    idle.close()
    thread.join(timeout=0.5)
    assert not thread.is_alive()
    assert failures == []

    replace_path = tmp_path / 'replace.sock'
    moved_path = tmp_path / 'moved.sock'
    replacement = RobotStateCollectorServer(
        _store(),
        str(replace_path),
        os.geteuid(),
    )
    replacement.start()
    os.rename(replace_path, moved_path)
    replace_path.write_text('replacement')
    replacement.close()
    assert replace_path.read_text() == 'replacement'
    os.unlink(moved_path)
