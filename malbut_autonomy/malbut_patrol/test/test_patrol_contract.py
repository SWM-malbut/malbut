"""Contract tests for patrol ownership and installed defaults."""

from pathlib import Path
from xml.etree import ElementTree

import yaml
import pytest

from malbut_patrol.geometry import yaw_to_quaternion
from malbut_patrol.patrol_manager import PatrolManager
from malbut_patrol.patrol_state import PatrolProgress
from malbut_patrol.route_loader import load_route


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_patrol_package_does_not_depend_on_simulation_or_publish_velocity():
    package = ElementTree.parse(PACKAGE_ROOT / 'package.xml').getroot()
    dependencies = {
        element.text
        for tag in ('depend', 'exec_depend')
        for element in package.findall(tag)
    }
    manager = (
        PACKAGE_ROOT
        / 'malbut_patrol'
        / 'patrol_manager.py'
    ).read_text(encoding='utf-8')

    assert 'malbut_gazebo' not in dependencies
    assert 'slam_toolbox' not in dependencies
    assert '/cmd_vel' not in manager
    assert 'NavigateToPose' in manager


def test_default_route_is_valid_and_identifies_the_saved_map():
    route_file = (
        PACKAGE_ROOT
        / 'config'
        / 'routes'
        / 'small_house_patrol.yaml'
    )
    route = load_route(route_file)
    raw = yaml.safe_load(route_file.read_text(encoding='utf-8'))

    assert route.map_id == 'small_house'
    assert route.frame_id == 'map'
    assert len(route.waypoints) >= 3
    assert raw['schedule']['mode'] == 'interval'


def test_planar_yaw_conversion_produces_a_unit_quaternion():
    quaternion = yaw_to_quaternion(1.5707963267948966)
    squared_norm = sum(value * value for value in quaternion)

    assert abs(squared_norm - 1.0) < 1e-12
    assert abs(quaternion[2] - 0.7071067811865475) < 1e-12
    assert abs(quaternion[3] - 0.7071067811865476) < 1e-12


def test_launch_exposes_safe_manual_start_and_relative_nav2_action():
    launch_text = (
        PACKAGE_ROOT / 'launch' / 'patrol.launch.py'
    ).read_text(encoding='utf-8')

    assert "default_value='false'" in launch_text
    assert "default_value='navigate_to_pose'" in launch_text
    assert "package='malbut_patrol'" in launch_text
    assert 'malbut_gazebo' not in launch_text


def test_route_reload_cannot_reset_an_active_patrol():
    """Refreshing Room-derived points must not bypass single-run ownership."""
    route_file = (
        PACKAGE_ROOT
        / 'config'
        / 'routes'
        / 'small_house_patrol.yaml'
    )
    route = load_route(route_file)
    manager = object.__new__(PatrolManager)
    manager._route_file = route_file
    manager._route = route
    manager._progress = PatrolProgress(route)
    manager._progress.start()

    with pytest.raises(RuntimeError, match='cannot start while state'):
        manager._reload_route()

    assert manager._progress.is_active
