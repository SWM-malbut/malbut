"""Contract tests for the reproducible SLAM entry point."""

from pathlib import Path

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
