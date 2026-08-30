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


class TextGazeboFaultProfile(str, Enum):
    """Public identifiers for bounded exactly-once pressure profiles."""

    NONE = 'none'
    DUPLICATE_REQUEST = 'duplicate_request'
    CONCURRENT_APPROVAL = 'concurrent_approval'
    COMPETING_WORKERS = 'competing_workers'


@dataclass(frozen=True, slots=True)
class TextGazeboPressureContract:
    """Exact public counters required for one bounded pressure profile."""

    request_attempt_count: int
    approval_attempt_count: int
    worker_contender_count: int
    pressure_contender_count: int
    pressure_winner_count: int
    pressure_nonwinner_count: int


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


_PRESSURE_CONTRACTS: Mapping[
    TextGazeboFaultProfile,
    TextGazeboPressureContract,
] = MappingProxyType({
    TextGazeboFaultProfile.NONE: TextGazeboPressureContract(
        request_attempt_count=1,
        approval_attempt_count=3,
        worker_contender_count=1,
        pressure_contender_count=1,
        pressure_winner_count=1,
        pressure_nonwinner_count=0,
    ),
    TextGazeboFaultProfile.DUPLICATE_REQUEST: (
        TextGazeboPressureContract(
            request_attempt_count=2,
            approval_attempt_count=3,
            worker_contender_count=1,
            pressure_contender_count=2,
            pressure_winner_count=1,
            pressure_nonwinner_count=1,
        )
    ),
    TextGazeboFaultProfile.CONCURRENT_APPROVAL: (
        TextGazeboPressureContract(
            request_attempt_count=1,
            approval_attempt_count=4,
            worker_contender_count=1,
            pressure_contender_count=2,
            pressure_winner_count=1,
            pressure_nonwinner_count=1,
        )
    ),
    TextGazeboFaultProfile.COMPETING_WORKERS: (
        TextGazeboPressureContract(
            request_attempt_count=1,
            approval_attempt_count=3,
            worker_contender_count=2,
            pressure_contender_count=2,
            pressure_winner_count=1,
            pressure_nonwinner_count=1,
        )
    ),
})


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


def coerce_fault_profile(
    value: Union[TextGazeboFaultProfile, str],
) -> TextGazeboFaultProfile:
    """Accept an enum or exact fault token, never caller-defined behavior."""
    if isinstance(value, TextGazeboFaultProfile):
        return value
    if type(value) is not str:
        raise ValueError('text Gazebo fault profile is invalid')
    try:
        return TextGazeboFaultProfile(value)
    except ValueError as error:
        raise ValueError(
            'text Gazebo fault profile is not allowlisted'
        ) from error


def pressure_contract(
    profile: Union[TextGazeboFaultProfile, str],
) -> TextGazeboPressureContract:
    """Return exact evidence counters for one allowlisted fault profile."""
    return _PRESSURE_CONTRACTS[coerce_fault_profile(profile)]


__all__ = [
    'TextGazeboFaultProfile',
    'TextGazeboPressureContract',
    'TextGazeboScenarioProfile',
    'TextGazeboScenarioSpec',
    'coerce_fault_profile',
    'coerce_scenario_profile',
    'pressure_contract',
    'scenario_spec',
]
