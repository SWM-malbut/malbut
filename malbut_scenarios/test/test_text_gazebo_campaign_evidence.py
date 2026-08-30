"""Contracts for content-free SWM25-134 campaign evidence."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
import stat
from threading import Barrier, Thread

import pytest

from malbut_scenarios.text_gazebo_campaign_core import (
    CampaignProfile,
    campaign_profile_binding,
)
from malbut_scenarios.text_gazebo_campaign_evidence import (
    CAMPAIGN_EVIDENCE_FORMAT,
    MAX_CHILD_MANIFEST_BYTES,
    CampaignCaseEvidence,
    CampaignCleanupAggregate,
    CampaignEvidenceError,
    CampaignTestVerdict,
    CaseCleanupState,
    CaseErrorCode,
    CaseTestVerdict,
    ProductOutcome,
    SafetyBlockCode,
    TextGazeboCampaignManifest,
    TextGazeboCampaignReceipt,
    parse_child_manifest,
    write_campaign_manifest,
)
from malbut_scenarios.text_gazebo_evidence import (
    CleanupEvidence,
    ConfirmationState,
    DispatchState,
    EvidenceCounts,
    EvidenceDurations,
    NavigationState,
    ProductOutcome as ChildProductOutcome,
    pressure_evidence_for,
    ReadinessState,
    RobotActionState,
    StableStates,
    TestStatus,
    TextGazeboEvidenceManifest,
    TextGazeboEvidenceReceipt,
    safety_fault_observation_for,
)
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboFaultProfile,
    TextGazeboSafetyProfile,
    TextGazeboScenarioProfile,
    safety_contract,
)


COMMIT = '1' * 40
SOURCE_DIGEST = '2' * 64
INSTALLED_DIGEST = '3' * 64
EMPTY_GOAL_SET_DIGEST = hashlib.sha256(b'[]').hexdigest()


def _child_manifest(
    *,
    run_digit='4',
    commit=COMMIT,
    goal_digit='5',
    target_digit='6',
    profile=TextGazeboScenarioProfile.HAPPY_PATH,
    fault_profile=TextGazeboFaultProfile.NONE,
    safety_profile=TextGazeboSafetyProfile.NONE,
):
    blocked = safety_profile is not TextGazeboSafetyProfile.NONE
    contract = safety_contract(safety_profile)
    receipt = TextGazeboEvidenceReceipt(
        run_id='run-' + run_digit * 32,
        commit=commit,
        source_tree_digest=SOURCE_DIGEST,
        installed_digest=INSTALLED_DIGEST,
        goal_set_digest=(
            EMPTY_GOAL_SET_DIGEST if blocked else goal_digit * 64
        ),
        runtime_binding_digest='6' * 64,
        target_binding_digest=target_digit * 64,
        scenario_profile=profile,
        fault_profile=fault_profile,
        pressure=pressure_evidence_for(fault_profile),
        safety_profile=safety_profile,
        product_outcome=(
            ChildProductOutcome.BLOCKED
            if blocked
            else ChildProductOutcome.SUCCEEDED
        ),
        test_status=TestStatus.PASSED,
        block_result_code=contract.result_code,
        fault_observation=safety_fault_observation_for(safety_profile),
        states=StableStates(
            readiness=ReadinessState.READY,
            confirmation=ConfirmationState.APPROVED,
            robot_action=(
                RobotActionState.BLOCKED
                if blocked
                else RobotActionState.SUCCEEDED
            ),
            dispatch=(
                DispatchState.NOT_CREATED
                if blocked
                else DispatchState.TERMINAL
            ),
            navigation=(
                NavigationState.NOT_STARTED
                if blocked
                else NavigationState.SUCCEEDED
            ),
        ),
        counts=EvidenceCounts(
            agent_proposal_count=1,
            confirmation_count=1,
            approved_confirmation_count=1,
            robot_action_count=1,
            dispatch_intent_count=0 if blocked else 1,
            robot_web_start_count=0 if blocked else 1,
            robot_web_verified_target_count=1,
            nav2_goal_count=0 if blocked else 1,
            preapproval_nav2_goal_count=0,
            terminal_result_count=0 if blocked else 1,
            replay_additional_effect_count=0,
        ),
        durations=EvidenceDurations(
            readiness_seconds=1,
            execution_seconds=2,
            cleanup_seconds=1,
            total_seconds=4,
        ),
        cleanup=CleanupEvidence(
            completed=True,
            owned_processes_remaining=0,
            ros_nodes_remaining=0,
            owned_sockets_remaining=0,
            forced_termination_count=0,
        ),
    )
    return TextGazeboEvidenceManifest(receipt)


def _child_summary(
    *,
    run_digit='4',
    commit=COMMIT,
    goal_digit='5',
    target_digit='6',
    profile=TextGazeboScenarioProfile.HAPPY_PATH,
    fault_profile=TextGazeboFaultProfile.NONE,
    safety_profile=TextGazeboSafetyProfile.NONE,
):
    manifest = _child_manifest(
        run_digit=run_digit,
        commit=commit,
        goal_digit=goal_digit,
        target_digit=target_digit,
        profile=profile,
        fault_profile=fault_profile,
        safety_profile=safety_profile,
    )
    payload = (manifest.canonical_json() + '\n').encode('utf-8')
    return parse_child_manifest(payload)


def _case(
    *,
    ordinal=1,
    case_id='normal-smoke',
    profile='happy_path',
    child=None,
    **changes,
):
    binding = None
    try:
        binding = campaign_profile_binding(CampaignProfile(profile))
    except ValueError:
        pass
    if child is None and 'child_manifest' not in changes:
        if binding is not None:
            child = _child_summary(
                profile=binding.scenario_profile,
                fault_profile=binding.fault_profile,
                safety_profile=binding.safety_profile,
            )
    expected = (
        ProductOutcome.SUCCEEDED
        if binding is None
        else ProductOutcome(binding.expected_outcome.value)
    )
    block_code = (
        SafetyBlockCode.NONE
        if binding is None
        else binding.expected_block_code
    )
    values = {
        'ordinal': ordinal,
        'case_id': case_id,
        'profile': profile,
        'expected_outcome': expected,
        'observed_outcome': expected,
        'test_verdict': CaseTestVerdict.PASSED,
        'error_code': CaseErrorCode.NONE,
        'child_manifest': child,
        'duration_seconds': 5,
        'cleanup': CaseCleanupState.CLEAN,
        'expected_block_code': block_code,
        'observed_block_code': block_code,
    }
    values.update(changes)
    return CampaignCaseEvidence(**values)


def _cleanup(case_count=1, **changes):
    values = {
        'completed': True,
        'clean_case_count': case_count,
        'incomplete_case_count': 0,
        'not_observed_case_count': 0,
        'owned_processes_remaining': 0,
        'ros_nodes_remaining': 0,
        'owned_sockets_remaining': 0,
        'forced_termination_count': 0,
    }
    values.update(changes)
    return CampaignCleanupAggregate(**values)


def _receipt(*, cases=None, **changes):
    normalized = cases if cases is not None else (_case(),)
    values = {
        'campaign_id': 'campaign-' + '7' * 32,
        'commit': COMMIT,
        'source_tree_digest': SOURCE_DIGEST,
        'installed_digest': INSTALLED_DIGEST,
        'cases': normalized,
        'test_verdict': CampaignTestVerdict.PASSED,
        'stopped_early': False,
        'total_duration_seconds': sum(
            case.duration_seconds for case in normalized
        ),
        'cleanup': _cleanup(len(normalized)),
    }
    values.update(changes)
    return TextGazeboCampaignReceipt(**values)


def test_child_parser_returns_strict_digest_only_success_summary():
    """Canonical child evidence yields only bounded verified facts."""
    manifest = _child_manifest()
    payload = (manifest.canonical_json() + '\n').encode('utf-8')

    summary = parse_child_manifest(payload)

    assert summary.manifest_digest == hashlib.sha256(
        manifest.canonical_json().encode('utf-8')
    ).hexdigest()
    assert summary.receipt_digest == manifest.receipt_digest
    assert summary.run_id == 'run-' + '4' * 32
    assert summary.commit == COMMIT
    assert summary.source_tree_digest == SOURCE_DIGEST
    assert summary.installed_digest == INSTALLED_DIGEST
    assert summary.goal_set_digest == '5' * 64
    assert summary.runtime_binding_digest == '6' * 64
    assert summary.target_binding_digest == '6' * 64
    assert summary.scenario_profile is (
        TextGazeboScenarioProfile.HAPPY_PATH
    )
    assert summary.fault_profile is TextGazeboFaultProfile.NONE
    assert summary.pressure == pressure_evidence_for(
        TextGazeboFaultProfile.NONE
    )
    assert summary.cleanup_complete is True
    assert summary.forced_termination_count == 0
    assert summary.simulation is True
    assert summary.physical_authorized is False
    assert summary.exact_success is True
    assert summary.total_duration_seconds == 4.0


@pytest.mark.parametrize(
    'profile,block_code,map_switch_count',
    (
        (
            TextGazeboSafetyProfile.STALE_STATE,
            SafetyBlockCode.ROBOT_STATE_STALE,
            0,
        ),
        (
            TextGazeboSafetyProfile.EMERGENCY_STOP,
            SafetyBlockCode.SAFETY_EMERGENCY_STOP,
            0,
        ),
        (
            TextGazeboSafetyProfile.MAP_REVISION_CHANGED,
            SafetyBlockCode.TARGET_BINDING_CHANGED,
            1,
        ),
    ),
)
def test_child_parser_accepts_exact_zero_effect_safety_block(
    profile,
    block_code,
    map_switch_count,
):
    """A typed BLOCKED product is a passing test only on exact evidence."""
    summary = _child_summary(
        profile=TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        safety_profile=profile,
    )

    assert summary.product_outcome is ProductOutcome.BLOCKED
    assert summary.exact_success is False
    assert summary.safety_profile is profile
    assert summary.block_result_code is block_code
    assert summary.goal_set_digest == EMPTY_GOAL_SET_DIGEST
    assert summary.test_status is TestStatus.PASSED
    assert summary.fault_observation.fault_application_count == 1
    assert summary.fault_observation.map_switch_count == map_switch_count


def test_child_parser_rejects_wrong_safety_block_code():
    """A different reason code cannot be relabelled as the expected block."""
    value = json.loads(_child_manifest(
        profile=TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        safety_profile=TextGazeboSafetyProfile.STALE_STATE,
    ).canonical_json())
    value['receipt']['block_result_code'] = 'safety_emergency_stop'
    receipt = json.dumps(
        value['receipt'],
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    )
    value['receipt_digest'] = hashlib.sha256(
        receipt.encode('utf-8')
    ).hexdigest()
    payload = (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':'),
        ) + '\n'
    ).encode('utf-8')

    with pytest.raises(CampaignEvidenceError) as caught:
        parse_child_manifest(payload)

    assert caught.value.code == 'child_manifest_success_invalid'


def test_child_parser_rejects_blocked_receipt_with_nonempty_goal_set():
    """Zero public goal counts cannot accompany a non-empty goal digest."""
    manifest = _child_manifest(
        profile=TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        safety_profile=TextGazeboSafetyProfile.STALE_STATE,
    )
    receipt = replace(manifest.receipt, goal_set_digest='9' * 64)
    invalid = TextGazeboEvidenceManifest(receipt)

    with pytest.raises(CampaignEvidenceError) as caught:
        parse_child_manifest(
            (invalid.canonical_json() + '\n').encode('utf-8')
        )

    assert caught.value.code == 'child_manifest_success_invalid'


@pytest.mark.parametrize(
    'payload,code',
    (
        ('not-bytes', 'child_manifest_payload_invalid'),
        (b'', 'child_manifest_payload_invalid'),
        (b'\xff\n', 'child_manifest_encoding_invalid'),
        (b'{}', 'child_manifest_json_invalid'),
        (b'{"x":NaN}\n', 'child_manifest_json_invalid'),
        (b'{"x":1,"x":2}\n', 'child_manifest_json_invalid'),
        (
            b'x' * (MAX_CHILD_MANIFEST_BYTES + 1),
            'child_manifest_too_large',
        ),
    ),
)
def test_child_parser_rejects_invalid_payload_with_stable_code(
    payload,
    code,
):
    """Malformed bytes fail with a stable content-free code."""
    with pytest.raises(CampaignEvidenceError) as caught:
        parse_child_manifest(payload)

    assert caught.value.code == code
    assert str(caught.value) == code


def _mutated_child(mutator):
    value = json.loads(_child_manifest().canonical_json())
    mutator(value)
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        ) + '\n'
    ).encode('utf-8')


def test_child_parser_rejects_wrong_format_extra_fields_and_digest():
    """Wrong envelopes, hidden keys, and digest drift fail closed."""
    wrong_format = _mutated_child(
        lambda value: value.__setitem__('format', 'wrong')
    )
    extra_field = _mutated_child(
        lambda value: value.__setitem__('private_path', '/private/value')
    )
    wrong_digest = _mutated_child(
        lambda value: value.__setitem__('receipt_digest', '0' * 64)
    )

    expected = (
        (wrong_format, 'child_manifest_format_invalid'),
        (extra_field, 'child_manifest_schema_invalid'),
        (wrong_digest, 'child_manifest_digest_invalid'),
    )
    for payload, code in expected:
        with pytest.raises(CampaignEvidenceError) as caught:
            parse_child_manifest(payload)
        assert caught.value.code == code
        assert '/private/value' not in repr(caught.value)


def test_child_parser_rejects_noncanonical_or_non_success_receipt():
    """Only canonical exact-success v4 child evidence is accepted."""
    manifest = _child_manifest()
    noncanonical = json.dumps(json.loads(manifest.canonical_json()))
    with pytest.raises(CampaignEvidenceError) as caught:
        parse_child_manifest((noncanonical + '\n').encode('utf-8'))
    assert caught.value.code == 'child_manifest_schema_invalid'

    def make_unknown(value):
        value['receipt']['states']['navigation'] = 'unknown'
        receipt = json.dumps(
            value['receipt'],
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':'),
        )
        value['receipt_digest'] = hashlib.sha256(
            receipt.encode('utf-8')
        ).hexdigest()

    with pytest.raises(CampaignEvidenceError) as caught:
        parse_child_manifest(_mutated_child(make_unknown))
    assert caught.value.code == 'child_manifest_success_invalid'


def test_one_case_passed_campaign_schema_is_fixed_and_digest_bound():
    """One smoke case can form a fixed canonical passed manifest."""
    receipt = _receipt()
    manifest = TextGazeboCampaignManifest(receipt)
    value = json.loads(manifest.canonical_json())

    assert value['format'] == CAMPAIGN_EVIDENCE_FORMAT
    assert set(value) == {'format', 'receipt', 'receipt_digest'}
    assert set(value['receipt']) == {
        'campaign_id',
        'cases',
        'cleanup',
        'commit',
        'installed_digest',
        'physical_authorized',
        'simulation',
        'source_tree_digest',
        'stopped_early',
        'test_verdict',
        'total_duration_seconds',
    }
    assert value['receipt']['test_verdict'] == 'passed'
    assert value['receipt']['simulation'] is True
    assert value['receipt']['physical_authorized'] is False
    assert value['receipt']['cases'][0] == {
        'case_id': 'normal-smoke',
        'child_manifest_digest': _child_summary().manifest_digest,
        'cleanup': 'clean',
        'duration_seconds': 5.0,
        'error_code': 'none',
        'expected_block_code': 'none',
        'expected_outcome': 'succeeded',
        'fault_profile': 'none',
        'observed_block_code': 'none',
        'observed_outcome': 'succeeded',
        'ordinal': 1,
        'pressure': pressure_evidence_for(
            TextGazeboFaultProfile.NONE
        ).as_dict(),
        'profile': 'happy_path',
        'scenario_profile': 'happy_path',
        'safety_profile': 'none',
        'target_binding_digest': '6' * 64,
        'test_verdict': 'passed',
    }
    assert manifest.receipt_digest == receipt.digest()
    assert manifest.digest() == hashlib.sha256(
        manifest.canonical_json().encode('utf-8')
    ).hexdigest()
    assert '\n' not in manifest.canonical_json()


def test_campaign_preserves_explicit_order_and_requires_contiguous_ordinals():
    """Explicit 1-based ordinals must agree with canonical tuple order."""
    cases = (
        _case(ordinal=1, case_id='first'),
        _case(
            ordinal=2,
            case_id='second',
            child=_child_summary(run_digit='8', goal_digit='9'),
        ),
    )
    receipt = _receipt(cases=cases)
    assert [item['case_id'] for item in receipt.as_dict()['cases']] == [
        'first',
        'second',
    ]

    invalid = replace(cases[1], ordinal=3)
    with pytest.raises(ValueError, match='ordinals'):
        _receipt(cases=(cases[0], invalid))


def test_three_space_campaign_binds_distinct_semantic_targets():
    """A passed three-space receipt proves the selected target per case."""
    profiles = (
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboScenarioProfile.HAPPY_KITCHEN,
        TextGazeboScenarioProfile.HAPPY_BEDROOM,
    )
    cases = tuple(
        _case(
            ordinal=ordinal,
            case_id=f'space-{ordinal}',
            profile=profile.value,
            child=_child_summary(
                run_digit=run_digit,
                goal_digit=goal_digit,
                target_digit=target_digit,
                profile=profile,
            ),
        )
        for ordinal, profile, run_digit, goal_digit, target_digit in (
            (1, profiles[0], 'a', '1', '4'),
            (2, profiles[1], 'b', '2', '5'),
            (3, profiles[2], 'c', '3', '6'),
        )
    )

    receipt = _receipt(cases=cases)

    assert receipt.test_verdict is CampaignTestVerdict.PASSED
    assert [
        case['profile'] for case in receipt.as_dict()['cases']
    ] == [profile.value for profile in profiles]
    assert len({
        case['target_binding_digest']
        for case in receipt.as_dict()['cases']
    }) == 3


def test_three_safety_blocks_share_empty_goal_digest_and_campaign_passes():
    """Zero-effect cases may repeat the canonical empty Nav2 goal set."""
    profiles = (
        CampaignProfile.STALE_STATE,
        CampaignProfile.EMERGENCY_STOP,
        CampaignProfile.MAP_REVISION_CHANGED,
    )
    cases = tuple(
        _case(
            ordinal=ordinal,
            case_id=f'safety-{ordinal}',
            profile=profile.value,
            child=_child_summary(
                run_digit=run_digit,
                profile=TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
                safety_profile=(
                    campaign_profile_binding(profile).safety_profile
                ),
            ),
        )
        for ordinal, profile, run_digit in (
            (1, profiles[0], 'a'),
            (2, profiles[1], 'b'),
            (3, profiles[2], 'c'),
        )
    )

    receipt = _receipt(cases=cases)

    assert receipt.test_verdict is CampaignTestVerdict.PASSED
    public_cases = receipt.as_dict()['cases']
    assert [case['expected_outcome'] for case in public_cases] == [
        'blocked',
        'blocked',
        'blocked',
    ]
    assert [case['observed_block_code'] for case in public_cases] == [
        'robot_state_stale',
        'safety_emergency_stop',
        'target_binding_changed',
    ]
    assert [case['safety_profile'] for case in public_cases] == [
        'stale_state',
        'emergency_stop',
        'map_revision_changed',
    ]


def test_case_rejects_child_from_a_different_scenario_profile():
    """A campaign label cannot relabel a living-room child as kitchen."""
    with pytest.raises(ValueError, match='scenario profile'):
        _case(
            profile=TextGazeboScenarioProfile.HAPPY_KITCHEN.value,
            child=_child_summary(
                profile=TextGazeboScenarioProfile.HAPPY_PATH
            ),
        )


def test_case_rejects_child_from_a_different_fault_profile() -> None:
    child = _child_summary(
        profile=TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        fault_profile=TextGazeboFaultProfile.NONE,
    )

    with pytest.raises(ValueError, match='fault profile'):
        _case(profile='duplicate_request', child=child)


@pytest.mark.parametrize(
    'case_profile,fault_profile',
    (
        ('duplicate_request', TextGazeboFaultProfile.DUPLICATE_REQUEST),
        ('concurrent_approval', TextGazeboFaultProfile.CONCURRENT_APPROVAL),
        ('competing_workers', TextGazeboFaultProfile.COMPETING_WORKERS),
    ),
)
def test_exactly_once_case_accepts_living_room_with_matching_fault(
    case_profile,
    fault_profile,
) -> None:
    child = _child_summary(
        profile=TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        fault_profile=fault_profile,
    )

    evidence = _case(profile=case_profile, child=child)

    assert evidence.profile == case_profile
    assert evidence.child_manifest.scenario_profile is (
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM
    )
    assert evidence.child_manifest.fault_profile is fault_profile


def test_campaign_rejects_reused_binding_for_different_locations():
    """Different semantic locations cannot silently resolve to one target."""
    living = _case(
        case_id='living',
        profile=TextGazeboScenarioProfile.HAPPY_LIVING_ROOM.value,
        child=_child_summary(
            run_digit='a',
            goal_digit='1',
            target_digit='7',
            profile=TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        ),
    )
    kitchen = _case(
        ordinal=2,
        case_id='kitchen',
        profile=TextGazeboScenarioProfile.HAPPY_KITCHEN.value,
        child=_child_summary(
            run_digit='b',
            goal_digit='2',
            target_digit='7',
            profile=TextGazeboScenarioProfile.HAPPY_KITCHEN,
        ),
    )

    with pytest.raises(ValueError, match='different semantic targets'):
        _receipt(cases=(living, kitchen))


def test_campaign_rejects_empty_duplicate_and_reused_child_evidence():
    """Campaigns require isolated non-empty cases and child evidence."""
    with pytest.raises(ValueError, match='non-empty'):
        _receipt(cases=(), cleanup=_cleanup(0))

    first = _case(case_id='duplicate')
    duplicate = replace(first, ordinal=2)
    with pytest.raises(ValueError, match='identifiers'):
        _receipt(cases=(first, duplicate))

    reused = replace(first, ordinal=2, case_id='second')
    with pytest.raises(ValueError, match='run identifiers'):
        _receipt(cases=(first, reused))

    same_goal = _child_summary(run_digit='8')
    reused_goal = replace(
        same_goal,
        manifest_digest='9' * 64,
        receipt_digest='a' * 64,
        run_id='run-' + 'b' * 32,
    )
    with pytest.raises(ValueError, match='goal-set digests'):
        _receipt(cases=(
            _case(case_id='first', child=same_goal),
            _case(
                ordinal=2,
                case_id='second',
                child=reused_goal,
            ),
        ))


def test_campaign_rejects_child_provenance_mismatch():
    """Every child must bind to the exact aggregate provenance."""
    mismatched = _case(child=_child_summary(commit='9' * 40))
    with pytest.raises(ValueError, match='provenance'):
        _receipt(cases=(mismatched,))


def test_campaign_rejects_duration_and_cleanup_aggregate_mismatch():
    """Aggregate duration and cleanup must cover every child case."""
    with pytest.raises(ValueError, match='duration'):
        _receipt(total_duration_seconds=4.9)

    with pytest.raises(ValueError, match='cleanup aggregate'):
        _receipt(cleanup=_cleanup(2))


def test_partial_mismatch_and_failed_cases_cannot_claim_campaign_passed():
    """No incomplete or failing case can support a passed campaign."""
    trigger = _case(
        child_manifest=None,
        observed_outcome=ProductOutcome.NOT_OBSERVED,
        test_verdict=CaseTestVerdict.FAILED,
        error_code=CaseErrorCode.EXECUTOR_EXCEPTION,
        duration_seconds=0,
        cleanup=CaseCleanupState.NOT_OBSERVED,
    )
    partial = _case(
        ordinal=2,
        case_id='not-run-after-trigger',
        child_manifest=None,
        observed_outcome=ProductOutcome.NOT_OBSERVED,
        test_verdict=CaseTestVerdict.PARTIAL,
        error_code=CaseErrorCode.PREVIOUS_CASE_UNSAFE,
        duration_seconds=0,
        cleanup=CaseCleanupState.NOT_OBSERVED,
    )
    with pytest.raises(ValueError, match='verdict'):
        _receipt(
            cases=(trigger, partial),
            test_verdict=CampaignTestVerdict.PASSED,
            stopped_early=True,
            cleanup=_cleanup(
                0,
                completed=False,
                not_observed_case_count=2,
            ),
        )

    mismatch = _case(
        child_manifest=None,
        expected_outcome=ProductOutcome.SUCCEEDED,
        observed_outcome=ProductOutcome.BLOCKED,
        observed_block_code=SafetyBlockCode.ROBOT_STATE_STALE,
        test_verdict=CaseTestVerdict.FAILED,
        error_code=CaseErrorCode.PRODUCT_OUTCOME_MISMATCH,
    )
    with pytest.raises(ValueError, match='verdict'):
        _receipt(
            cases=(mismatch,),
            test_verdict=CampaignTestVerdict.PASSED,
        )

    failed = _case(
        child_manifest=None,
        observed_outcome=ProductOutcome.FAILED,
        test_verdict=CaseTestVerdict.FAILED,
        error_code=CaseErrorCode.EXECUTOR_FAILED,
    )
    with pytest.raises(ValueError, match='verdict'):
        _receipt(
            cases=(failed,),
            test_verdict=CampaignTestVerdict.PASSED,
        )


def test_passed_case_and_partial_case_require_consistent_error_codes():
    """Verdicts remain bound to stable diagnostic codes."""
    with pytest.raises(ValueError, match='passed case'):
        _case(error_code=CaseErrorCode.EXECUTOR_FAILED)
    with pytest.raises(ValueError, match='requires an error'):
        _case(
            child_manifest=None,
            test_verdict=CaseTestVerdict.PARTIAL,
        )


def test_case_evidence_requires_the_current_profile_allowlist():
    """Aggregate evidence cannot invent a profile outside campaign core."""
    with pytest.raises(ValueError, match='profile is not allowlisted'):
        _case(profile='arbitrary_shell')


def test_stopped_campaign_requires_a_trailing_not_run_suffix():
    """No passed case may appear after a fail-closed campaign stop."""
    failed = _case(
        child_manifest=None,
        observed_outcome=ProductOutcome.NOT_OBSERVED,
        test_verdict=CaseTestVerdict.FAILED,
        error_code=CaseErrorCode.EXECUTOR_EXCEPTION,
        cleanup=CaseCleanupState.NOT_OBSERVED,
        duration_seconds=0,
    )
    passed_after_stop = _case(
        ordinal=2,
        case_id='passed-after-stop',
        child=_child_summary(run_digit='8', goal_digit='9'),
    )
    with pytest.raises(ValueError, match='NOT_RUN suffix'):
        _receipt(
            cases=(failed, passed_after_stop),
            test_verdict=CampaignTestVerdict.FAILED,
            stopped_early=True,
            cleanup=_cleanup(
                1,
                completed=False,
                not_observed_case_count=1,
            ),
        )


def test_child_parser_rejects_inconsistent_phase_duration_total():
    """A child total duration must cover its sequential phase durations."""
    def shorten_total(value):
        value['receipt']['durations']['total_seconds'] = 1
        receipt = json.dumps(
            value['receipt'],
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':'),
        )
        value['receipt_digest'] = hashlib.sha256(
            receipt.encode('utf-8')
        ).hexdigest()

    with pytest.raises(CampaignEvidenceError) as caught:
        parse_child_manifest(_mutated_child(shorten_total))
    assert caught.value.code == 'child_manifest_success_invalid'


def test_child_parser_normalizes_excessive_json_nesting():
    """Deep bounded JSON fails with a stable code, not RecursionError."""
    payload = ('[' * 1500 + '0' + ']' * 1500 + '\n').encode('ascii')
    with pytest.raises(CampaignEvidenceError) as caught:
        parse_child_manifest(payload)
    assert caught.value.code == 'child_manifest_json_invalid'


def test_campaign_values_are_immutable_slot_only_and_repr_is_bounded():
    """Evidence objects are immutable and avoid identifier expansion."""
    receipt = _receipt()
    manifest = TextGazeboCampaignManifest(receipt)
    with pytest.raises(FrozenInstanceError):
        receipt.campaign_id = 'campaign-' + '8' * 32
    assert not hasattr(receipt, '__dict__')
    assert not hasattr(manifest, '__dict__')
    assert receipt.campaign_id not in repr(receipt)
    assert _child_summary().run_id not in repr(_child_summary())


def test_writer_creates_private_parent_and_owner_only_atomic_file(tmp_path):
    """Publishing creates only owner-private canonical evidence."""
    parent = tmp_path / 'private-evidence'
    destination = parent / 'campaign.json'
    manifest = TextGazeboCampaignManifest(_receipt())

    digest = write_campaign_manifest(destination, manifest)

    assert digest == manifest.digest()
    assert destination.read_text(encoding='utf-8') == (
        manifest.canonical_json() + '\n'
    )
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.stat().st_uid == os.getuid()
    assert list(parent.glob('.*.tmp')) == []


def test_writer_refuses_overwrite_and_symlink_paths(tmp_path):
    """Publishing never follows links or replaces existing evidence."""
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    destination = private / 'campaign.json'
    manifest = TextGazeboCampaignManifest(_receipt())
    write_campaign_manifest(destination, manifest)
    with pytest.raises(FileExistsError, match='already exists'):
        write_campaign_manifest(destination, manifest)

    target = private / 'target.json'
    target.write_text('unchanged', encoding='utf-8')
    alias = private / 'alias.json'
    alias.symlink_to(target.name)
    with pytest.raises(ValueError, match='symbolic link'):
        write_campaign_manifest(alias, manifest)
    assert target.read_text(encoding='utf-8') == 'unchanged'

    real_parent = tmp_path / 'real-parent'
    real_parent.mkdir(mode=0o700)
    parent_alias = tmp_path / 'parent-alias'
    parent_alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match='symbolic link'):
        write_campaign_manifest(parent_alias / 'receipt.json', manifest)


def test_concurrent_writers_publish_one_complete_campaign(tmp_path):
    """Concurrent publication selects exactly one complete manifest."""
    parent = tmp_path / 'private'
    parent.mkdir(mode=0o700)
    destination = parent / 'campaign.json'
    manifests = (
        TextGazeboCampaignManifest(_receipt()),
        TextGazeboCampaignManifest(
            _receipt(campaign_id='campaign-' + '8' * 32)
        ),
    )
    barrier = Barrier(2)
    outcomes = []

    def publish(manifest):
        barrier.wait()
        try:
            write_campaign_manifest(destination, manifest)
        except FileExistsError:
            outcomes.append('exists')
        else:
            outcomes.append('published')

    threads = [Thread(target=publish, args=(item,)) for item in manifests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ['exists', 'published']
    saved = destination.read_text(encoding='utf-8').rstrip('\n')
    assert saved in {item.canonical_json() for item in manifests}
    assert list(parent.glob('.*.tmp')) == []
