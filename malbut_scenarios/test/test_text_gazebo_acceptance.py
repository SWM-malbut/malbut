"""Contracts for the SWM25-133 installed acceptance supervisor."""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from malbut_scenarios import text_gazebo_acceptance as acceptance
from malbut_scenarios.counting_robot_web_proxy import RobotWebProxyCounts
from malbut_scenarios.nav2_goal_status_observer import (
    GoalStatusEvidence,
    Nav2GoalStatusEvidence,
)
from malbut_scenarios.text_gazebo_evidence import CleanupEvidence
from malbut_scenarios.text_gazebo_runtime import (
    LedgerSnapshot,
    ProposalReceipt,
    TextGazeboRuntimeError,
)


_COMMIT = '1' * 40
_DIGEST = '2' * 64
_TREE_DIGEST = '3' * 64
_SOURCE_TREE = Path('/source/tree')
_GOAL_DIGEST = hashlib.sha256(b'one-private-goal').hexdigest()


def _layout() -> acceptance.InstalledLayout:
    return acceptance.InstalledLayout(
        Path('/installed/bin/ros2'),
        Path('/installed/bin/agent'),
        Path('/installed/share/testbed.launch.py'),
        _DIGEST,
        ((
            'malbut_scenarios/example.py',
            Path('/installed/lib/example.py'),
        ),),
    )


def _attestation() -> acceptance.SourceInstallAttestation:
    return acceptance.SourceInstallAttestation(_COMMIT, _TREE_DIGEST)


def _args(**changes) -> argparse.Namespace:
    values = {
        'check': False,
        'run': True,
        'execute_approved_simulation': True,
        'evidence': Path('/private/evidence/receipt.json'),
        'source_commit': _COMMIT,
        'source_tree': _SOURCE_TREE,
        'gui': False,
        'ros_domain_id': 77,
    }
    values.update(changes)
    return argparse.Namespace(**values)


def _proxy_counts(*, started: bool) -> RobotWebProxyCounts:
    count = 1 if started else 0
    return RobotWebProxyCounts(
        bootstrap_count=count,
        status_count=count,
        preview_count=count,
        start_count=count,
        cancel_count=0,
        verified_preview_count=count,
    )


def _preapproval() -> LedgerSnapshot:
    return LedgerSnapshot(
        confirmation_count=1,
        approved_confirmation_count=0,
        confirmation_state='pending',
        confirmation_disposition='pending',
        confirmation_result_code='confirmation_pending',
        robot_action_count=0,
        action_state=None,
        action_result_code=None,
        dispatch_intent_count=0,
        dispatch_state=None,
        dispatch_result_code=None,
        simulation=None,
        physical_authorized=None,
    )


def _known_success() -> LedgerSnapshot:
    return LedgerSnapshot(
        confirmation_count=1,
        approved_confirmation_count=1,
        confirmation_state='resolved',
        confirmation_disposition='approved',
        confirmation_result_code='confirmation_approved',
        robot_action_count=1,
        action_state='SUCCEEDED',
        action_result_code='NAVIGATION_SUCCEEDED',
        dispatch_intent_count=1,
        dispatch_state='TERMINAL',
        dispatch_result_code='NAVIGATION_SUCCEEDED',
        simulation=True,
        physical_authorized=False,
    )


def _nav2_success(*goal_digests: str) -> Nav2GoalStatusEvidence:
    goals = tuple(
        GoalStatusEvidence(
            goal_uuid_sha256=digest,
            latest_status='succeeded',
            latest_status_code=4,
            terminal=True,
            status_observation_count=2,
        )
        for digest in goal_digests
    )
    return Nav2GoalStatusEvidence(
        status_topic='/navigate_to_pose/_action/status',
        distinct_goal_count=len(goals),
        status_message_count=2,
        rejected_status_entry_count=0,
        goals=goals,
    )


def _cleanup(nav2=None) -> acceptance._CleanupResult:
    return acceptance._CleanupResult(
        duration_seconds=0.3,
        evidence=CleanupEvidence(
            completed=True,
            owned_processes_remaining=0,
            ros_nodes_remaining=0,
            owned_sockets_remaining=0,
            forced_termination_count=0,
        ),
        nav2=nav2 or _nav2_success(_GOAL_DIGEST),
    )


