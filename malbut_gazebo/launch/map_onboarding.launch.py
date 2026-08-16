"""Launch first-run mapping on an already running robot sensor graph."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Start SLAM, active-SLAM Nav2, web setup, and optional RViz."""
    share = Path(get_package_share_directory("malbut_gazebo"))
    slam_share = Path(get_package_share_directory("slam_toolbox"))
    mapper = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(slam_share / "launch" / "online_async_launch.py")
        ),
        launch_arguments={
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "slam_params_file": LaunchConfiguration("slam_params_file"),
        }.items(),
    )
    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(share / "launch" / "navigation.launch.py")
        ),
        launch_arguments={
            "localization_source": "slam",
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "rviz": "false",
            "robot_web": "false",
        }.items(),
    )
    recorder = Node(
        package="malbut_gazebo",
        executable="record_localization_state",
        name="localization_state_recorder",
        output="screen",
        parameters=[{
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "state_path": LaunchConfiguration("localization_state"),
        }],
    )
    onboarding = Node(
        package="malbut_gazebo",
        executable="map_onboarding_server",
        name="map_onboarding_server",
        output="screen",
        parameters=[{"use_sim_time": LaunchConfiguration("use_sim_time")}],
        arguments=[
            "--host", LaunchConfiguration("web_host"),
            "--port", LaunchConfiguration("web_port"),
            "--store", LaunchConfiguration("map_store"),
            "--auto-start", LaunchConfiguration("auto_start"),
            "--replace-existing", LaunchConfiguration("replace_existing"),
            "--save-posegraph", LaunchConfiguration("save_posegraph"),
            "--runtime-request-file",
            LaunchConfiguration("runtime_request_file"),
        ],
    )
    rviz = Node(
        package="rviz2",
        executable="rviz2",
        arguments=["-d", LaunchConfiguration("rviz_config")],
        parameters=[{"use_sim_time": LaunchConfiguration("use_sim_time")}],
        condition=IfCondition(LaunchConfiguration("rviz")),
        output="screen",
    )
    return LaunchDescription([
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument(
            "slam_params_file",
            default_value=str(share / "config" / "slam.yaml"),
        ),
        DeclareLaunchArgument("rviz", default_value="false"),
        DeclareLaunchArgument(
            "rviz_config", default_value=str(share / "rviz" / "slam.rviz")
        ),
        DeclareLaunchArgument(
            "localization_state",
            default_value=str(
                Path.home() / ".ros" / "malbut" / "localization_state.yaml"
            ),
        ),
        DeclareLaunchArgument("web_host", default_value="127.0.0.1"),
        DeclareLaunchArgument("web_port", default_value="8765"),
        DeclareLaunchArgument(
            "map_store",
            default_value=str(
                Path.home() / ".local" / "share" / "malbut" / "maps"
            ),
        ),
        DeclareLaunchArgument("auto_start", default_value="false"),
        DeclareLaunchArgument("replace_existing", default_value="false"),
        DeclareLaunchArgument("save_posegraph", default_value="false"),
        DeclareLaunchArgument("runtime_request_file", default_value=""),
        mapper,
        navigation,
        recorder,
        onboarding,
        rviz,
    ])
