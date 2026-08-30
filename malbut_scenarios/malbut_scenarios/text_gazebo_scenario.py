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


class TextGazeboSafetyProfile(str, Enum):
    """Allowlisted dispatch-time Safety conditions for SWM25-137."""

    NONE = 'none'
    STALE_STATE = 'stale_state'
    EMERGENCY_STOP = 'emergency_stop'
    MAP_REVISION_CHANGED = 'map_revision_changed'


class TextGazeboExecutionProfile(str, Enum):
    """Allowlisted, default-off execution outcomes for SWM25-138."""

    NONE = 'none'
    NAV2_UNAVAILABLE = 'nav2_unavailable'
    START_RESPONSE_LOST = 'start_response_lost'
    TERMINAL_STATUS_RESPONSE_LOST = 'terminal_status_response_lost'


@dataclass(frozen=True, slots=True)
class TextGazeboPressureContract:
    """Exact public counters required for one bounded pressure profile."""

    request_attempt_count: int
    approval_attempt_count: int
    worker_contender_count: int
    pressure_contender_count: int
    pressure_winner_count: int
    pressure_nonwinner_count: int


@dataclass(frozen=True, slots=True)
class TextGazeboSafetyContract:
    """Exact product result and injection counts for one Safety profile."""

    result_code: str | None
    fault_application_count: int
    map_switch_count: int


@dataclass(frozen=True, slots=True)
class TextGazeboExecutionContract:
    """
    Record exact fault-axis observations and ROS effects for one profile.

    ``start_forward_count`` belongs to the bounded fault observation, not to
    the run-wide product-effect totals.  It is therefore zero for ``NONE``
    even though a normal successful run has one Robot Web start; that total
    lives in ``EvidenceCounts.robot_web_start_count``.
    """

    result_code: str | None
    fault_application_count: int
    start_forward_count: int
    start_response_drop_count: int
    terminal_status_response_drop_count: int
    expected_nav2_goal_count: int
    expected_nav2_terminal_count: int
    unavailable_endpoint_count: int


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


_SAFETY_CONTRACTS: Mapping[
    TextGazeboSafetyProfile,
    TextGazeboSafetyContract,
] = MappingProxyType({
    TextGazeboSafetyProfile.NONE: TextGazeboSafetyContract(
        result_code=None,
        fault_application_count=0,
        map_switch_count=0,
    ),
    TextGazeboSafetyProfile.STALE_STATE: TextGazeboSafetyContract(
        result_code='robot_state_stale',
        fault_application_count=1,
        map_switch_count=0,
    ),
    TextGazeboSafetyProfile.EMERGENCY_STOP: TextGazeboSafetyContract(
        result_code='safety_emergency_stop',
        fault_application_count=1,
        map_switch_count=0,
    ),
    TextGazeboSafetyProfile.MAP_REVISION_CHANGED: (
        TextGazeboSafetyContract(
            result_code='target_binding_changed',
            fault_application_count=1,
            map_switch_count=1,
        )
    ),
})


_EXECUTION_CONTRACTS: Mapping[
    TextGazeboExecutionProfile,
    TextGazeboExecutionContract,
] = MappingProxyType({
    TextGazeboExecutionProfile.NONE: TextGazeboExecutionContract(
        result_code=None,
        fault_application_count=0,
        start_forward_count=0,
        start_response_drop_count=0,
        terminal_status_response_drop_count=0,
        expected_nav2_goal_count=1,
        expected_nav2_terminal_count=1,
        unavailable_endpoint_count=0,
    ),
    TextGazeboExecutionProfile.NAV2_UNAVAILABLE: (
        TextGazeboExecutionContract(
            result_code='navigation_start_outcome_unknown',
            fault_application_count=1,
            start_forward_count=1,
            start_response_drop_count=0,
            terminal_status_response_drop_count=0,
            expected_nav2_goal_count=0,
            expected_nav2_terminal_count=0,
            unavailable_endpoint_count=1,
        )
    ),
    TextGazeboExecutionProfile.START_RESPONSE_LOST: (
        TextGazeboExecutionContract(
            result_code='navigation_start_outcome_unknown',
            fault_application_count=1,
            start_forward_count=1,
            start_response_drop_count=1,
            terminal_status_response_drop_count=0,
            expected_nav2_goal_count=1,
            expected_nav2_terminal_count=1,
            unavailable_endpoint_count=0,
        )
    ),
    TextGazeboExecutionProfile.TERMINAL_STATUS_RESPONSE_LOST: (
        TextGazeboExecutionContract(
            result_code='navigation_status_outcome_unknown',
            fault_application_count=1,
            start_forward_count=1,
            start_response_drop_count=0,
            terminal_status_response_drop_count=1,
            expected_nav2_goal_count=1,
            expected_nav2_terminal_count=1,
            unavailable_endpoint_count=0,
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


def coerce_safety_profile(
    value: Union[TextGazeboSafetyProfile, str],
) -> TextGazeboSafetyProfile:
    """Accept only an enum or exact dispatch Safety profile token."""
    if isinstance(value, TextGazeboSafetyProfile):
        return value
    if type(value) is not str:
        raise ValueError('text Gazebo Safety profile is invalid')
    try:
        return TextGazeboSafetyProfile(value)
    except ValueError as error:
        raise ValueError(
            'text Gazebo Safety profile is not allowlisted'
        ) from error


def safety_contract(
    profile: Union[TextGazeboSafetyProfile, str],
) -> TextGazeboSafetyContract:
    """Return the exact fail-closed contract for one Safety profile."""
    return _SAFETY_CONTRACTS[coerce_safety_profile(profile)]


def coerce_execution_profile(
    value: Union[TextGazeboExecutionProfile, str],
) -> TextGazeboExecutionProfile:
    """Accept only an enum or exact execution fault profile token."""
    if isinstance(value, TextGazeboExecutionProfile):
        return value
    if type(value) is not str:
        raise ValueError('text Gazebo execution profile is invalid')
    try:
        return TextGazeboExecutionProfile(value)
    except ValueError as error:
        raise ValueError(
            'text Gazebo execution profile is not allowlisted'
        ) from error


def execution_contract(
    profile: Union[TextGazeboExecutionProfile, str],
) -> TextGazeboExecutionContract:
    """Return the exact result contract for one execution profile."""
    return _EXECUTION_CONTRACTS[coerce_execution_profile(profile)]


__all__ = [
    'TextGazeboExecutionContract',
    'TextGazeboExecutionProfile',
    'TextGazeboFaultProfile',
    'TextGazeboPressureContract',
    'TextGazeboSafetyContract',
    'TextGazeboSafetyProfile',
    'TextGazeboScenarioProfile',
    'TextGazeboScenarioSpec',
    'coerce_execution_profile',
    'coerce_fault_profile',
    'coerce_scenario_profile',
    'coerce_safety_profile',
    'execution_contract',
    'pressure_contract',
    'safety_contract',
    'scenario_spec',
]