def test_check_mode_is_default_safe_and_does_not_enter_runtime(
    monkeypatch,
    capsys,
) -> None:
    """Check reports only digests and never enables a runtime effect."""
    called = []
    monkeypatch.setattr(acceptance, '_installed_layout', _layout)
    monkeypatch.setattr(
        acceptance,
        '_source_attestation',
        lambda *_: _attestation(),
    )
    monkeypatch.setattr(
        acceptance,
        '_run_acceptance',
        lambda *_args: called.append('run'),
    )

    result = acceptance.main([
        '--check',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(_SOURCE_TREE),
    ])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ''
    assert json.loads(captured.out) == {
        'installed_digest': _DIGEST,
        'mode': 'check',
        'nav2_start_count': 0,
        'physical_authorized': False,
        'simulation': True,
        'source_tree_digest': _TREE_DIGEST,
        'status': 'ok',
    }
    assert called == []
    assert '/installed/' not in captured.out


def test_run_mode_prints_only_public_manifest_digest(
    monkeypatch,
    capsys,
) -> None:
    """A successful CLI response excludes evidence paths and run bindings."""
    calls = []

    class Manifest:
        def digest(self):
            return '9' * 64

    def run(args, layout, attestation):
        calls.append((args, layout, attestation))
        return Manifest()

    monkeypatch.setattr(acceptance, '_installed_layout', _layout)
    monkeypatch.setattr(
        acceptance,
        '_source_attestation',
        lambda *_: _attestation(),
    )
    monkeypatch.setattr(acceptance, '_run_acceptance', run)
    evidence = '/private/evidence/private-receipt.json'

    result = acceptance.main([
        '--run',
        '--execute-approved-simulation',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(_SOURCE_TREE),
        '--evidence',
        evidence,
        '--ros-domain-id',
        '77',
    ])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ''
    assert json.loads(captured.out) == {
        'manifest_digest': '9' * 64,
        'mode': 'run',
        'physical_authorized': False,
        'simulation': True,
        'status': 'succeeded',
    }
    assert evidence not in captured.out
    assert len(calls) == 1
    assert calls[0][0].ros_domain_id == 77
    assert calls[0][1] == _layout()
    assert calls[0][2] == _attestation()


@pytest.mark.parametrize(
    'arguments',
    (
        [
            '--run',
            '--source-commit',
            _COMMIT,
            '--source-tree',
            str(_SOURCE_TREE),
        ],
        [
            '--run',
            '--execute-approved-simulation',
            '--source-commit',
            _COMMIT,
            '--source-tree',
            str(_SOURCE_TREE),
            '--evidence',
            '/private/evidence.json',
        ],
        [
            '--check',
            '--source-commit',
            _COMMIT,
            '--source-tree',
            str(_SOURCE_TREE),
            '--execute-approved-simulation',
        ],
        [
            '--check',
            '--source-commit',
            _COMMIT,
            '--source-tree',
            str(_SOURCE_TREE),
            '--gui',
        ],
        [
            '--run',
            '--execute-approved-simulation',
            '--source-commit',
            'A' * 40,
            '--source-tree',
            str(_SOURCE_TREE),
        ],
        [
            '--run',
            '--execute-approved-simulation',
            '--source-commit',
            _COMMIT,
            '--source-tree',
            str(_SOURCE_TREE),
            '--evidence',
            'relative.json',
        ],
        [
            '--run',
            '--execute-approved-simulation',
            '--source-commit',
            _COMMIT,
            '--source-tree',
            str(_SOURCE_TREE),
            '--evidence',
            '/private/evidence.json',
            '--ros-domain-id',
            '0',
        ],
    ),
)
def test_invalid_or_unarmed_modes_fail_before_installed_layout(
    arguments,
    monkeypatch,
    capsys,
) -> None:
    """A missing authority flag and unsafe options fail before discovery."""
    monkeypatch.setattr(
        acceptance,
        '_installed_layout',
        lambda: pytest.fail('installed discovery must not run'),
    )

    result = acceptance.main(arguments)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ''
    assert json.loads(captured.err) == {
        'error_code': 'acceptance_arguments_invalid',
        'status': 'failed',
    }


