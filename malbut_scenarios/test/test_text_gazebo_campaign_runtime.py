"""Contracts for the installed SWM25-133 campaign runner adapter."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from malbut_scenarios import text_gazebo_campaign_runtime as runtime
from malbut_scenarios.owned_process import (
    ProcessCleanupEvidence,
    ProcessOutputEvidence,
)
from malbut_scenarios.text_gazebo_evidence import (
    CleanupEvidence,
    ConfirmationState,
    DispatchState,
    EvidenceCounts,
    EvidenceDurations,
    NavigationState,
    ReadinessState,
    RobotActionState,
    StableStates,
    TextGazeboEvidenceManifest,
    TextGazeboEvidenceReceipt,
    write_evidence_manifest,
)


_COMMIT = '1' * 40
_SOURCE_DIGEST = '2' * 64
_INSTALLED_DIGEST = '3' * 64
_EMPTY_DIGEST = hashlib.sha256(b'').hexdigest()


class _Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class _FakePopenOwner:
    instances = []
    on_start = None
    exit_code = 0
    never_exits = False
    cleanup_complete = True
    forced = 0
    overflowed = False
    captured_payload = b''
    start_error = False

    def __init__(
        self,
        label,
        argv,
        *,
        cwd,
        environment,
        maximum_output_bytes,
    ):
        self.label = label
        self.argv = tuple(argv)
        self.cwd = cwd
        self.environment = dict(environment)
        self.maximum_output_bytes = maximum_output_bytes
        self.started = False
        self.stopped = False
        type(self).instances.append(self)

    @property
    def returncode(self):
        if not self.started or self.never_exits:
            return None
        return self.exit_code

    def start(self):
        if self.start_error:
            raise RuntimeError('/private/start/failure')
        self.started = True
        callback = type(self).on_start
        if callback is not None:
            callback(self)
        clock = getattr(self, 'clock', None)
        if clock is not None:
            clock.value += 5.0

    def require_running(self):
        if self.overflowed:
            raise runtime.OwnedProcessError('process_output_overflow')

    def stop(self, **_kwargs):
        self.stopped = True
        return ProcessCleanupEvidence(
            process_started=self.started,
            remaining_process_count=(0 if self.cleanup_complete else 1),
            forced_termination_count=self.forced,
            output_collector_stopped=self.cleanup_complete,
            output_overflowed=self.overflowed,
            cleanup_complete=self.cleanup_complete,
        )

    def output_evidence(self):
        if self.label == 'campaign-check-runner':
            return runtime._CapturedOutputEvidence(
                payload=self.captured_payload,
                bytes_observed=len(self.captured_payload),
                digest=hashlib.sha256(self.captured_payload).hexdigest(),
                overflowed=self.overflowed,
            )
        return ProcessOutputEvidence(
            bytes_observed=17,
            bytes_hashed=17,
            digest='9' * 64,
            overflowed=self.overflowed,
        )


class _FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _IdentityFailureProcess:
    pid = 424242

    def __init__(self):
        self.stdout = _FakePipe()
        self.returncode = None
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout):
        assert timeout == 5
        self.waited = True
        return self.returncode


@pytest.fixture(autouse=True)
def reset_fake_owner():
    _FakePopenOwner.instances = []
    _FakePopenOwner.on_start = None
    _FakePopenOwner.exit_code = 0
    _FakePopenOwner.never_exits = False
    _FakePopenOwner.cleanup_complete = True
    _FakePopenOwner.forced = 0
    _FakePopenOwner.overflowed = False
    _FakePopenOwner.captured_payload = b''
    _FakePopenOwner.start_error = False


def _layout(tmp_path):
    prefix = tmp_path / 'install'
    executable = (
        prefix / 'lib/malbut_scenarios/run_text_gazebo_acceptance'
    )
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    executable.chmod(0o755)
    source = tmp_path / 'source'
    source.mkdir()
    evidence_parent = tmp_path / 'evidence'
    evidence_parent.mkdir(mode=0o700)
    evidence_parent.chmod(0o700)
    return prefix, source, evidence_parent


def _config(prefix, source, **changes):
    values = {
        'installed_prefix': prefix,
        'source_tree': source,
        'source_commit': _COMMIT,
        'source_tree_digest': _SOURCE_DIGEST,
        'installed_digest': _INSTALLED_DIGEST,
        'timeout_seconds': 2.0,
        'poll_interval_seconds': 0.1,
    }
    values.update(changes)
    return runtime.TextGazeboCampaignRunnerConfig(**values)


def _check_config(prefix, source, **changes):
    values = {
        'installed_prefix': prefix,
        'source_tree': source,
        'source_commit': _COMMIT,
        'timeout_seconds': 2.0,
        'poll_interval_seconds': 0.1,
    }
    values.update(changes)
    return runtime.TextGazeboCampaignCheckConfig(**values)


def _request(evidence_parent, **changes):
    values = {
        'ros_domain_id': 71,
        'evidence_path': evidence_parent / 'case.json',
        'gui': False,
    }
    values.update(changes)
    return runtime.TextGazeboCampaignRunRequest(**values)


def _manifest(**changes):
    values = {
        'run_id': 'run-' + '4' * 32,
        'commit': _COMMIT,
        'source_tree_digest': _SOURCE_DIGEST,
        'installed_digest': _INSTALLED_DIGEST,
        'goal_set_digest': '5' * 64,
        'runtime_binding_digest': '6' * 64,
        'states': StableStates(
            readiness=ReadinessState.READY,
            confirmation=ConfirmationState.APPROVED,
            robot_action=RobotActionState.SUCCEEDED,
            dispatch=DispatchState.TERMINAL,
            navigation=NavigationState.SUCCEEDED,
        ),
        'counts': EvidenceCounts(
            agent_proposal_count=1,
            confirmation_count=1,
            approved_confirmation_count=1,
            robot_action_count=1,
            dispatch_intent_count=1,
            robot_web_start_count=1,
            robot_web_verified_target_count=1,
            nav2_goal_count=1,
            preapproval_nav2_goal_count=0,
            terminal_result_count=1,
            replay_additional_effect_count=0,
        ),
        'durations': EvidenceDurations(
            readiness_seconds=1.0,
            execution_seconds=2.0,
            cleanup_seconds=1.0,
            total_seconds=4.0,
        ),
        'cleanup': CleanupEvidence(
            completed=True,
            owned_processes_remaining=0,
            ros_nodes_remaining=0,
            owned_sockets_remaining=0,
            forced_termination_count=0,
        ),
    }
    values.update(changes)
    return TextGazeboEvidenceManifest(
        TextGazeboEvidenceReceipt(**values)
    )


def _summary(manifest=None):
    selected = manifest if manifest is not None else _manifest()
    return runtime.parse_child_manifest(
        (selected.canonical_json() + '\n').encode('utf-8')
    )


def _check_payload(**changes):
    value = {
        'installed_digest': _INSTALLED_DIGEST,
        'mode': 'check',
        'nav2_start_count': 0,
        'physical_authorized': False,
        'simulation': True,
        'source_tree_digest': _SOURCE_DIGEST,
        'status': 'ok',
    }
    value.update(changes)
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True) + '\n'
    ).encode('utf-8')


def _runner(config, clock):
    def owner_factory(*args, **kwargs):
        owner = _FakePopenOwner(*args, **kwargs)
        owner.clock = clock
        return owner

    return runtime.InstalledTextGazeboAcceptanceRunner(
        config,
        owner_factory=owner_factory,
        capture_owner_factory=owner_factory,
        environment_source=lambda: {
            'AMENT_PREFIX_PATH': str(config.installed_prefix),
            'PATH': '/usr/bin',
            'LANG': 'C.UTF-8',
            'OPENAI_API_KEY': 'must-not-cross-boundary',
        },
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def _interrupting_runner(config):
    def owner_factory(*args, **kwargs):
        return _FakePopenOwner(*args, **kwargs)

    def interrupted_sleep(_seconds):
        raise KeyboardInterrupt

    return runtime.InstalledTextGazeboAcceptanceRunner(
        config,
        owner_factory=owner_factory,
        capture_owner_factory=owner_factory,
        environment_source=lambda: {
            'AMENT_PREFIX_PATH': str(config.installed_prefix),
            'LANG': 'C.UTF-8',
            'PATH': '/usr/bin',
        },
        monotonic=lambda: 0.0,
        sleep=interrupted_sleep,
    )


def test_construction_is_zero_io_and_repr_hides_paths(
    tmp_path,
    monkeypatch,
):
    prefix = (tmp_path / 'missing-install').absolute()
    source = (tmp_path / 'missing-source').absolute()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('construction performed I/O')

    monkeypatch.setattr(runtime.os, 'lstat', forbidden)
    config = _config(prefix, source)
    runner = runtime.InstalledTextGazeboAcceptanceRunner(
        config,
        owner_factory=forbidden,
        environment_source=forbidden,
    )

    assert calls == []
    assert str(prefix) not in repr(config)
    assert str(source) not in repr(config)
    assert repr(runner) == (
        'InstalledTextGazeboAcceptanceRunner(configured=True)'
    )


@pytest.mark.parametrize(
    'changes',
    (
        {'installed_prefix': Path('relative')},
        {'source_tree': Path('relative')},
        {'source_commit': 'a' * 39},
        {'source_commit': 'A' * 40},
        {'source_tree_digest': 'g' * 64},
        {'installed_digest': '1' * 63},
        {'timeout_seconds': True},
        {'timeout_seconds': 901},
        {'maximum_output_bytes': 4095},
        {'poll_interval_seconds': 0},
    ),
)
def test_config_rejects_unbounded_or_ambiguous_values(tmp_path, changes):
    prefix = (tmp_path / 'install').absolute()
    source = (tmp_path / 'source').absolute()

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_config_invalid$',
    ):
        _config(prefix, source, **changes)


@pytest.mark.parametrize(
    'changes',
    (
        {'ros_domain_id': 0},
        {'ros_domain_id': 101},
        {'ros_domain_id': True},
        {'evidence_path': Path('relative.json')},
        {'gui': 1},
    ),
)
def test_request_requires_explicit_bounded_authority(tmp_path, changes):
    evidence = (tmp_path / 'evidence').absolute()

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_config_invalid$',
    ):
        _request(evidence, **changes)


def test_success_uses_exact_installed_argv_sanitized_env_and_v2_evidence(
    tmp_path,
):
    prefix, source, evidence_parent = _layout(tmp_path)
    config = _config(prefix, source)
    request = _request(evidence_parent)
    clock = _Clock()
    manifest = _manifest()
    _FakePopenOwner.on_start = lambda _owner: write_evidence_manifest(
        request.evidence_path,
        manifest,
    )

    result = _runner(config, clock).run(request)
    owner = _FakePopenOwner.instances[0]

    assert owner.argv == (
        str(
            prefix
            / 'lib/malbut_scenarios/run_text_gazebo_acceptance'
        ),
        '--run',
        '--execute-approved-simulation',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(source),
        '--ros-domain-id',
        '71',
        '--evidence',
        str(request.evidence_path),
    )
    assert owner.cwd == source
    assert owner.environment['ROS_DOMAIN_ID'] == '71'
    assert owner.environment['ROS_LOCALHOST_ONLY'] == '1'
    assert 'OPENAI_API_KEY' not in owner.environment
    assert owner.started is True
    assert owner.stopped is True
    assert result.manifest_digest == manifest.digest()
    assert result.receipt_digest == manifest.receipt_digest
    assert result.commit == _COMMIT
    assert result.source_tree_digest == _SOURCE_DIGEST
    assert result.installed_digest == _INSTALLED_DIGEST
    assert result.exact_success is True
    assert result.cleanup_complete is True
    assert result.forced_termination_count == 0
    assert result.simulation is True
    assert result.physical_authorized is False
    assert result.child_manifest == _summary(manifest)
    assert result.child_manifest.total_duration_seconds == 4.0
    assert result.child_output_bytes == 17
    assert str(source) not in repr(result)
    assert str(request.evidence_path) not in repr(result)


def test_check_uses_exact_non_actuating_argv_and_strict_public_output(
    tmp_path,
):
    prefix, source, _ = _layout(tmp_path)
    config = _config(prefix, source)
    _FakePopenOwner.captured_payload = _check_payload()

    result = _runner(config, _Clock()).check()
    owner = _FakePopenOwner.instances[0]

    assert owner.argv == (
        str(
            prefix
            / 'lib/malbut_scenarios/run_text_gazebo_acceptance'
        ),
        '--check',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(source),
    )
    assert '--run' not in owner.argv
    assert '--execute-approved-simulation' not in owner.argv
    assert '--evidence' not in owner.argv
    assert '--ros-domain-id' not in owner.argv
    assert owner.environment['ROS_LOCALHOST_ONLY'] == '1'
    assert 'OPENAI_API_KEY' not in owner.environment
    assert result.commit == _COMMIT
    assert result.source_tree_digest == _SOURCE_DIGEST
    assert result.installed_digest == _INSTALLED_DIGEST
    assert result.nav2_start_count == 0
    assert result.simulation is True
    assert result.physical_authorized is False
    assert str(source) not in repr(result)


def test_check_config_discovers_digests_before_run_config_exists(tmp_path):
    prefix, source, evidence_parent = _layout(tmp_path)
    config = _check_config(prefix, source)
    _FakePopenOwner.captured_payload = _check_payload()
    runner = _runner(config, _Clock())

    result = runner.check()

    assert result.source_tree_digest == _SOURCE_DIGEST
    assert result.installed_digest == _INSTALLED_DIGEST
    assert _SOURCE_DIGEST not in repr(config)
    assert _INSTALLED_DIGEST not in repr(config)
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_config_invalid$',
    ):
        runner.run(_request(evidence_parent))


def test_default_check_owner_runs_one_bounded_non_gazebo_child(tmp_path):
    prefix, source, _ = _layout(tmp_path)
    executable = (
        prefix / 'lib/malbut_scenarios/run_text_gazebo_acceptance'
    )
    payload = _check_payload()
    executable.write_text(
        '#!/usr/bin/python3\n'
        'import sys\n'
        f'sys.stdout.buffer.write({payload!r})\n',
        encoding='utf-8',
    )
    executable.chmod(0o755)
    config = _check_config(prefix, source)
    runner = runtime.InstalledTextGazeboAcceptanceRunner(
        config,
        owner_factory=_FakePopenOwner,
        environment_source=lambda: {
            'AMENT_PREFIX_PATH': str(prefix),
            'PATH': '/usr/bin',
            'LANG': 'C.UTF-8',
        },
    )

    result = runner.check()

    assert result.nav2_start_count == 0
    assert result.child_output_bytes == len(payload)
    assert result.child_output_digest == hashlib.sha256(payload).hexdigest()
    assert _FakePopenOwner.instances == []


@pytest.mark.parametrize('phase', ('create', 'cleanup'))
def test_check_temp_lifecycle_failure_is_stable_and_content_free(
    tmp_path,
    phase,
):
    prefix, source, _ = _layout(tmp_path)
    private_temp = tmp_path / 'private-check-temp'
    private_temp.mkdir(mode=0o700)

    class CleanupFailure:
        def __enter__(self):
            return str(private_temp)

        def __exit__(self, *_args):
            raise OSError('/private/temp/cleanup')

    def temporary_factory(**_kwargs):
        if phase == 'create':
            raise OSError('/private/temp/create')
        return CleanupFailure()

    _FakePopenOwner.captured_payload = _check_payload()
    runner = runtime.InstalledTextGazeboAcceptanceRunner(
        _check_config(prefix, source),
        owner_factory=_FakePopenOwner,
        capture_owner_factory=_FakePopenOwner,
        temporary_directory_factory=temporary_factory,
        environment_source=lambda: {'PATH': '/usr/bin'},
    )

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_unexpected_failure$',
    ) as raised:
        runner.check()

    assert raised.value.__cause__ is None
    assert 'private' not in str(raised.value)


@pytest.mark.parametrize(
    'payload',
    (
        b'private child traceback\n',
        _check_payload(status='failed'),
        _check_payload(nav2_start_count=1),
        _check_payload(source_tree_digest='7' * 64),
        _check_payload(installed_digest='7' * 64),
        _check_payload(extra='not-allowed'),
        _check_payload() + b'private extra output\n',
    ),
)
def test_check_rejects_malformed_actuating_or_unbound_output(
    tmp_path,
    payload,
):
    prefix, source, _ = _layout(tmp_path)
    _FakePopenOwner.captured_payload = payload

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ) as raised:
        _runner(_config(prefix, source), _Clock()).check()

    assert 'private' not in str(raised.value)


def test_gui_is_an_explicit_single_flag(tmp_path):
    prefix, source, evidence_parent = _layout(tmp_path)
    config = _config(prefix, source)
    request = _request(evidence_parent, gui=True)
    _FakePopenOwner.on_start = lambda _owner: write_evidence_manifest(
        request.evidence_path,
        _manifest(),
    )

    _runner(config, _Clock()).run(request)

    owner = _FakePopenOwner.instances[0]
    assert owner.argv[-1] == '--gui'
    assert owner.argv.count('--gui') == 1


def test_existing_evidence_fails_before_child_start(tmp_path):
    prefix, source, evidence_parent = _layout(tmp_path)
    request = _request(evidence_parent)
    request.evidence_path.write_text('private-old-data', encoding='utf-8')

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ):
        _runner(_config(prefix, source), _Clock()).run(request)

    assert _FakePopenOwner.instances == []


def test_symlink_executable_and_non_private_parent_fail_before_start(
    tmp_path,
):
    prefix, source, evidence_parent = _layout(tmp_path)
    executable = (
        prefix / 'lib/malbut_scenarios/run_text_gazebo_acceptance'
    )
    real = executable.with_name('real-runner')
    executable.rename(real)
    executable.symlink_to(real)

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_install_invalid$',
    ):
        _runner(_config(prefix, source), _Clock()).run(
            _request(evidence_parent)
        )
    assert _FakePopenOwner.instances == []

    executable.unlink()
    real.rename(executable)
    evidence_parent.chmod(0o755)
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ):
        _runner(_config(prefix, source), _Clock()).run(
            _request(evidence_parent)
        )
    assert _FakePopenOwner.instances == []


def test_timeout_stops_child_and_exposes_only_stable_code(tmp_path):
    prefix, source, evidence_parent = _layout(tmp_path)
    _FakePopenOwner.never_exits = True
    runner = _runner(
        _config(prefix, source, timeout_seconds=0.3),
        _Clock(),
    )

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_timeout$',
    ) as raised:
        runner.run(_request(evidence_parent))

    assert _FakePopenOwner.instances[0].stopped is True
    assert str(source) not in str(raised.value)


def test_check_timeout_remains_stable_after_bounded_forced_cleanup(
    tmp_path,
):
    prefix, source, _ = _layout(tmp_path)
    _FakePopenOwner.never_exits = True
    _FakePopenOwner.forced = 1

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_timeout$',
    ):
        _runner(
            _check_config(prefix, source, timeout_seconds=0.3),
            _Clock(),
        ).check()

    assert _FakePopenOwner.instances[0].stopped is True


@pytest.mark.parametrize('use_check', (False, True))
def test_keyboard_interrupt_still_cleans_owned_child(tmp_path, use_check):
    """One Ctrl-C is re-raised only after the child cleanup completes."""
    prefix, source, evidence_parent = _layout(tmp_path)
    _FakePopenOwner.never_exits = True
    config = (
        _check_config(prefix, source)
        if use_check else _config(prefix, source)
    )
    runner = _interrupting_runner(config)

    with pytest.raises(KeyboardInterrupt):
        if use_check:
            runner.check()
        else:
            runner.run(_request(evidence_parent))

    assert _FakePopenOwner.instances[0].stopped is True


@pytest.mark.parametrize('use_check', (False, True))
def test_interrupt_never_masks_incomplete_cleanup(tmp_path, use_check):
    """Unproven cleanup takes precedence over the public interrupt result."""
    prefix, source, evidence_parent = _layout(tmp_path)
    _FakePopenOwner.never_exits = True
    _FakePopenOwner.cleanup_complete = False
    config = (
        _check_config(prefix, source)
        if use_check else _config(prefix, source)
    )
    runner = _interrupting_runner(config)

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_cleanup_incomplete$',
    ):
        if use_check:
            runner.check()
        else:
            runner.run(_request(evidence_parent))

    assert _FakePopenOwner.instances[0].stopped is True


def test_nonzero_child_cleanup_and_output_fail_with_stable_codes(tmp_path):
    prefix, source, evidence_parent = _layout(tmp_path)
    config = _config(prefix, source)

    _FakePopenOwner.exit_code = 7
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_child_failed$',
    ):
        _runner(config, _Clock()).run(_request(evidence_parent))

    _FakePopenOwner.instances = []
    _FakePopenOwner.exit_code = 0
    _FakePopenOwner.cleanup_complete = False
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_cleanup_incomplete$',
    ):
        _runner(config, _Clock()).run(_request(evidence_parent))

    _FakePopenOwner.instances = []
    _FakePopenOwner.cleanup_complete = True
    _FakePopenOwner.overflowed = True
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_output_overflow$',
    ):
        _runner(config, _Clock()).run(_request(evidence_parent))

    _FakePopenOwner.instances = []
    _FakePopenOwner.overflowed = False
    _FakePopenOwner.forced = 1
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_cleanup_incomplete$',
    ):
        _runner(config, _Clock()).run(_request(evidence_parent))


def test_private_start_failure_is_not_retained_by_public_error(tmp_path):
    prefix, source, evidence_parent = _layout(tmp_path)

    def fail_with_private_details(*_args, **_kwargs):
        raise RuntimeError('/private/path and secret child argv')

    runner = runtime.InstalledTextGazeboAcceptanceRunner(
        _config(prefix, source),
        owner_factory=fail_with_private_details,
        environment_source=lambda: {'PATH': '/usr/bin'},
    )
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_start_failed$',
    ) as raised:
        runner.run(_request(evidence_parent))

    assert raised.value.__cause__ is None
    assert 'private' not in str(raised.value)


def test_spawned_identity_failure_never_claims_complete_cleanup(
    monkeypatch,
    tmp_path,
):
    """A spawned-but-unverified session cannot look never-started or clean."""
    process = _IdentityFailureProcess()
    monkeypatch.setattr(
        runtime.subprocess,
        'Popen',
        lambda *_args, **_kwargs: process,
    )

    def identity_unavailable(_pid):
        raise runtime.OwnedProcessError('process_identity_unavailable')

    monkeypatch.setattr(runtime, '_read_process_stat', identity_unavailable)
    owner = runtime._CapturedProcessOwner(
        'campaign-check-runner',
        ('/installed/runner', '--check'),
        cwd=tmp_path,
        environment={'PATH': '/usr/bin'},
        maximum_output_bytes=4096,
    )

    with pytest.raises(
        runtime.OwnedProcessError,
        match='^process_identity_unavailable$',
    ):
        owner.start()

    cleanup = owner.stop()
    assert process.killed is True
    assert process.waited is True
    assert process.stdout.closed is True
    assert cleanup.process_started is True
    assert cleanup.remaining_process_count == 0
    assert cleanup.output_collector_stopped is True
    assert cleanup.cleanup_complete is False


def test_spawned_identity_failure_is_public_cleanup_failure(
    monkeypatch,
    tmp_path,
):
    """The installed boundary prioritizes unproven cleanup over start error."""
    prefix, source, _unused = _layout(tmp_path)
    process = _IdentityFailureProcess()
    monkeypatch.setattr(
        runtime.subprocess,
        'Popen',
        lambda *_args, **_kwargs: process,
    )

    def identity_unavailable(_pid):
        raise runtime.OwnedProcessError('process_identity_unavailable')

    monkeypatch.setattr(runtime, '_read_process_stat', identity_unavailable)
    runner = runtime.InstalledTextGazeboAcceptanceRunner(
        _check_config(prefix, source),
        capture_owner_factory=runtime._CapturedProcessOwner,
        environment_source=lambda: {
            'AMENT_PREFIX_PATH': str(prefix),
            'LANG': 'C.UTF-8',
            'PATH': '/usr/bin',
        },
    )

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_cleanup_incomplete$',
    ) as raised:
        runner.check()

    assert raised.value.__cause__ is None


@pytest.mark.parametrize('use_check', (False, True))
def test_clean_not_started_cleanup_preserves_start_failure(
    tmp_path,
    use_check,
):
    prefix, source, evidence_parent = _layout(tmp_path)
    _FakePopenOwner.start_error = True
    config = (
        _check_config(prefix, source)
        if use_check else _config(prefix, source)
    )
    runner = _runner(config, _Clock())

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_start_failed$',
    ) as raised:
        if use_check:
            runner.check()
        else:
            runner.run(_request(evidence_parent))

    owner = _FakePopenOwner.instances[0]
    assert owner.started is False
    assert owner.stopped is True
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    'changes',
    (
        {'run_id': 'private-run-id'},
        {'exact_success': False},
        {'cleanup_complete': False},
        {'forced_termination_count': 1},
        {'simulation': False},
        {'physical_authorized': True},
        {'elapsed_seconds': 3.9},
    ),
)
def test_run_result_cannot_construct_a_weakened_success(changes):
    child = _summary()
    values = {
        'manifest_digest': child.manifest_digest,
        'receipt_digest': child.receipt_digest,
        'run_id': child.run_id,
        'commit': child.commit,
        'source_tree_digest': child.source_tree_digest,
        'installed_digest': child.installed_digest,
        'goal_set_digest': child.goal_set_digest,
        'runtime_binding_digest': child.runtime_binding_digest,
        'elapsed_seconds': 5.0,
        'child_output_digest': _EMPTY_DIGEST,
        'child_output_bytes': 0,
        'exact_success': True,
        'cleanup_complete': True,
        'forced_termination_count': 0,
        'simulation': True,
        'physical_authorized': False,
        'child_manifest': child,
    }
    values.update(changes)

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ):
        runtime.TextGazeboCampaignRunResult(**values)


@pytest.mark.parametrize(
    'manifest',
    (
        _manifest(commit='7' * 40),
        _manifest(source_tree_digest='7' * 64),
        _manifest(installed_digest='7' * 64),
    ),
)
def test_evidence_must_bind_expected_commit_source_and_install(
    tmp_path,
    manifest,
):
    prefix, source, evidence_parent = _layout(tmp_path)
    request = _request(evidence_parent)
    _FakePopenOwner.on_start = lambda _owner: write_evidence_manifest(
        request.evidence_path,
        manifest,
    )

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ):
        _runner(_config(prefix, source), _Clock()).run(request)


def test_manifest_parser_rejects_noncanonical_extra_and_private_files(
    tmp_path,
):
    prefix, source, evidence_parent = _layout(tmp_path)
    config = _config(prefix, source)
    request = _request(evidence_parent)
    value = _manifest().as_dict()
    value['private_path'] = '/private/host/path'

    def write_invalid(_owner):
        request.evidence_path.write_text(
            json.dumps(value),
            encoding='utf-8',
        )
        request.evidence_path.chmod(0o600)

    _FakePopenOwner.on_start = write_invalid
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ) as raised:
        _runner(config, _Clock()).run(request)
    assert '/private/host/path' not in str(raised.value)
    assert raised.value.__cause__ is None

    request.evidence_path.unlink()
    _FakePopenOwner.instances = []

    def write_public(_owner):
        write_evidence_manifest(request.evidence_path, _manifest())
        request.evidence_path.chmod(0o644)

    _FakePopenOwner.on_start = write_public
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ):
        _runner(config, _Clock()).run(request)


def test_missing_evidence_after_exit_is_never_success(tmp_path):
    prefix, source, evidence_parent = _layout(tmp_path)

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ):
        _runner(_config(prefix, source), _Clock()).run(
            _request(evidence_parent)
        )


def test_fifo_evidence_is_opened_nonblocking_and_rejected(
    tmp_path,
    monkeypatch,
):
    _, _, evidence_parent = _layout(tmp_path)
    evidence_path = evidence_parent / 'fifo.json'
    os.mkfifo(evidence_path, mode=0o600)
    real_open = runtime.os.open

    def checked_open(path, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(runtime.os, 'open', checked_open)
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ):
        runtime._read_manifest(evidence_path)


@pytest.mark.parametrize('mutation', ('replace', 'chmod'))
def test_evidence_path_identity_and_private_mode_are_rechecked_after_read(
    tmp_path,
    monkeypatch,
    mutation,
):
    _, _, evidence_parent = _layout(tmp_path)
    evidence_path = evidence_parent / 'case.json'
    manifest = _manifest()
    write_evidence_manifest(evidence_path, manifest)
    real_read = runtime.os.read
    mutated = False

    def racing_read(descriptor, maximum):
        nonlocal mutated
        payload = real_read(descriptor, maximum)
        if not mutated:
            mutated = True
            if mutation == 'replace':
                replacement = evidence_parent / 'replacement.json'
                replacement.write_bytes(
                    (manifest.canonical_json() + '\n').encode('utf-8')
                )
                replacement.chmod(0o600)
                os.replace(replacement, evidence_path)
            else:
                evidence_path.chmod(0o644)
        return payload

    monkeypatch.setattr(runtime.os, 'read', racing_read)
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_evidence_invalid$',
    ):
        runtime._read_manifest(evidence_path)


def test_source_tree_and_prefix_must_be_canonical(tmp_path):
    prefix, source, evidence_parent = _layout(tmp_path)
    source_link = tmp_path / 'source-link'
    source_link.symlink_to(source, target_is_directory=True)

    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_source_invalid$',
    ):
        _runner(_config(prefix, source_link), _Clock()).run(
            _request(evidence_parent)
        )

    prefix_link = tmp_path / 'prefix-link'
    prefix_link.symlink_to(prefix, target_is_directory=True)
    with pytest.raises(
        runtime.TextGazeboCampaignRuntimeError,
        match='^campaign_runner_install_invalid$',
    ):
        _runner(_config(prefix_link, source), _Clock()).run(
            _request(evidence_parent)
        )


def test_frozen_request_cannot_be_retargeted_after_validation(tmp_path):
    _, _, evidence_parent = _layout(tmp_path)
    request = _request(evidence_parent)

    with pytest.raises(Exception):
        request.ros_domain_id = 72
    with pytest.raises(Exception):
        request.evidence_path = evidence_parent / 'different.json'


def test_module_does_not_invoke_shell_or_capture_private_output():
    source = Path(runtime.__file__).read_text(encoding='utf-8')
    captured = runtime._CapturedOutputEvidence(
        payload=b'private-child-output',
        bytes_observed=20,
        digest='1' * 64,
        overflowed=False,
    )

    assert 'shell=True' not in source
    assert 'subprocess.check_output(' not in source
    assert 'getoutput(' not in source
    assert 'CalledProcessError' not in source
    assert 'private-child-output' not in repr(captured)
