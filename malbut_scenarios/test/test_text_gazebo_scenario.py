"""Contracts for the bounded SWM25-135 scenario allowlist."""

from dataclasses import FrozenInstanceError

import pytest

from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboExecutionProfile,
    TextGazeboFaultProfile,
    TextGazeboSafetyProfile,
    TextGazeboScenarioProfile,
    coerce_execution_profile,
    coerce_fault_profile,
    coerce_safety_profile,
    coerce_scenario_profile,
    execution_contract,
    pressure_contract,
    safety_contract,
    scenario_spec,
)


@pytest.mark.parametrize(
    'profile,request_text,location',
    (
        ('happy_path', '거실로 가줘', '거실'),
        ('happy_living_room', '거실로 가줘', '거실'),
        ('happy_kitchen', '주방으로 가줘', '주방'),
        ('happy_bedroom', '침실로 가줘', '침실'),
    ),
)
def test_allowlisted_profiles_have_canonical_bindings(
    profile, request_text, location,
) -> None:
    spec = scenario_spec(profile)

    assert spec.profile.value == profile
    assert spec.request_text == request_text
    assert spec.location == location


@pytest.mark.parametrize('value', ('거실', 'kitchen', '', 'happy_unknown', 1))
def test_raw_or_unknown_profile_is_rejected(value) -> None:
    with pytest.raises(ValueError):
        coerce_scenario_profile(value)


def test_scenario_specs_are_immutable_and_stable() -> None:
    profile = TextGazeboScenarioProfile.HAPPY_KITCHEN
    first = scenario_spec(profile)

    assert scenario_spec(profile) is first
    assert repr(first) == (
        "TextGazeboScenarioSpec(profile='happy_kitchen')"
    )
    assert '주방' not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.location = '침실'


@pytest.mark.parametrize(
    'profile,counts',
    (
        ('none', (1, 3, 1, 1, 1, 0)),
        ('duplicate_request', (2, 3, 1, 2, 1, 1)),
        ('concurrent_approval', (1, 4, 1, 2, 1, 1)),
        ('competing_workers', (1, 3, 2, 2, 1, 1)),
    ),
)
def test_fault_profiles_have_exact_pressure_contracts(
    profile,
    counts,
) -> None:
    selected = coerce_fault_profile(profile)
    contract = pressure_contract(selected)

    assert selected is TextGazeboFaultProfile(profile)
    assert (
        contract.request_attempt_count,
        contract.approval_attempt_count,
        contract.worker_contender_count,
        contract.pressure_contender_count,
        contract.pressure_winner_count,
        contract.pressure_nonwinner_count,
    ) == counts


@pytest.mark.parametrize('value', ('duplicate', 'shell', '', 1))
def test_unknown_fault_profile_is_rejected(value) -> None:
    with pytest.raises(ValueError):
        coerce_fault_profile(value)


@pytest.mark.parametrize(
    'profile,result_code,fault_count,map_switch_count',
    (
        ('none', None, 0, 0),
        ('stale_state', 'robot_state_stale', 1, 0),
        ('emergency_stop', 'safety_emergency_stop', 1, 0),
        ('map_revision_changed', 'target_binding_changed', 1, 1),
    ),
)
def test_safety_profiles_have_exact_blocking_contracts(
    profile,
    result_code,
    fault_count,
    map_switch_count,
) -> None:
    selected = coerce_safety_profile(profile)
    contract = safety_contract(selected)

    assert selected is TextGazeboSafetyProfile(profile)
    assert contract.result_code == result_code
    assert contract.fault_application_count == fault_count
    assert contract.map_switch_count == map_switch_count


@pytest.mark.parametrize('value', ('stale', 'e_stop', 'map', '', 1))
def test_unknown_safety_profile_is_rejected(value) -> None:
    with pytest.raises(ValueError):
        coerce_safety_profile(value)


@pytest.mark.parametrize(
    'profile,result_code,fault_count,start_drop,status_drop,goals,'
    'terminals,unavailable_count',
    (
        ('none', None, 0, 0, 0, 1, 1, 0),
        (
            'nav2_unavailable',
            'navigation_start_outcome_unknown',
            1, 0, 0, 0, 0, 1,
        ),
        (
            'start_response_lost',
            'navigation_start_outcome_unknown',
            1, 1, 0, 1, 1, 0,
        ),
        (
            'terminal_status_response_lost',
            'navigation_status_outcome_unknown',
            1, 0, 1, 1, 1, 0,
        ),
    ),
)
def test_execution_profiles_have_exact_unknown_result_contracts(
    profile,
    result_code,
    fault_count,
    start_drop,
    status_drop,
    goals,
    terminals,
    unavailable_count,
) -> None:
    selected = coerce_execution_profile(profile)
    contract = execution_contract(selected)

    assert selected is TextGazeboExecutionProfile(profile)
    assert contract.result_code == result_code
    assert contract.fault_application_count == fault_count
    assert contract.start_forward_count == (
        0 if selected is TextGazeboExecutionProfile.NONE else 1
    )
    assert contract.start_response_drop_count == start_drop
    assert contract.terminal_status_response_drop_count == status_drop
    assert contract.expected_nav2_goal_count == goals
    assert contract.expected_nav2_terminal_count == terminals
    assert contract.unavailable_endpoint_count == unavailable_count


@pytest.mark.parametrize(
    'value',
    ('start_timeout', 'drop_all', 'status_error', '', 1, True),
)
def test_unknown_execution_profile_is_rejected(value) -> None:
    with pytest.raises(ValueError):
        coerce_execution_profile(value)
