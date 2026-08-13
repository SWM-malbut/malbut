"""Launch a Malbut world with an RGB-D test humanoid."""

from pathlib import Path

from ament_index_python.packages import (
    get_package_prefix,
    get_package_share_directory,
)
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    ExecuteProcess,
    IncludeLaunchDescription,
    RegisterEventHandler,
    TimerAction,
)
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
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
    "rviz",
    "lidar_enabled",
    "depth_camera_enabled",
    "imu_enabled",
    "verbosity",
    "spawn_robot",
    "bridge",
    "iterations",
)


def _shutdown_on_actor_spawn_failure(event, _context):
    if event.returncode == 0:
        return []
    reason = f"humanoid spawn helper exited with code {event.returncode}"
    return [EmitEvent(event=Shutdown(reason=reason))]


def generate_launch_description():
    """Start a selected world, robot, and local humanoid actor model."""
    gazebo_share = Path(get_package_share_directory(GAZEBO_PACKAGE))
    perception_share = Path(
        get_package_share_directory("malbut_perception")
    )
    default_model = (
        Path.home() / ".cache" / "malbut_perception" / "yolov5n.onnx"
    )
    default_reid_model = (
        Path.home()
        / ".cache"
        / "malbut_perception"
        / "osnet_x0_25_msmt17.onnx"
    )
    spawn_helper = (
        Path(get_package_prefix(GAZEBO_PACKAGE))
        / "lib"
        / GAZEBO_PACKAGE
        / "spawn_when_ready"
    )
    actor_file = gazebo_share / "models" / "humanoid_actor" / "model.sdf"

    simulation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(gazebo_share / "launch" / "worlds.launch.py")
        ),
        launch_arguments={
            name: LaunchConfiguration(name) for name in SIMULATION_ARGUMENTS
        }.items(),
    )
    actor_spawn = ExecuteProcess(
        cmd=[
            str(spawn_helper),
            "--world",
            LaunchConfiguration("world_name"),
            "--entity-name",
            LaunchConfiguration("actor_name"),
            "--file",
            str(actor_file),
            "--x",
            LaunchConfiguration("actor_x"),
            "--y",
            LaunchConfiguration("actor_y"),
            "--z",
            LaunchConfiguration("actor_z"),
            "--yaw",
            LaunchConfiguration("actor_yaw"),
            "--timeout",
            LaunchConfiguration("spawn_timeout"),
        ],
        output="screen",
    )
    perception = Node(
        package="malbut_perception",
        executable="person_localizer",
        name="person_localizer",
        output="screen",
        condition=IfCondition(LaunchConfiguration("perception")),
        parameters=[
            LaunchConfiguration("perception_config"),
            {
                "use_sim_time": LaunchConfiguration("use_sim_time"),
                "detector_backend": LaunchConfiguration("detector_backend"),
                "model_path": LaunchConfiguration("model_path"),
                "dnn_target": LaunchConfiguration("dnn_target"),
                "opencv_num_threads": LaunchConfiguration(
                    "opencv_num_threads"
                ),
                "reid_backend": LaunchConfiguration("reid_backend"),
                "reid_model_path": LaunchConfiguration("reid_model_path"),
                "output_frame": LaunchConfiguration("output_frame"),
                "projection_frame": LaunchConfiguration(
                    "projection_frame"
                ),
                "publish_debug_image": LaunchConfiguration(
                    "publish_debug_image"
                ),
                "debug_image_transport": LaunchConfiguration(
                    "debug_image_transport"
                ),
                "debug_jpeg_quality": LaunchConfiguration(
                    "debug_jpeg_quality"
                ),
            },
        ],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("world_name", default_value="small_house"),
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
            DeclareLaunchArgument("rviz", default_value="false"),
            DeclareLaunchArgument("lidar_enabled", default_value="true"),
            DeclareLaunchArgument(
                "depth_camera_enabled", default_value="true"
            ),
            DeclareLaunchArgument("imu_enabled", default_value="true"),
            DeclareLaunchArgument("verbosity", default_value="2"),
            DeclareLaunchArgument("spawn_robot", default_value="true"),
            DeclareLaunchArgument("bridge", default_value="true"),
            DeclareLaunchArgument("iterations", default_value=""),
            DeclareLaunchArgument(
                "actor_name", default_value="humanoid_target"
            ),
            DeclareLaunchArgument(
                "actor_spawn_delay",
                default_value="0.0",
                description=(
                    "Delay actor creation so the robot camera can be ready."
                ),
            ),
            DeclareLaunchArgument("perception", default_value="false"),
            DeclareLaunchArgument(
                "perception_config",
                default_value=str(
                    perception_share / "config" / "person_detection.yaml"
                ),
            ),
            DeclareLaunchArgument("detector_backend", default_value="auto"),
            DeclareLaunchArgument(
                "model_path", default_value=str(default_model)
            ),
            DeclareLaunchArgument("dnn_target", default_value="auto"),
            DeclareLaunchArgument(
                "opencv_num_threads", default_value="4"
            ),
            DeclareLaunchArgument("reid_backend", default_value="auto"),
            DeclareLaunchArgument(
                "reid_model_path", default_value=str(default_reid_model)
            ),
            DeclareLaunchArgument("output_frame", default_value=""),
            DeclareLaunchArgument(
                "projection_frame",
                default_value="camera_depth_optical_frame",
                description=(
                    "REP-103 frame used by RGB-D pixel projection."
                ),
            ),
            DeclareLaunchArgument(
                "publish_debug_image", default_value="true"
            ),
            DeclareLaunchArgument(
                "debug_image_transport", default_value="compressed"
            ),
            DeclareLaunchArgument(
                "debug_jpeg_quality", default_value="80"
            ),
            # The mapped Small House circuit starts 1.48 m in front of the
            # robot and returns here after visiting the connected rooms.
            DeclareLaunchArgument("actor_x", default_value="-2.19"),
            DeclareLaunchArgument("actor_y", default_value="-1.17"),
            DeclareLaunchArgument("actor_z", default_value="0.0"),
            DeclareLaunchArgument("actor_yaw", default_value="0.0"),
            simulation,
            RegisterEventHandler(
                OnProcessExit(
                    target_action=actor_spawn,
                    on_exit=_shutdown_on_actor_spawn_failure,
                )
            ),
            TimerAction(
                period=LaunchConfiguration("actor_spawn_delay"),
                actions=[actor_spawn],
            ),
            perception,
        ]
    )
