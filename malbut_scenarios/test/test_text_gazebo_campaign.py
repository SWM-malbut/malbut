"""CLI integration contracts for the SWM25-134 campaign boundary."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import stat

import pytest

from malbut_scenarios import text_gazebo_campaign as campaign
from malbut_scenarios.text_gazebo_campaign_core import (
    ExpectedProductOutcome,
    SafetyBlockCode,
    UnknownResultCode,
)
from malbut_scenarios.text_gazebo_campaign_evidence import (
    ChildManifestSummary,
    ProductOutcome,
)
from malbut_scenarios.text_gazebo_campaign_runtime import (
    TextGazeboCampaignCheckConfig,
    TextGazeboCampaignCheckResult,
    TextGazeboCampaignRunResult,
    TextGazeboCampaignRunnerConfig,
    TextGazeboCampaignRuntimeError,
)
from malbut_scenarios.text_gazebo_evidence import (
    execution_fault_observation_for,
    pressure_evidence_for,
    safety_fault_observation_for,
)
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboExecutionProfile,
    TextGazeboFaultProfile,
    TextGazeboSafetyProfile,
    TextGazeboScenarioProfile,
    execution_contract,
    safety_contract,
)


_COMMIT = '1' * 40
_SOURCE_DIGEST = '2' * 64
_INSTALL_DIGEST = '3' * 64


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode('ascii')).hexdigest()


def _check_result() -> TextGazeboCampaignCheckResult:
    return TextGazeboCampaignCheckResult(
        commit=_COMMIT,
        source_tree_digest=_SOURCE_DIGEST,
        installed_digest=_INSTALL_DIGEST,
        child_output_digest=_digest('check-output'),
        child_output_bytes=128,
        elapsed_seconds=0.1,
        nav2_start_count=0,
        simulation=True,
        physical_authorized=False,
    )


def _run_result(
    ordinal: int,
    profile: TextGazeboScenarioProfile = TextGazeboScenarioProfile.HAPPY_PATH,
    fault_profile: TextGazeboFaultProfile = TextGazeboFaultProfile.NONE,
    safety_profile: TextGazeboSafetyProfile = TextGazeboSafetyProfile.NONE,
    execution_profile: TextGazeboExecutionProfile = (
        TextGazeboExecutionProfile.NONE
    ),
) -> TextGazeboCampaignRunResult:
    blocked = safety_profile is not TextGazeboSafetyProfile.NONE
    unknown = execution_profile is not TextGazeboExecutionProfile.NONE
    contract = safety_contract(safety_profile)
    execution = execution_contract(execution_profile)
    product_outcome = (
        ProductOutcome.BLOCKED
        if blocked
        else (ProductOutcome.UNKNOWN if unknown else ProductOutcome.SUCCEEDED)
    )
    block_result_code = (
        SafetyBlockCode.NONE
        if contract.result_code is None
        else SafetyBlockCode(contract.result_code)
    )
    manifest_digest = _digest(f'manifest-{ordinal}')
    receipt_digest = _digest(f'receipt-{ordinal}')
    goal_digest = (
        hashlib.sha256(b'[]').hexdigest()
        if blocked or execution.expected_nav2_goal_count == 0
        else _digest(f'goal-{ordinal}')
    )
    binding_digest = _digest(f'binding-{ordinal}')
    target_digest = _digest(f'target-{profile.value}')
    summary = ChildManifestSummary(
        manifest_digest=manifest_digest,
        receipt_digest=receipt_digest,
        run_id=f'run-{ordinal:032x}',
        commit=_COMMIT,
        source_tree_digest=_SOURCE_DIGEST,
        installed_digest=_INSTALL_DIGEST,
        goal_set_digest=goal_digest,
        runtime_binding_digest=binding_digest,
        target_binding_digest=target_digest,
        scenario_profile=profile,
        cleanup_complete=True,
        owned_processes_remaining=0,
        ros_nodes_remaining=0,
        owned_sockets_remaining=0,
        forced_termination_count=0,
        simulation=True,
        physical_authorized=False,
        exact_success=not blocked and not unknown,
        total_duration_seconds=0.0,
        fault_profile=fault_profile,
        pressure=pressure_evidence_for(fault_profile),
        safety_profile=safety_profile,
        execution_profile=execution_profile,
        product_outcome=product_outcome,
        block_result_code=block_result_code,
        unknown_result_code=(
            UnknownResultCode.NONE
            if execution.result_code is None
            else UnknownResultCode(execution.result_code)
        ),
        fault_observation=safety_fault_observation_for(safety_profile),
        execution_fault_observation=(
            execution_fault_observation_for(execution_profile)
        ),
    )
    return TextGazeboCampaignRunResult(
        manifest_digest=manifest_digest,
        receipt_digest=receipt_digest,
        run_id=summary.run_id,
        commit=_COMMIT,
        source_tree_digest=_SOURCE_DIGEST,
        installed_digest=_INSTALL_DIGEST,
        goal_set_digest=goal_digest,
        runtime_binding_digest=binding_digest,
        target_binding_digest=target_digest,
        scenario_profile=profile,
        elapsed_seconds=0.0,
        child_output_digest=_digest(f'output-{ordinal}'),
        child_output_bytes=256,
        exact_success=not blocked and not unknown,
        cleanup_complete=True,
        forced_termination_count=0,
        simulation=True,
        physical_authorized=False,
        child_manifest=summary,
        fault_profile=fault_profile,
        pressure=summary.pressure,
        safety_profile=safety_profile,
        execution_profile=execution_profile,
        product_outcome=product_outcome,
        block_result_code=block_result_code,
        unknown_result_code=summary.unknown_result_code,
        execution_fault_observation=(
            summary.execution_fault_observation
        ),
    )


class _FakeInstalledRunner:
    configs = []
    requests = []
    failure_code = None
    result_profile = None

    def __init__(self, config) -> None:
        self.config = config
        type(self).configs.append(config)

    def check(self):
        return _check_result()

    def run(self, request):
        type(self).requests.append(request)
        if self.failure_code is not None:
            raise TextGazeboCampaignRuntimeError(self.failure_code)
        return _run_result(
            len(type(self).requests),
            self.result_profile or request.scenario_profile,
            request.fault_profile,
            request.safety_profile,
            request.execution_profile,
        )


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    _FakeInstalledRunner.configs = []
    _FakeInstalledRunner.requests = []
    _FakeInstalledRunner.failure_code = None
    _FakeInstalledRunner.result_profile = None


def _source_tree(tmp_path: Path) -> Path:
    selected = tmp_path / 'source'
    selected.mkdir()
    return selected.resolve(strict=True)


def _private_evidence(tmp_path: Path) -> Path:
    parent = tmp_path / 'private'
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    return parent.resolve(strict=True) / 'campaign.json'


def _patch_installed(monkeypatch, tmp_path: Path) -> Path:
    prefix = tmp_path / 'install'
    prefix.mkdir()
    prefix = prefix.resolve(strict=True)
    monkeypatch.setattr(
        campaign,
        '_discover_installed_prefix',
        lambda: prefix,
    )
    monkeypatch.setattr(
        campaign,
        'InstalledTextGazeboAcceptanceRunner',
        _FakeInstalledRunner,
    )
    return prefix


def _run_arguments(
    source: Path,
    evidence: Path,
    count: int = 1,
    profiles=None,
):
    values = [
        '--run',
        '--execute-approved-simulation',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(source),
        '--evidence',
        str(evidence),
        '--ros-domain-id',
        '77',
    ]
    selected = profiles or ['happy_path'] * count
    for profile in selected:
        values.extend(('--case-profile', profile))
    return values


def test_help_is_a_successful_non_actuating_exit(capsys) -> None:
    """Requested help succeeds without emitting a false failure record."""
    result = campaign.main(['--help'])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ''
    assert 'usage:' in captured.out
    assert '--execute-approved-simulation' in captured.out
    assert 'explicitly arm simulation execution' in captured.out


def test_run_is_default_off_before_install_discovery(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Missing explicit simulation authority cannot reach a child runner."""
    source = _source_tree(tmp_path)
    called = []
    monkeypatch.setattr(
        campaign,
        '_discover_installed_prefix',
        lambda: called.append(True),
    )

    result = campaign.main([
        '--run',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(source),
        '--evidence',
        str(tmp_path / 'private' / 'campaign.json'),
        '--ros-domain-id',
        '77',
        '--case-profile',
        'happy_path',
    ])

    captured = capsys.readouterr()
    assert result == 1
    assert called == []
    assert json.loads(captured.err) == {
        'error_code': 'campaign_arguments_invalid',
        'status': 'failed',
    }


