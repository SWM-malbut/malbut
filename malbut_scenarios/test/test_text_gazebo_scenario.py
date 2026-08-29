"""Contracts for the bounded SWM25-135 scenario allowlist."""

from dataclasses import FrozenInstanceError

import pytest

from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboScenarioProfile,
    coerce_scenario_profile,
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
