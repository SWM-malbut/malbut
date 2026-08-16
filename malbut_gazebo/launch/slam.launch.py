"""Launch a Malbut simulation with online SLAM and its RViz view."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


GAZEBO_PACKAGE = "malbut_gazebo"
SIMULATION_ARGUMENTS = (
    "world_name",
    "variant_config",
    "entity_name",
    "spawn_timeout",
    "x",
    "y",
    "z",
    "yaw",
    "gui",
    "headless",
    "paused",
    "use_sim_time",
    "depth_camera_enabled",
    "imu_enabled",
    "verbosity",
    "iterations",
)


def generate_launch_description():
    gazebo_share = Path(get_package_share_directory(GAZEBO_PACKAGE))
    slam_toolbox_share = Path(
        get_package_share_directory("slam_toolbox")
    )

    simulation_arguments = {
        name: LaunchConfiguration(name) for name in SIMULATION_ARGUMENTS
    }
    simulation_arguments.update(
        {
            "rviz": "false",
            "lidar_enabled": "true",
            "spawn_robot": "true",
            "bridge": "true",
        }
    )

    simulation = GroupAction(
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    str(gazebo_share / "launch" / "worlds.launch.py")
                ),
                launch_arguments=simulation_arguments.items(),
            )
        ],
    )
    slam_toolbox = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(
                slam_toolbox_share
                / "launch"
                / "online_async_launch.py"
            )
        ),
        launch_arguments={
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "slam_params_file": LaunchConfiguration("slam_params_file"),
        }.items(),
    )
    rviz = Node(
        package="rviz2",
        executable="rviz2",
        arguments=["-d", LaunchConfiguration("rviz_config")],
        parameters=[
            {"use_sim_time": LaunchConfiguration("use_sim_time")}
        ],
        condition=IfCondition(LaunchConfiguration("rviz")),
        output="screen",
    )
    localization_recorder = Node(
        package=GAZEBO_PACKAGE,
        executable="record_localization_state",
        name="localization_state_recorder",
        parameters=[{
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "state_path": LaunchConfiguration("localization_state"),
        }],
        output="screen",
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "world_name",
                default_value="small_house",
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
            DeclareLaunchArgument("x", default_value=""),
            DeclareLaunchArgument("y", default_value=""),
            DeclareLaunchArgument("z", default_value=""),
            DeclareLaunchArgument("yaw", default_value=""),
            DeclareLaunchArgument("gui", default_value="true"),
            DeclareLaunchArgument("headless", default_value="false"),
            DeclareLaunchArgument("paused", default_value="false"),
            DeclareLaunchArgument("use_sim_time", default_value="true"),
            DeclareLaunchArgument(
                "depth_camera_enabled",
                default_value="true",
            ),
            DeclareLaunchArgument("imu_enabled", default_value="true"),
            DeclareLaunchArgument("verbosity", default_value="2"),
            DeclareLaunchArgument("iterations", default_value=""),
            DeclareLaunchArgument(
                "slam_params_file",
                default_value=str(gazebo_share / "config" / "slam.yaml"),
                description="SLAM Toolbox parameter file.",
            ),
            DeclareLaunchArgument(
                "rviz",
                default_value="true",
                description="Open the project SLAM view in RViz.",
            ),
            DeclareLaunchArgument(
                "rviz_config",
                default_value=str(gazebo_share / "rviz" / "slam.rviz"),
                description="RViz configuration for mapping.",
            ),
            DeclareLaunchArgument(
                "localization_state",
                default_value=str(
                    Path.home()
                    / ".ros"
                    / "malbut"
                    / "localization_state.yaml"
                ),
                description=(
                    "State file used to hand SLAM localization to Nav2."
                ),
            ),
            simulation,
            slam_toolbox,
            localization_recorder,
            rviz,
        ]
    )