@pytest.mark.parametrize(
    'source_arguments',
    (
        [],
        ['--source-tree', 'relative/source/tree'],
    ),
)
def test_missing_or_relative_source_tree_fails_before_discovery(
    source_arguments,
    monkeypatch,
    capsys,
) -> None:
    """Source identity must be an explicit absolute path before discovery."""
    monkeypatch.setattr(
        acceptance,
        '_installed_layout',
        lambda: pytest.fail('installed discovery must not run'),
    )
    arguments = [
        '--check',
        '--source-commit',
        _COMMIT,
        *source_arguments,
    ]

    result = acceptance.main(arguments)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ''
    assert json.loads(captured.err) == {
        'error_code': 'acceptance_arguments_invalid',
        'status': 'failed',
    }


@pytest.mark.parametrize('mode', ('check', 'run'))
def test_source_attestation_failure_is_redacted_and_stops_mode(
    mode,
    monkeypatch,
    capsys,
) -> None:
    """Neither check nor run proceeds after source/install mismatch."""
    runtime_calls = []
    private_path = '/private/source/worktree'
    monkeypatch.setattr(acceptance, '_installed_layout', _layout)
    monkeypatch.setattr(
        acceptance,
        'attest_source_install',
        lambda *_args: (_ for _ in ()).throw(RuntimeError(private_path)),
    )
    monkeypatch.setattr(
        acceptance,
        '_run_acceptance',
        lambda *_args: runtime_calls.append('run'),
    )
    arguments = [
        '--' + mode,
        '--source-commit',
        _COMMIT,
        '--source-tree',
        private_path,
    ]
    if mode == 'run':
        arguments.extend([
            '--execute-approved-simulation',
            '--evidence',
            '/private/evidence.json',
            '--ros-domain-id',
            '77',
        ])

    result = acceptance.main(arguments)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ''
    assert json.loads(captured.err) == {
        'error_code': 'source_attestation_failed',
        'status': 'failed',
    }
    assert private_path not in captured.err
    assert runtime_calls == []


def test_source_attestation_receives_exact_installed_bindings(
    monkeypatch,
) -> None:
    """Attestation compares the declared source tree to selected files."""
    calls = []

    def attest(source_tree, commit, bindings):
        calls.append((source_tree, commit, bindings))
        return _attestation()

    monkeypatch.setattr(acceptance, 'attest_source_install', attest)

    result = acceptance._source_attestation(_args(), _layout())

    assert result == _attestation()
    assert calls == [(
        _SOURCE_TREE,
        _COMMIT,
        {
            'malbut_scenarios/example.py': Path(
                '/installed/lib/example.py'
            ),
        },
    )]


def test_parser_failure_is_one_bounded_argument_error(capsys) -> None:
    """Even argparse failures must remain one machine-readable safe code."""
    result = acceptance.main([])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ''
    assert json.loads(captured.err) == {
        'error_code': 'acceptance_arguments_invalid',
        'status': 'failed',
    }


@pytest.mark.parametrize(
    'error,expected',
    (
        (
            acceptance.TextGazeboAcceptanceError(
                'terminal_evidence_invalid'
            ),
            'terminal_evidence_invalid',
        ),
        (
            TextGazeboRuntimeError('ledger_terminal_timeout'),
            'ledger_terminal_timeout',
        ),
        (RuntimeError('private-token-and-path'), 'unexpected_failure'),
        (KeyboardInterrupt(), 'interrupted'),
    ),
)
def test_failure_boundary_returns_only_stable_public_code(
    error,
    expected,
) -> None:
    """Private exception messages never become acceptance output."""
    assert acceptance._safe_code(error) == expected


