"""Contract tests for the reproducible SLAM entry point."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch_ros.actions import Node
import yaml

from malbut_description.variant_config import load_variant_arguments


GAZEBO_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTION_ROOT = GAZEBO_ROOT.parent / "malbut_description"
PROFILE = (
    DESCRIPTION_ROOT
    / "config"
    / "ultimate_orin_nx_super_mecanum.yaml"
)


def test_slam_sensor_contract_matches_the_robot_profile():
    """SLAM consumes the simulated robot frames, topic, and LiDAR range."""
    parameters = yaml.safe_load(
        (GAZEBO_ROOT / "config" / "slam.yaml").read_text(
            encoding="utf-8"
        )
    )["/**"]["ros__parameters"]
    robot_arguments = load_variant_arguments(PROFILE)

    assert parameters["map_frame"] == "map"
    assert parameters["odom_frame"] == "odom"
    assert parameters["base_frame"] == "base_footprint"
    assert parameters["scan_topic"] == "scan"
    assert parameters["max_laser_range"] == robot_arguments[
        "lidar_max_range"
    ]
    assert parameters[
        "scan_buffer_maximum_scan_distance"
    ] == robot_arguments["lidar_max_range"]


def test_slam_rviz_view_displays_the_map_and_lidar_scan():
    """The checked-in RViz view visualizes both mapping outputs."""
    config = yaml.safe_load(
        (GAZEBO_ROOT / "rviz" / "slam.rviz").read_text(
            encoding="utf-8"
        )
    )
    manager = config["Visualization Manager"]
    displays = {
        display["Class"]: display for display in manager["Displays"]
    }

    assert manager["Global Options"]["Fixed Frame"] == "map"
    assert displays["rviz_default_plugins/Map"]["Topic"][
        "Value"
    ] == "/map"
    scan_topic = displays["rviz_default_plugins/LaserScan"]["Topic"]
    assert scan_topic["Value"] == "/scan"
    assert scan_topic["Reliability Policy"] == "Best Effort"


def test_slam_launch_uses_the_canonical_simulator_and_async_mapper():
    """The public launch file composes the maintained entry points."""
    launch_files = list(
        (GAZEBO_ROOT / "launch").rglob("slam.launch.py")
    )
    assert launch_files == [
        GAZEBO_ROOT / "launch" / "slam.launch.py"
    ]
    launch_text = launch_files[0].read_text(encoding="utf-8")

    assert '"worlds.launch.py"' in launch_text
    assert '"online_async_launch.py"' in launch_text
    assert '"rviz": "false"' in launch_text
    assert '"lidar_enabled": "true"' in launch_text
    assert '"spawn_robot": "true"' in launch_text
    assert '"bridge": "true"' in launch_text


def test_simulator_arguments_cannot_disable_the_slam_rviz_node():
    """The child simulator's rviz=false must remain in a scoped context."""
    launch_file = GAZEBO_ROOT / "launch" / "slam.launch.py"
    spec = importlib.util.spec_from_file_location(
        "malbut_slam_launch", launch_file
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()

    rviz_argument = next(
        entity
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
        and entity.name == "rviz"
    )
    assert rviz_argument.default_value[0].text == "true"
    assert any(isinstance(entity, Node) for entity in description.entities)

    simulation_group = next(
        entity
        for entity in description.entities
        if isinstance(entity, GroupAction)
    )
    scoped_actions = simulation_group.execute(LaunchContext())
    assert type(scoped_actions[0]).__name__ == "PushLaunchConfigurations"
    assert type(scoped_actions[-1]).__name__ == "PopLaunchConfigurations"

    simulation = next(
        entity
        for entity in scoped_actions
        if isinstance(entity, IncludeLaunchDescription)
    )
    assert dict(simulation.launch_arguments)["rviz"] == "false"


def test_slam_records_localization_for_navigation_handoff():
    """Mapping must retain map-to-odom before its publisher is stopped."""
    launch_file = GAZEBO_ROOT / "launch" / "slam.launch.py"
    spec = importlib.util.spec_from_file_location(
        "malbut_slam_handoff_launch", launch_file
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()
    nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
    ]
    executables = {node._Node__node_executable for node in nodes}
    assert "record_localization_state" in executables
    arguments = {
        entity.name
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert "localization_state" in arguments
