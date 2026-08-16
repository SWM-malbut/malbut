"""Launch simulation, active SLAM navigation, and the map setup UI."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


SIMULATION_ARGUMENTS = (
    "world_name", "variant_config", "entity_name", "spawn_timeout",
    "x", "y", "z", "yaw", "gui", "headless", "paused",
    "use_sim_time", "depth_camera_enabled", "imu_enabled", "verbosity",
    "iterations",
)


def generate_launch_description():
    """Compose the complete no-terminal first-run mapping workflow."""
    share = Path(get_package_share_directory("malbut_gazebo"))
    simulation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(share / "launch" / "worlds.launch.py")
        ),
        launch_arguments={
            **{
                name: LaunchConfiguration(name)
                for name in SIMULATION_ARGUMENTS
            },
            "rviz": "false",
            "lidar_enabled": "true",
            "spawn_robot": "true",
            "bridge": "true",
        }.items(),
    )
    onboarding = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(share / "launch" / "map_onboarding.launch.py")
        ),
        launch_arguments={
            "use_sim_time": LaunchConfiguration("use_sim_time"),
            "rviz": LaunchConfiguration("rviz"),
            "web_host": LaunchConfiguration("web_host"),
            "web_port": LaunchConfiguration("web_port"),
            "map_store": LaunchConfiguration("map_store"),
            "auto_start": LaunchConfiguration("auto_start"),
            "replace_existing": LaunchConfiguration("replace_existing"),
            "save_posegraph": LaunchConfiguration("save_posegraph"),
            "runtime_request_file": LaunchConfiguration(
                "runtime_request_file"
            ),
        }.items(),
    )
    declarations = [
        DeclareLaunchArgument("world_name", default_value="small_house"),
        DeclareLaunchArgument(
            "variant_config", default_value="rosorin_ultimate_mecanum.yaml"
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
        DeclareLaunchArgument("depth_camera_enabled", default_value="true"),
        DeclareLaunchArgument("imu_enabled", default_value="true"),
        DeclareLaunchArgument("verbosity", default_value="2"),
        DeclareLaunchArgument("iterations", default_value=""),
        DeclareLaunchArgument("rviz", default_value="true"),
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
        DeclareLaunchArgument(
            "save_posegraph",
            default_value="false",
            description=(
                "Store the large SLAM pose graph for later mapping "
                "continuation."
            ),
        ),
        DeclareLaunchArgument("runtime_request_file", default_value=""),
    ]
    return LaunchDescription([*declarations, simulation, onboarding])