def test_main_redacts_unexpected_installed_discovery_failure(
    monkeypatch,
    capsys,
) -> None:
    """An unexpected host diagnostic is replaced with a stable code."""
    monkeypatch.setattr(
        acceptance,
        '_installed_layout',
        lambda: (_ for _ in ()).throw(
            RuntimeError('private-token-and-/private/path')
        ),
    )

    result = acceptance.main([
        '--check',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(_SOURCE_TREE),
    ])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ''
    assert json.loads(captured.err) == {
        'error_code': 'unexpected_failure',
        'status': 'failed',
    }
    assert 'private-token' not in captured.err
    assert '/private/path' not in captured.err


def test_goal_set_digest_is_order_independent_and_binds_full_set() -> None:
    """Goal evidence is deterministic without exposing raw goal UUIDs."""
    first = hashlib.sha256(b'private-goal-one').hexdigest()
    second = hashlib.sha256(b'private-goal-two').hexdigest()
    ordered = _nav2_success(first, second)
    reversed_order = _nav2_success(second, first)
    expected_payload = json.dumps(
        sorted((first, second)),
        ensure_ascii=True,
        allow_nan=False,
        separators=(',', ':'),
    ).encode('ascii')

    assert acceptance._goal_set_digest(ordered) == hashlib.sha256(
        expected_payload
    ).hexdigest()
    assert acceptance._goal_set_digest(ordered) == (
        acceptance._goal_set_digest(reversed_order)
    )
    assert acceptance._goal_set_digest(_nav2_success(first)) != (
        acceptance._goal_set_digest(ordered)
    )


@pytest.mark.parametrize(
    'stdout,expected',
    (
        (b'/_ros2cli_4242\n', 0),
        (b'/_ros2cli_4242\n/navigation_server\n', 1),
        (b'/_ros2cli_4242\n/_ros2cli_456\n', 1),
        (b'/_ros2cli_456\n', 1),
        (b'/_ros2cli_4242\n/_ros2cli_4242\n', 1),
        (b'/navigation_server\n/controller_server\n', 2),
        (b'warning text\n/_ros2cli_4242\n', 0),
    ),
)
def test_ros_node_count_subtracts_exactly_one_owned_cli_observer(
    stdout,
    expected,
    monkeypatch,
) -> None:
    """Only this Popen PID's query node is excluded from residue."""
    calls = []

    class Process:
        pid = 4242
        returncode = 0

        def communicate(self, *, timeout):
            assert timeout == 10.0
            return stdout, b''

    def popen(arguments, **options):
        calls.append((arguments, options))
        return Process()

    monkeypatch.setattr(acceptance.subprocess, 'Popen', popen)
    environment = {'ROS_DOMAIN_ID': '77', 'ROS_LOCALHOST_ONLY': '1'}

    assert acceptance._ros_node_count(
        Path('/installed/bin/ros2'), environment
    ) == expected
    arguments, options = calls[0]
    assert arguments == [
        '/installed/bin/ros2',
        'node',
        'list',
        '--no-daemon',
        '--spin-time',
        '1.0',
        '--all',
    ]
    assert options['shell'] is False
    assert options['env'] == environment
    assert options['stdin'] is acceptance.subprocess.DEVNULL
    assert options['stdout'] is acceptance.subprocess.PIPE
    assert options['stderr'] is acceptance.subprocess.DEVNULL


@pytest.mark.parametrize(
    'returncode,stdout',
    (
        (1, b''),
        (0, b'x' * 1_000_001),
        (0, b'\xff'),
    ),
)
def test_ros_node_count_fails_closed_on_untrusted_cli_result(
    returncode,
    stdout,
    monkeypatch,
) -> None:
    """Failed, oversized, and non-UTF-8 ROS output cannot prove cleanup."""
    process = SimpleNamespace(
        pid=4242,
        returncode=returncode,
        communicate=lambda **_options: (stdout, b''),
    )
    monkeypatch.setattr(
        acceptance.subprocess,
        'Popen',
        lambda *_args, **_kwargs: process,
    )

    with pytest.raises(
        acceptance.TextGazeboAcceptanceError,
        match='cleanup_incomplete',
    ):
        acceptance._ros_node_count(Path('/installed/bin/ros2'), {})


