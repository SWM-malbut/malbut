"""Contracts for the bounded SWM25-135 scenario allowlist."""

from dataclasses import FrozenInstanceError

import pytest

from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboFaultProfile,
    TextGazeboScenarioProfile,
    coerce_fault_profile,
    coerce_scenario_profile,
    pressure_contract,
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
