"""Fail-closed tests for the final Agent-to-Gazebo command boundary."""

import os
from pathlib import Path
import sqlite3

import pytest

import test_gazebo_monitor_room_command_runner as command_tests
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboSimulationExecutionPolicy,
)
from malbut_agent_server.gazebo_monitor_room_command_runner import (
    GazeboMonitorRoomCommandRunnerError,
)
from malbut_agent_server.gazebo_monitor_room_gateway_client import (
    GazeboMonitorRoomGatewayClient,
)


def _assert_drive_stops_before_exchange(
    context,
    tmp_path,
    monkeypatch,
    *,
    suffix,
):
    calls = []

    def forbidden_exchange(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('Gazebo UDS exchange must not be attempted')

    monkeypatch.setattr(
        GazeboMonitorRoomGatewayClient,
        'exchange',
        forbidden_exchange,
    )
    runner = command_tests._runner(
        context,
        tmp_path / 'x.sock',
    )
    with pytest.raises(GazeboMonitorRoomCommandRunnerError):
        runner.drive_once(
            context['confirmation_request_id'],
            f'{suffix}-run',
        )
    assert calls == []


def test_policy_instance_has_exact_slots_and_clock_shadow_cannot_execute(
    tmp_path,
    monkeypatch,
):
    """Instance/class method replacement cannot bypass the deadline."""
    context = command_tests._durably_prepared(
        tmp_path,
        monkeypatch,
        suffix='policy-clock-shadow',
    )
    policy = context['policy']
    context['boot'].now_ns = context['claim'].deadline_boottime_ns

    with pytest.raises(AttributeError):
        object.__setattr__(
            policy,
            'current_boottime_ns',
            lambda: 0,
        )
    with pytest.raises(AttributeError):
        object.__setattr__(
            policy,
            'current_host_boot_id',
            lambda: policy.expected_host_boot_id,
        )
    monkeypatch.setattr(
        GazeboSimulationExecutionPolicy,
        'current_boottime_ns',
        lambda _self: 0,
    )
    _assert_drive_stops_before_exchange(
        context,
        tmp_path,
        monkeypatch,
        suffix='policy-clock-shadow',
    )
    context['store'].close()


def test_policy_trust_root_slot_mutation_fails_before_exchange(
    tmp_path,
    monkeypatch,
):
    """Even object.__setattr__ on a real slot invalidates the outer seal."""
    context = command_tests._durably_prepared(
        tmp_path,
        monkeypatch,
        suffix='policy-root-mutation',
    )
    object.__setattr__(context['policy'], '_boottime_ns', lambda: 0)
    _assert_drive_stops_before_exchange(
        context,
        tmp_path,
        monkeypatch,
        suffix='policy-root-mutation',
    )
    context['store'].close()


@pytest.mark.parametrize(
    'mutation',
    (
        'unlink',
        'mode',
        'foreign_keys',
        'synchronous',
        'database_path',
        'connection',
        'wal_mode',
        'parent_mode',
    ),
)
def test_file_store_drift_never_reaches_gateway_exchange(
    tmp_path,
    monkeypatch,
    mutation,
):
    """Every pinned file/connection/PRAGMA drift fails before UDS I/O."""
    case_dir = tmp_path / mutation
    case_dir.mkdir(mode=0o700)
    context = command_tests._durably_prepared(
        case_dir,
        monkeypatch,
        suffix=f'durability-{mutation}',
    )
    store = context['store']
    database = Path(context['database'])
    original_connection = store._connection
    replacement = None
    restore = None
    if mutation == 'unlink':
        database.unlink()
    elif mutation == 'mode':
        os.chmod(database, 0o666)

        def restore():
            os.chmod(database, 0o600)
    elif mutation == 'foreign_keys':
        original_connection.execute('PRAGMA foreign_keys=OFF')

        def restore():
            original_connection.execute('PRAGMA foreign_keys=ON')
    elif mutation == 'synchronous':
        original_connection.execute('PRAGMA synchronous=NORMAL')

        def restore():
            original_connection.execute('PRAGMA synchronous=FULL')
    elif mutation == 'database_path':
        store.database_path = str(database.with_name('different.sqlite3'))

        def restore():
            store.database_path = str(database)
    elif mutation == 'connection':
        replacement = sqlite3.connect(':memory:', check_same_thread=False)
        store._connection = replacement

        def restore():
            store._connection = original_connection
    elif mutation == 'wal_mode':
        selected = original_connection.execute(
            'PRAGMA journal_mode=DELETE'
        ).fetchone()
        assert str(selected[0]).lower() == 'delete'

        def restore_wal():
            selected_wal = original_connection.execute(
                'PRAGMA journal_mode=WAL'
            ).fetchone()
            assert str(selected_wal[0]).lower() == 'wal'

        restore = restore_wal
    elif mutation == 'parent_mode':
        os.chmod(database.parent, 0o777)

        def restore():
            os.chmod(database.parent, 0o700)
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)

    try:
        _assert_drive_stops_before_exchange(
            context,
            tmp_path,
            monkeypatch,
            suffix=f'durability-{mutation}',
        )
    finally:
        if restore is not None:
            restore()
        if replacement is not None:
            replacement.close()
        store.close()


@pytest.mark.parametrize('suffix', ('-wal', '-shm'))
def test_wal_sidecar_permission_drift_stops_before_exchange(
    tmp_path,
    monkeypatch,
    suffix,
):
    """The active WAL and SHM files remain bound private service files."""
    context = command_tests._durably_prepared(
        tmp_path,
        monkeypatch,
        suffix=f'sidecar-{suffix[1:]}',
    )
    sidecar = Path(f"{context['database']}{suffix}")
    assert sidecar.exists()
    os.chmod(sidecar, 0o666)
    try:
        _assert_drive_stops_before_exchange(
            context,
            tmp_path,
            monkeypatch,
            suffix=f'sidecar-{suffix[1:]}',
        )
    finally:
        os.chmod(sidecar, 0o600)
        context['store'].close()


@pytest.mark.parametrize('suffix', ('-wal', '-shm'))
def test_dangling_sidecar_symlink_stops_before_exchange(
    tmp_path,
    monkeypatch,
    suffix,
):
    """A dangling symlink is present for lstat purposes and never ignored."""
    context = command_tests._durably_prepared(
        tmp_path,
        monkeypatch,
        suffix=f'dangling-{suffix[1:]}',
    )
    sidecar = Path(f"{context['database']}{suffix}")
    backup = Path(f'{sidecar}.held')
    missing = Path(f'{sidecar}.missing')
    assert sidecar.exists()
    sidecar.rename(backup)
    sidecar.symlink_to(missing)
    try:
        assert os.path.lexists(sidecar)
        assert not sidecar.exists()
        _assert_drive_stops_before_exchange(
            context,
            tmp_path,
            monkeypatch,
            suffix=f'dangling-{suffix[1:]}',
        )
    finally:
        sidecar.unlink()
        backup.rename(sidecar)
        context['store'].close()