def test_check_uses_only_non_actuating_installed_check(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Check obtains provenance with zero campaign case execution."""
    source = _source_tree(tmp_path)
    prefix = _patch_installed(monkeypatch, tmp_path)

    result = campaign.main([
        '--check',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(source),
        '--case-profile',
        'happy_path',
    ])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ''
    assert _FakeInstalledRunner.requests == []
    assert len(_FakeInstalledRunner.configs) == 1
    config = _FakeInstalledRunner.configs[0]
    assert isinstance(config, TextGazeboCampaignCheckConfig)
    assert config.installed_prefix == prefix
    assert json.loads(captured.out) == {
        'case_count': 1,
        'installed_digest': _INSTALL_DIGEST,
        'mode': 'check',
        'nav2_start_count': 0,
        'physical_authorized': False,
        'simulation': True,
        'source_tree_digest': _SOURCE_DIGEST,
        'status': 'ok',
    }


def test_check_requires_a_bounded_allowlisted_plan_before_child_io(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """A check without cases cannot attest an unspecified campaign."""
    source = _source_tree(tmp_path)
    called = []
    monkeypatch.setattr(
        campaign,
        '_discover_installed_prefix',
        lambda: called.append(True),
    )

    result = campaign.main([
        '--check',
        '--source-commit',
        _COMMIT,
        '--source-tree',
        str(source),
    ])

    assert result == 1
    assert called == []
    assert json.loads(capsys.readouterr().err) == {
        'error_code': 'campaign_arguments_invalid',
        'status': 'failed',
    }


def test_cli_case_factory_keeps_canonical_profile_expectations() -> None:
    """CLI callers cannot override the allowlisted Safety expectation."""
    happy, stale = campaign._cases(('happy_path', 'stale_state'))

    assert happy.expected_outcome is ExpectedProductOutcome.SUCCEEDED
    assert happy.expected_block_code is SafetyBlockCode.NONE
    assert stale.expected_outcome is ExpectedProductOutcome.BLOCKED
    assert stale.expected_block_code is SafetyBlockCode.ROBOT_STATE_STALE


def test_profiles_map_to_unique_ordered_cases_and_private_child_paths(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Repeated allowlisted profiles retain order and isolated child paths."""
    source = _source_tree(tmp_path)
    evidence = _private_evidence(tmp_path)
    _patch_installed(monkeypatch, tmp_path)

    profiles = [
        'happy_living_room',
        'happy_kitchen',
        'happy_bedroom',
    ]
    result = campaign.main(_run_arguments(
        source,
        evidence,
        profiles=profiles,
    ))

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ''
    assert len(_FakeInstalledRunner.configs) == 2
    assert isinstance(
        _FakeInstalledRunner.configs[1],
        TextGazeboCampaignRunnerConfig,
    )
    assert _FakeInstalledRunner.configs[1].source_tree_digest == (
        _SOURCE_DIGEST
    )
    paths = [
        request.evidence_path
        for request in _FakeInstalledRunner.requests
    ]
    assert [path.name for path in paths] == [
        'case-001.json',
        'case-002.json',
        'case-003.json',
    ]
    assert len({path.parent for path in paths}) == 1
    assert paths[0].parent.parent == evidence.parent
    assert stat.S_IMODE(paths[0].parent.stat().st_mode) == 0o700
    assert all(
        request.ros_domain_id == 77
        for request in _FakeInstalledRunner.requests
    )
    assert [
        request.scenario_profile.value
        for request in _FakeInstalledRunner.requests
    ] == profiles

    payload = json.loads(evidence.read_text(encoding='utf-8'))
    receipt = payload['receipt']
    assert receipt['test_verdict'] == 'passed'
    assert receipt['stopped_early'] is False
    assert [item['ordinal'] for item in receipt['cases']] == [1, 2, 3]
    assert [item['case_id'] for item in receipt['cases']] == [
        'case-001',
        'case-002',
        'case-003',
    ]
    assert [item['profile'] for item in receipt['cases']] == [
        'happy_living_room',
        'happy_kitchen',
        'happy_bedroom',
    ]
    assert [item['expected_outcome'] for item in receipt['cases']] == [
        'succeeded',
        'succeeded',
        'succeeded',
    ]
    assert [item['child_manifest_digest'] for item in receipt['cases']] == [
        _digest('manifest-1'),
        _digest('manifest-2'),
        _digest('manifest-3'),
    ]
    assert receipt['cleanup']['completed'] is True
    assert receipt['cleanup']['clean_case_count'] == 3
    response = json.loads(captured.out)
    assert response['case_count'] == 3
    assert response['status'] == 'succeeded'
    assert response['simulation'] is True
    assert response['physical_authorized'] is False


def test_exactly_once_tokens_map_to_living_room_and_distinct_faults(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    source = _source_tree(tmp_path)
    evidence = _private_evidence(tmp_path)
    _patch_installed(monkeypatch, tmp_path)
    profiles = [
        'duplicate_request',
        'concurrent_approval',
        'competing_workers',
    ]

    result = campaign.main(_run_arguments(
        source,
        evidence,
        profiles=profiles,
    ))

    assert result == 0
    assert capsys.readouterr().err == ''
    requests = _FakeInstalledRunner.requests
    assert [item.scenario_profile for item in requests] == [
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
    ] * 3
    assert [item.fault_profile.value for item in requests] == profiles
    receipt = json.loads(evidence.read_text(encoding='utf-8'))['receipt']
    assert [item['profile'] for item in receipt['cases']] == profiles
    assert [item['scenario_profile'] for item in receipt['cases']] == [
        'happy_living_room',
    ] * 3
    assert [item['fault_profile'] for item in receipt['cases']] == profiles
    assert [item['pressure'] for item in receipt['cases']] == [
        pressure_evidence_for(profile).as_dict()
        for profile in (
            TextGazeboFaultProfile.DUPLICATE_REQUEST,
            TextGazeboFaultProfile.CONCURRENT_APPROVAL,
            TextGazeboFaultProfile.COMPETING_WORKERS,
        )
    ]
    assert receipt['cleanup']['completed'] is True
    assert receipt['cleanup']['clean_case_count'] == 3


def test_swm137_profiles_pass_only_as_exact_zero_effect_blocks(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """One CLI campaign reuses the runner for all three Safety gates."""
    source = _source_tree(tmp_path)
    evidence = _private_evidence(tmp_path)
    _patch_installed(monkeypatch, tmp_path)
    profiles = [
        'stale_state',
        'emergency_stop',
        'map_revision_changed',
    ]

    result = campaign.main(_run_arguments(
        source,
        evidence,
        profiles=profiles,
    ))

    assert result == 0
    assert capsys.readouterr().err == ''
    requests = _FakeInstalledRunner.requests
    assert [item.scenario_profile.value for item in requests] == [
        'happy_living_room',
        'happy_living_room',
        'happy_living_room',
    ]
    assert [item.fault_profile.value for item in requests] == [
        'none',
        'none',
        'none',
    ]
    assert [item.safety_profile.value for item in requests] == profiles

    receipt = json.loads(evidence.read_text(encoding='utf-8'))['receipt']
    assert receipt['test_verdict'] == 'passed'
    assert [item['expected_outcome'] for item in receipt['cases']] == [
        'blocked',
        'blocked',
        'blocked',
    ]
    assert [item['observed_outcome'] for item in receipt['cases']] == [
        'blocked',
        'blocked',
        'blocked',
    ]
    assert [item['expected_block_code'] for item in receipt['cases']] == [
        'robot_state_stale',
        'safety_emergency_stop',
        'target_binding_changed',
    ]
    assert [item['observed_block_code'] for item in receipt['cases']] == [
        'robot_state_stale',
        'safety_emergency_stop',
        'target_binding_changed',
    ]
    assert [item['safety_profile'] for item in receipt['cases']] == profiles
    assert receipt['cleanup']['completed'] is True


def test_swm138_five_case_campaign_preserves_exact_unknown_contracts(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Normal controls and all execution faults pass in explicit order."""
    source = _source_tree(tmp_path)
    evidence = _private_evidence(tmp_path)
    _patch_installed(monkeypatch, tmp_path)
    profiles = [
        'happy_living_room',
        'nav2_unavailable',
        'start_response_lost',
        'terminal_status_response_lost',
        'happy_living_room',
    ]

    result = campaign.main(_run_arguments(
        source,
        evidence,
        profiles=profiles,
    ))

    assert result == 0
    assert capsys.readouterr().err == ''
    requests = _FakeInstalledRunner.requests
    assert [item.execution_profile.value for item in requests] == [
        'none',
        'nav2_unavailable',
        'start_response_lost',
        'terminal_status_response_lost',
        'none',
    ]
    receipt = json.loads(evidence.read_text(encoding='utf-8'))['receipt']
    assert receipt['test_verdict'] == 'passed'
    assert [item['profile'] for item in receipt['cases']] == profiles
    assert [item['expected_outcome'] for item in receipt['cases']] == [
        'succeeded',
        'unknown',
        'unknown',
        'unknown',
        'succeeded',
    ]
    assert [
        item['expected_unknown_result_code']
        for item in receipt['cases']
    ] == [
        'none',
        'navigation_start_outcome_unknown',
        'navigation_start_outcome_unknown',
        'navigation_status_outcome_unknown',
        'none',
    ]
    assert [
        item['observed_unknown_result_code']
        for item in receipt['cases']
    ] == [
        'none',
        'navigation_start_outcome_unknown',
        'navigation_start_outcome_unknown',
        'navigation_status_outcome_unknown',
        'none',
    ]
    assert [
        item['execution_fault_observation']['observed']
        for item in receipt['cases']
    ] == [False, True, True, True, False]
    assert receipt['cleanup']['completed'] is True
    assert receipt['cleanup']['clean_case_count'] == 5


def test_child_cleanup_failure_stops_and_publishes_failed_aggregate(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Unproven child cleanup blocks every later case and campaign pass."""
    source = _source_tree(tmp_path)
    evidence = _private_evidence(tmp_path)
    _patch_installed(monkeypatch, tmp_path)
    _FakeInstalledRunner.failure_code = 'campaign_runner_cleanup_incomplete'

    result = campaign.main(_run_arguments(source, evidence, count=3))

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ''
    assert len(_FakeInstalledRunner.requests) == 1
    payload = json.loads(evidence.read_text(encoding='utf-8'))
    receipt = payload['receipt']
    assert receipt['test_verdict'] == 'failed'
    assert receipt['stopped_early'] is True
    assert [item['test_verdict'] for item in receipt['cases']] == [
        'failed',
        'partial',
        'partial',
    ]
    assert [item['error_code'] for item in receipt['cases']] == [
        'executor_exception',
        'previous_case_unsafe',
        'previous_case_unsafe',
    ]
    assert receipt['cleanup'] == {
        'clean_case_count': 0,
        'completed': False,
        'forced_termination_count': 0,
        'incomplete_case_count': 0,
        'not_observed_case_count': 3,
        'owned_processes_remaining': 0,
        'owned_sockets_remaining': 0,
        'ros_nodes_remaining': 0,
    }
    response = json.loads(captured.err)
    assert response['error_code'] == 'campaign_failed'
    assert response['case_count'] == 3
    assert response['test_verdict'] == 'failed'


def test_runner_result_for_another_profile_fails_closed(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """A child result for another room cannot pass the campaign case."""
    source = _source_tree(tmp_path)
    evidence = _private_evidence(tmp_path)
    _patch_installed(monkeypatch, tmp_path)
    _FakeInstalledRunner.result_profile = (
        TextGazeboScenarioProfile.HAPPY_BEDROOM
    )

    result = campaign.main(_run_arguments(
        source,
        evidence,
        profiles=['happy_living_room'],
    ))

    assert result == 1
    assert len(_FakeInstalledRunner.requests) == 1
    payload = json.loads(evidence.read_text(encoding='utf-8'))
    case = payload['receipt']['cases'][0]
    assert case['profile'] == 'happy_living_room'
    assert case['test_verdict'] == 'failed'
    assert case['error_code'] == 'executor_exception'
    assert json.loads(capsys.readouterr().err)['status'] == 'failed'


def test_preexisting_aggregate_blocks_before_any_child(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """No-overwrite preflight happens before the non-check runner exists."""
    source = _source_tree(tmp_path)
    evidence = _private_evidence(tmp_path)
    evidence.write_text('private-existing', encoding='utf-8')
    evidence.chmod(0o600)
    _patch_installed(monkeypatch, tmp_path)

    result = campaign.main(_run_arguments(source, evidence))

    captured = capsys.readouterr()
    assert result == 1
    assert _FakeInstalledRunner.configs == []
    assert _FakeInstalledRunner.requests == []
    assert evidence.read_text(encoding='utf-8') == 'private-existing'
    assert json.loads(captured.err) == {
        'error_code': 'campaign_evidence_invalid',
        'status': 'failed',
    }


def test_existing_non_private_parent_is_rejected_without_chmod(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Preflight never mutates an operator directory to make it acceptable."""
    source = _source_tree(tmp_path)
    parent = tmp_path / 'not-private'
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    evidence = parent.resolve(strict=True) / 'campaign.json'
    _patch_installed(monkeypatch, tmp_path)

    result = campaign.main(_run_arguments(source, evidence))

    assert result == 1
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert _FakeInstalledRunner.configs == []
    assert _FakeInstalledRunner.requests == []
    assert json.loads(capsys.readouterr().err) == {
        'error_code': 'campaign_evidence_invalid',
        'status': 'failed',
    }


def test_public_output_never_contains_private_paths(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Success output is bounded and excludes source/evidence host paths."""
    source = _source_tree(tmp_path)
    evidence = _private_evidence(tmp_path)
    prefix = _patch_installed(monkeypatch, tmp_path)

    assert campaign.main(_run_arguments(source, evidence)) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for private_value in (str(source), str(evidence), str(prefix)):
        assert private_value not in combined
    assert 'case-001.json' not in combined


def test_campaign_cli_has_no_agent_or_execution_subsystem_imports() -> None:
    """Campaign imports only core, evidence, and runtime ports."""
    source = Path(campaign.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    banned = (
        'malbut_agent_server',
        'malbut_gazebo',
        'malbut_roaming',
        'rclpy',
    )
    assert not any(
        name == prefix or name.startswith(prefix + '.')
        for name in imported
        for prefix in banned
    )


def test_more_than_32_cases_fails_before_install_discovery(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    source = _source_tree(tmp_path)
    called = []
    monkeypatch.setattr(
        campaign,
        '_discover_installed_prefix',
        lambda: called.append(True),
    )
    evidence = tmp_path / 'private' / 'campaign.json'

    result = campaign.main(_run_arguments(source, evidence, count=33))

    assert result == 1
    assert called == []
    assert json.loads(capsys.readouterr().err)['error_code'] == (
        'campaign_arguments_invalid'
    )
