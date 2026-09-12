"""Smoke checks for the installed tracking entry points and public action."""

from ament_index_python.packages import (
    get_package_prefix,
    get_package_share_directory,
)
from malbut_interfaces.action import FollowPerson
import os
from pathlib import Path


def test_installed_tracking_entry_points_and_runtime_assets():
    """Check build outputs instead of matching CMake or wrapper source text."""
    executables = Path(get_package_prefix('malbut_tracking')) / 'lib' / 'malbut_tracking'
    for name in (
        'lidar_foreground_preprocessor',
        'person_follower',
        'person_localizer',
        'person_tracking_benchmark',
    ):
        path = executables / name
        assert path.is_file(), path
        assert os.access(path, os.X_OK), path

    share = Path(get_package_share_directory('malbut_tracking'))
    for relative in (
        'config/person_following.yaml',
        'config/lidar_foreground.yaml',
        'launch/person_following.launch.py',
        'launch/lidar_foreground.launch.py',
        'benchmark/config/benchmark.yaml',
        'benchmark/config/scenarios.yaml',
        'benchmark/launch/person_tracking_benchmark.launch.py',
        'benchmark/actors',
    ):
        assert (share / relative).exists(), relative


def test_generated_follow_person_action_contract():
    """Keep target selection and result fields compatible for callers."""
    assert FollowPerson.Goal.VISIBLE_PERSON == 0
    assert FollowPerson.Goal.REGISTERED_PERSON == 1
    assert FollowPerson.Goal.get_fields_and_field_types() == {
        'target_mode': 'uint8',
        'target_person_id': 'string',
        'desired_distance_m': 'float',
    }
    assert FollowPerson.Result.get_fields_and_field_types() == {
        'success': 'boolean',
        'final_state': 'string',
        'message': 'string',
    }
    assert FollowPerson.Feedback.get_fields_and_field_types() == {
        'state': 'string',
        'target_visible': 'boolean',
    }
