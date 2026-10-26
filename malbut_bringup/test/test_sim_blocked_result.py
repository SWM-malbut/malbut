"""Reject controller, TF and lifecycle failures in the blocked-passage oracle."""

from copy import deepcopy

from keepout_navigation_sim import validate_blocked_result
import pytest


@pytest.fixture
def rejected():
    """Create a stationary robot whose first request found no valid path."""
    return {'success': False, 'legs': [{'planning_status': 6,
            'planning_failure': 'no_valid_path', 'path': []}], 'errors': [],
            'commands': [[0., 0., 0.]], 'pose_samples': [[0., 0., 0., 0.]],
            'footprint_intersections': {'restricted_zone': 0, 'physical_geometry': 0}}


def test_accept_confirmed_no_path(rejected):
    """An expected geometric rejection passes without moving the robot."""
    validate_blocked_result(rejected)


@pytest.mark.parametrize('change', [
    {'planning_status': 4, 'navigation_status': 6},
    {'planning_status': 5},
    {'planning_failure': None},
    {'path': [[0., 0.], [3.15, 0.]]},
    {'navigation_status': 6},
])
def test_reject_other_action_outcomes(rejected, change):
    """Action abort alone does not establish that the passage is blocked."""
    rejected['legs'][0].update(change)
    with pytest.raises(AssertionError):
        validate_blocked_result(rejected)


@pytest.mark.parametrize('change', [
    {'errors': [{'message': 'Transform or controller failure'}]},
    {'commands': [[.1, 0., 0.]]},
    {'pose_samples': [[0., .6, 0., 0.]]},
    {'footprint_intersections': {'physical_geometry': 1}},
    {'success': True},
    {'legs': []},
])
def test_reject_faults_motion_and_boundary_intrusion(rejected, change):
    """Neither faults nor accidental motion qualify as a correct refusal."""
    rejected.update(deepcopy(change))
    with pytest.raises(AssertionError):
        validate_blocked_result(rejected)
