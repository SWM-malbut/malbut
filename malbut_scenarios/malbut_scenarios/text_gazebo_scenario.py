"""Bounded, immutable scenarios for the text-to-Gazebo acceptance path."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Union


class TextGazeboScenarioProfile(str, Enum):
    """Public identifiers for the finite set of approved test scenarios."""

    HAPPY_PATH = 'happy_path'
    HAPPY_LIVING_ROOM = 'happy_living_room'
    HAPPY_KITCHEN = 'happy_kitchen'
    HAPPY_BEDROOM = 'happy_bedroom'


@dataclass(frozen=True, repr=False, slots=True)
class TextGazeboScenarioSpec:
    """Server-owned natural-language request and semantic target binding."""

    profile: TextGazeboScenarioProfile
    request_text: str
    location: str

    def __repr__(self) -> str:
        """Render only the bounded profile, never request or location text."""
        return (
            'TextGazeboScenarioSpec('
            f'profile={self.profile.value!r})'
        )


_SPECS: Mapping[TextGazeboScenarioProfile, TextGazeboScenarioSpec] = (
    MappingProxyType({
        TextGazeboScenarioProfile.HAPPY_PATH: TextGazeboScenarioSpec(
            TextGazeboScenarioProfile.HAPPY_PATH, '거실로 가줘', '거실'
        ),
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM: TextGazeboScenarioSpec(
            TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
            '거실로 가줘', '거실',
        ),
        TextGazeboScenarioProfile.HAPPY_KITCHEN: TextGazeboScenarioSpec(
            TextGazeboScenarioProfile.HAPPY_KITCHEN,
            '주방으로 가줘', '주방',
        ),
        TextGazeboScenarioProfile.HAPPY_BEDROOM: TextGazeboScenarioSpec(
            TextGazeboScenarioProfile.HAPPY_BEDROOM,
            '침실로 가줘', '침실',
        ),
    })
)


def coerce_scenario_profile(
    value: Union[TextGazeboScenarioProfile, str],
) -> TextGazeboScenarioProfile:
    """Accept an enum or exact public profile token, never a raw location."""
    if isinstance(value, TextGazeboScenarioProfile):
        return value
    if type(value) is not str:
        raise ValueError('text Gazebo scenario profile is invalid')
    try:
        return TextGazeboScenarioProfile(value)
    except ValueError as error:
        raise ValueError(
            'text Gazebo scenario profile is not allowlisted'
        ) from error


def scenario_spec(
    profile: Union[TextGazeboScenarioProfile, str],
) -> TextGazeboScenarioSpec:
    """Return the immutable binding for an allowlisted profile."""
    return _SPECS[coerce_scenario_profile(profile)]


__all__ = [
    'TextGazeboScenarioProfile',
    'TextGazeboScenarioSpec',
    'coerce_scenario_profile',
    'scenario_spec',
]