def test_ros_node_count_kills_and_reaps_timed_out_query(monkeypatch) -> None:
    """A hung ROS query is killed, reaped, and cannot prove cleanup."""
    events = []

    class Process:
        pid = 4242
        returncode = None

        def communicate(self, *, timeout=None):
            events.append(('communicate', timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired('ros2', timeout)
            self.returncode = -9
            return b'', b''

        def kill(self):
            events.append('kill')

    monkeypatch.setattr(
        acceptance.subprocess,
        'Popen',
        lambda *_args, **_kwargs: Process(),
    )

    with pytest.raises(
        acceptance.TextGazeboAcceptanceError,
        match='cleanup_incomplete',
    ):
        acceptance._ros_node_count(Path('/installed/bin/ros2'), {})

    assert events == [('communicate', 10.0), 'kill', ('communicate', None)]


def test_build_receipt_requires_and_projects_exact_once_evidence() -> None:
    """A successful receipt is derived only from exact ledger and ROS facts."""
    ledger = _known_success()
    proxy = _proxy_counts(started=True)
    successful = acceptance._SuccessfulRun(
        readiness_seconds=2.0,
        execution_seconds=4.0,
        preapproval_goal_count=0,
        final_ledger=ledger,
        proxy_counts=proxy,
    )

    receipt = acceptance._build_receipt(
        args=_args(),
        layout=_layout(),
        attestation=_attestation(),
        run_id='run-' + '3' * 32,
        total_seconds=7.0,
        successful=successful,
        binding={
            'device_id': 'device-private',
            'map_id': 'map-private',
            'map_revision': 'revision-private',
        },
        cleanup=_cleanup(),
    )

    assert receipt.simulation is True
    assert receipt.physical_authorized is False
    assert receipt.source_tree_digest == _TREE_DIGEST
    assert receipt.counts.as_dict() == {
        'agent_proposal_count': 1,
        'confirmation_count': 1,
        'approved_confirmation_count': 1,
        'robot_action_count': 1,
        'dispatch_intent_count': 1,
        'robot_web_start_count': 1,
        'robot_web_verified_target_count': 1,
        'nav2_goal_count': 1,
        'preapproval_nav2_goal_count': 0,
        'terminal_result_count': 1,
        'replay_additional_effect_count': 0,
    }
    rendered = receipt.canonical_json()
    assert 'device-private' not in rendered
    assert 'map-private' not in rendered
    assert 'revision-private' not in rendered


@pytest.mark.parametrize(
    'change',
    (
        {'preapproval_goal_count': 1},
        {'proxy_counts': _proxy_counts(started=False)},
    ),
)
def test_build_receipt_rejects_non_exact_effect_counts(change) -> None:
    """Preapproval effects and missing Robot Web starts cannot claim pass."""
    values = {
        'readiness_seconds': 1.0,
        'execution_seconds': 1.0,
        'preapproval_goal_count': 0,
        'final_ledger': _known_success(),
        'proxy_counts': _proxy_counts(started=True),
    }
    values.update(change)

    with pytest.raises(ValueError, match='success receipt'):
        acceptance._build_receipt(
            args=_args(),
            layout=_layout(),
            attestation=_attestation(),
            run_id='run-' + '4' * 32,
            total_seconds=3.0,
            successful=acceptance._SuccessfulRun(**values),
            binding={
                'device_id': 'device',
                'map_id': 'map',
                'map_revision': 'revision',
            },
            cleanup=_cleanup(),
        )


def test_supervisor_orders_observation_before_runtime_and_effects(
    monkeypatch,
    tmp_path,
) -> None:
    """The observation window precedes Gazebo and approval precedes effects."""
    events = []
    state = {'approved': False}

    class Reservation:
        next_port = 31000

        def __init__(self):
            self.port = Reservation.next_port
            Reservation.next_port += 1

        def release(self):
            events.append(('release', self.port))

    class Observer:
        def snapshot(self):
            if state['approved']:
                return _nav2_success(_GOAL_DIGEST)
            return _nav2_success()

    class Proxy:
        def snapshot(self):
            return _proxy_counts(started=state['approved'])

    class Ledger:
        def snapshot(self, _confirmation_id):
            events.append('ledger.snapshot')
            return _known_success() if state['approved'] else _preapproval()

        def await_known_success(self, _confirmation_id, **_options):
            events.append('ledger.await-success')
            assert state['approved'] is True
            return _known_success()

    class Client:
        def create_conversation(self):
            events.append('client.create')

        def request_navigation(self):
            events.append('client.request')
            return ProposalReceipt('private-confirmation-id')

        def approve_navigation(self):
            events.append('client.approve')
            state['approved'] = True

        def replay_approval(self):
            events.append('client.replay')

        def send_late_approval(self):
            events.append('client.late')

    supervisor = acceptance._AcceptanceSupervisor(
        layout=_layout(),
        run_root=tmp_path,
        domain_id=77,
        gui=False,
        nonce='a' * 32,
    )
    monkeypatch.setattr(acceptance, '_ros_node_count', lambda *_args: 0)
    monkeypatch.setattr(acceptance, 'LoopbackPortReservation', Reservation)
    monkeypatch.setattr(
        acceptance,
        'SQLiteAcceptanceObserver',
        lambda _db: Ledger(),
    )
    monkeypatch.setattr(
        acceptance.time,
        'sleep',
        lambda seconds: events.append(('sleep', seconds)),
    )
    monkeypatch.setattr(
        supervisor,
        '_prepare_fixture',
        lambda: {
            'device_id': 'device',
            'map_id': 'map',
            'map_revision': 'revision',
            'store': '/private/store',
            'user_map_path': '/private/user-map.json',
            'expected_preview_digest': 'd' * 64,
        },
    )

    def start_observer():
        events.append('observer.start-window')
        supervisor._observer = Observer()

    def start_gazebo(_fixture):
        events.append('gazebo.start')

    def readiness(*_args, **_kwargs):
        events.append('gazebo.ready')
        return 1.0

    def start_proxy(fixture):
        assert fixture['expected_preview_digest'] == 'd' * 64
        events.append('proxy.start')
        supervisor._proxy = Proxy()

    def start_agent(_fixture):
        events.append('agent.start')
        return Client()

    def await_nav2():
        events.append('nav2.succeeded')

    monkeypatch.setattr(supervisor, '_start_observer', start_observer)
    monkeypatch.setattr(supervisor, '_start_gazebo', start_gazebo)
    monkeypatch.setattr(acceptance, '_await_robot_web_readiness', readiness)
    monkeypatch.setattr(supervisor, '_start_proxy', start_proxy)
    monkeypatch.setattr(supervisor, '_start_agent', start_agent)
    monkeypatch.setattr(supervisor, '_await_nav2_success', await_nav2)

    successful, binding = supervisor.run()

    prefix = [event for event in events if isinstance(event, str)]
    assert prefix[:14] == [
        'observer.start-window',
        'gazebo.start',
        'gazebo.ready',
        'proxy.start',
        'agent.start',
        'client.create',
        'client.request',
        'ledger.snapshot',
        'client.approve',
        'ledger.await-success',
        'nav2.succeeded',
        'ledger.snapshot',
        'client.replay',
        'client.late',
    ]
    assert prefix[14:] == ['ledger.snapshot'] * 9
    replay_sleeps = [
        event for event in events if event == ('sleep', 0.25)
    ]
    assert len(replay_sleeps) == 8
    assert successful.preapproval_goal_count == 0
    assert successful.proxy_counts.start_count == 1
    assert binding == {
        'device_id': 'device',
        'map_id': 'map',
        'map_revision': 'revision',
    }


def test_supervisor_rejects_effect_that_appears_in_delayed_sample(
    monkeypatch,
    tmp_path,
) -> None:
    """A duplicate effect surfacing after several samples still fails."""
    state = {'approved': False, 'proxy_samples': 0}
    sleeps = []

    class Reservation:
        next_port = 32000

        def __init__(self):
            self.port = Reservation.next_port
            Reservation.next_port += 1

        def release(self):
            return None

    class Observer:
        def snapshot(self):
            return (
                _nav2_success(_GOAL_DIGEST)
                if state['approved']
                else _nav2_success()
            )

    class Proxy:
        def snapshot(self):
            state['proxy_samples'] += 1
            if not state['approved']:
                return _proxy_counts(started=False)
            counts = _proxy_counts(started=True)
            if state['proxy_samples'] == 6:
                return RobotWebProxyCounts(
                    bootstrap_count=counts.bootstrap_count,
                    status_count=counts.status_count,
                    preview_count=counts.preview_count,
                    start_count=2,
                    cancel_count=counts.cancel_count,
                    verified_preview_count=(
                        counts.verified_preview_count
                    ),
                )
            return counts

    class Ledger:
        def snapshot(self, _confirmation_id):
            return _known_success() if state['approved'] else _preapproval()

        def await_known_success(self, _confirmation_id, **_options):
            return _known_success()

    class Client:
        def create_conversation(self):
            return None

        def request_navigation(self):
            return ProposalReceipt('private-confirmation-id')

        def approve_navigation(self):
            state['approved'] = True

        def replay_approval(self):
            return None

        def send_late_approval(self):
            return None

    supervisor = acceptance._AcceptanceSupervisor(
        layout=_layout(),
        run_root=tmp_path,
        domain_id=77,
        gui=False,
        nonce='c' * 32,
    )
    monkeypatch.setattr(acceptance, '_ros_node_count', lambda *_args: 0)
    monkeypatch.setattr(acceptance, 'LoopbackPortReservation', Reservation)
    monkeypatch.setattr(
        acceptance,
        'SQLiteAcceptanceObserver',
        lambda _db: Ledger(),
    )
    monkeypatch.setattr(
        acceptance.time,
        'sleep',
        lambda seconds: sleeps.append(seconds),
    )
    monkeypatch.setattr(
        supervisor,
        '_prepare_fixture',
        lambda: {
            'device_id': 'device',
            'map_id': 'map',
            'map_revision': 'revision',
            'store': '/private/store',
            'user_map_path': '/private/user-map.json',
            'expected_preview_digest': 'e' * 64,
        },
    )

    def start_observer():
        supervisor._observer = Observer()

    def start_proxy(fixture):
        assert fixture['expected_preview_digest'] == 'e' * 64
        supervisor._proxy = Proxy()

    monkeypatch.setattr(supervisor, '_start_observer', start_observer)
    monkeypatch.setattr(supervisor, '_start_gazebo', lambda _fixture: None)
    monkeypatch.setattr(
        acceptance,
        '_await_robot_web_readiness',
        lambda *_args, **_kwargs: 1.0,
    )
    monkeypatch.setattr(supervisor, '_start_proxy', start_proxy)
    monkeypatch.setattr(
        supervisor,
        '_start_agent',
        lambda _fixture: Client(),
    )
    monkeypatch.setattr(supervisor, '_await_nav2_success', lambda: None)

    with pytest.raises(
        acceptance.TextGazeboAcceptanceError,
        match='replay_effect_detected',
    ) as raised:
        supervisor.run()

    assert raised.value.code == 'replay_effect_detected'
    assert state['proxy_samples'] == 6
    assert sleeps == [0.25] * 4


def test_initial_nonempty_domain_keeps_original_failure_code(
    monkeypatch,
) -> None:
    """Pre-existing ROS nodes fail before ownership without cleanup masking."""
    calls = []
    monkeypatch.setattr(
        acceptance,
        '_ros_node_count',
        lambda *_args: calls.append('initial-probe') or 1,
    )
    monkeypatch.setattr(
        acceptance,
        '_await_zero_ros_nodes',
        lambda *_args: pytest.fail(
            'an unverified pre-existing domain is not ours to clean'
        ),
    )

    with pytest.raises(
        acceptance.TextGazeboAcceptanceError,
        match='ros_domain_not_empty',
    ) as raised:
        acceptance._run_acceptance(
            _args(),
            _layout(),
            _attestation(),
        )

    assert raised.value.code == 'ros_domain_not_empty'
    assert calls == ['initial-probe']


def test_cleanup_continues_after_agent_stop_exception(
    monkeypatch,
    tmp_path,
) -> None:
    """One failed owner stop cannot skip the remaining cleanup attempts."""
    events = []

    class Agent:
        def stop(self, **_options):
            events.append('agent.stop')
            raise RuntimeError('private agent cleanup failure')

    class Proxy:
        def close(self, _timeout):
            events.append('proxy.close')
            return True

    class Observer:
        def end_window(self):
            events.append('observer.end-window')
            return _nav2_success()

        def close(self):
            events.append('observer.close')

        def join(self, _timeout):
            events.append('observer.join')
            return True

        def raise_if_failed(self):
            events.append('observer.check')

    class Gazebo:
        def stop(self, **_options):
            events.append('gazebo.stop')
            return SimpleNamespace(
                cleanup_complete=True,
                remaining_process_count=0,
                forced_termination_count=0,
            )

    supervisor = acceptance._AcceptanceSupervisor(
        layout=_layout(),
        run_root=tmp_path,
        domain_id=77,
        gui=False,
        nonce='b' * 32,
    )
    supervisor._agent = Agent()
    supervisor._proxy = Proxy()
    supervisor._observer = Observer()
    supervisor._window_open = True
    supervisor._gazebo = Gazebo()
    supervisor._empty_domain_verified = True
    monkeypatch.setattr(
        acceptance,
        '_await_zero_ros_nodes',
        lambda *_args: events.append('ros.probe') or 0,
    )

    with pytest.raises(
        acceptance.TextGazeboAcceptanceError,
        match='cleanup_incomplete',
    ) as raised:
        supervisor.cleanup()

    assert raised.value.code == 'cleanup_incomplete'
    assert events == [
        'agent.stop',
        'proxy.close',
        'observer.end-window',
        'observer.close',
        'observer.join',
        'observer.check',
        'gazebo.stop',
        'ros.probe',
    ]
    assert supervisor._cleaned.evidence.completed is False


def test_acceptance_source_has_no_broad_kill_direct_nav2_or_db_write() -> None:
    """The supervisor composes owners and observers, not hidden authority."""
    source = inspect.getsource(acceptance)
    tree = ast.parse(source)
    call_names = {
        '.'.join(
            part
            for part in (
                getattr(node.func.value, 'id', None),
                node.func.attr,
            )
            if part
        )
        if isinstance(node.func, ast.Attribute)
        else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    strings = {
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    }

    assert call_names.isdisjoint({
        'os.kill',
        'os.killpg',
        'create_publisher',
        'create_client',
        'ActionClient',
    })
    assert 'NavigateToPose' not in source
    assert 'shell=True' not in source.replace(' ', '')
    assert not any(
        token in value
        for value in strings
        for token in ('killall', 'pkill', 'rm -rf')
    )
    assert not any(
        statement in value
        for value in strings
        for statement in (
            'insert into',
            'update robot_actions',
            'update execution_outbox',
            'delete from',
            'drop table',
        )
    )


def test_ros_node_count_uses_bounded_popen_and_reaps_timeout() -> None:
    """Static AST protects query identity, injection, and timeout cleanup."""
    tree = ast.parse(inspect.getsource(acceptance._ros_node_count))
    popen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'Popen'
    ]
    communicate_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'communicate'
    ]
    kill_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'kill'
    ]

    assert len(popen_calls) == 1
    keywords = {item.arg: item.value for item in popen_calls[0].keywords}
    assert isinstance(keywords['shell'], ast.Constant)
    assert keywords['shell'].value is False
    timeout_values = [
        keyword.value.value
        for call in communicate_calls
        for keyword in call.keywords
        if keyword.arg == 'timeout'
        and isinstance(keyword.value, ast.Constant)
    ]
    assert timeout_values == [10.0]
    assert len(communicate_calls) == 2
    assert len(kill_calls) == 1
