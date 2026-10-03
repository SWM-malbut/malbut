"""Launch the Jetson/Aurora home-camera media profile with a discovered RGB topic.

Fall pose analysis is started separately by the robot Bringup (malbut_fall_pose).
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, RegisterEventHandler
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    config = PathJoinSubstitution(
        [FindPackageShare("homecam_media_agent"), "config", "aurora.yaml"]
    )
    image_topic = LaunchConfiguration("image_topic")
    camera_info_topic = LaunchConfiguration("camera_info_topic")
    backend_url = LaunchConfiguration("backend_url")
    device_id = LaunchConfiguration("device_id")
    monitoring_enabled = LaunchConfiguration("monitoring_enabled")
    audio_source = LaunchConfiguration("audio_source")
    audio_sink = LaunchConfiguration("audio_sink")
    microphone_enabled = LaunchConfiguration("microphone_enabled")

    common_overrides = {
        "use_sim_time": False,
        "image_topic": ParameterValue(image_topic, value_type=str),
        "odom_topic": ParameterValue(
            LaunchConfiguration("odom_topic"), value_type=str
        ),
        "backend_url": ParameterValue(backend_url, value_type=str),
        "device_id": ParameterValue(device_id, value_type=str),
        "monitoring_enabled": ParameterValue(
            monitoring_enabled, value_type=bool
        ),
    }
    media_node = Node(
        package="homecam_media_agent",
        executable="homecam_media_agent_node",
        name="homecam_media_agent",
        output="screen",
        parameters=[
            config,
            common_overrides,
            {
                "camera_info_topic": ParameterValue(
                    camera_info_topic, value_type=str
                ),
                "audio_source": ParameterValue(audio_source, value_type=str),
                "audio_sink": ParameterValue(audio_sink, value_type=str),
                **{
                    "fall_" + peer + "_runtime_id": ParameterValue(
                        LaunchConfiguration("fall_" + peer + "_runtime_id"), value_type=str
                    ) for peer in ("bridge", "manager", "vlm")
                },
                "microphone_enabled": ParameterValue(
                    microphone_enabled, value_type=bool
                ),
            },
        ],
    )
    return LaunchDescription(
        [
            # No default on purpose: the Aurora driver topic must be discovered.
            DeclareLaunchArgument("image_topic"),
            DeclareLaunchArgument("camera_info_topic", default_value=""),
            DeclareLaunchArgument("odom_topic", default_value="/odom"),
            DeclareLaunchArgument("backend_url", default_value=""),
            DeclareLaunchArgument("device_id", default_value="jetson-homecam"),
            DeclareLaunchArgument("monitoring_enabled", default_value="false"),
            DeclareLaunchArgument("audio_source", default_value="default"),
            DeclareLaunchArgument("audio_sink", default_value="default"),
            DeclareLaunchArgument("microphone_enabled", default_value="true"),
            *[DeclareLaunchArgument("fall_" + peer + "_runtime_id", default_value="")
              for peer in ("bridge", "manager", "vlm")],
            RegisterEventHandler(
                OnProcessExit(
                    target_action=media_node,
                    on_exit=[
                        EmitEvent(
                            event=Shutdown(reason="homecam media agent exited")
                        )
                    ],
                )
            ),
            media_node,
        ]
    )
