"""Launch a catalogued Malbut environment through the canonical simulator."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration

from malbut_gazebo.world_catalog import resolve_world


GAZEBO_PACKAGE = "malbut_gazebo"
FORWARDED_ARGUMENTS = (
    "variant_config",
    "entity_name",
    "spawn_timeout",
    "gui",
    "headless",
    "paused",
    "use_sim_time",
    "rviz",
    "lidar_enabled",
    "depth_camera_enabled",
    "imu_enabled",
    "verbosity",
    "spawn_robot",
    "bridge",
    "iterations",
)


def _launch_setup(context):
    gazebo_share = Path(get_package_share_directory(GAZEBO_PACKAGE))
    world_name = LaunchConfiguration("world_name").perform(context)
    world_file, world_config = resolve_world(
        gazebo_share / "config" / "worlds.yaml",
        gazebo_share / "worlds",
        world_name,
    )

    pose = {}
    for name in ("x", "y", "z", "yaw"):
        override = LaunchConfiguration(name).perform(context).strip()
        pose[name] = override or str(world_config["spawn"][name])

    launch_arguments = {
        name: LaunchConfiguration(name) for name in FORWARDED_ARGUMENTS
    }
    launch_arguments.update({"world": str(world_file), **pose})
    return [
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                str(gazebo_share / "launch" / "simulation.launch.py")
            ),
            launch_arguments=launch_arguments.items(),
        )
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "world_name",
                default_value="empty",
                description=(
                    "World catalog key: empty, test_arena, robocup_home, "
                    "or small_house."
                ),
            ),
            DeclareLaunchArgument(
                "variant_config",
                default_value="rosorin_ultimate_mecanum.yaml",
            ),
            DeclareLaunchArgument("entity_name", default_value="malbut"),
            DeclareLaunchArgument("spawn_timeout", default_value="60"),
            DeclareLaunchArgument(
                "x",
                default_value="",
                description="Override catalog spawn X; empty uses the default.",
            ),
            DeclareLaunchArgument(
                "y",
                default_value="",
                description="Override catalog spawn Y; empty uses the default.",
            ),
            DeclareLaunchArgument(
                "z",
                default_value="",
                description="Override catalog spawn Z; empty uses the default.",
            ),
            DeclareLaunchArgument(
                "yaw",
                default_value="",
                description=(
                    "Override catalog spawn yaw; empty uses the default."
                ),
            ),
            DeclareLaunchArgument("gui", default_value="true"),
            DeclareLaunchArgument("headless", default_value="false"),
            DeclareLaunchArgument("paused", default_value="false"),
            DeclareLaunchArgument("use_sim_time", default_value="true"),
            DeclareLaunchArgument("rviz", default_value="false"),
            DeclareLaunchArgument("lidar_enabled", default_value="true"),
            DeclareLaunchArgument(
                "depth_camera_enabled",
                default_value="true",
            ),
            DeclareLaunchArgument("imu_enabled", default_value="true"),
            DeclareLaunchArgument("verbosity", default_value="2"),
            DeclareLaunchArgument(
                "spawn_robot",
                default_value="true",
                description="Start robot_state_publisher and spawn the robot.",
            ),
            DeclareLaunchArgument(
                "bridge",
                default_value="true",
                description="Start the configured ROS-Gazebo bridge.",
            ),
            DeclareLaunchArgument(
                "iterations",
                default_value="",
                description=(
                    "Stop after this many Gazebo iterations; empty runs until "
                    "shutdown."
                ),
            ),
            OpaqueFunction(function=_launch_setup),
        ]
    )
