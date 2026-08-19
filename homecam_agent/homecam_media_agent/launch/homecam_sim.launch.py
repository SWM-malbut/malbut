"""Launch the Gazebo home-camera media and detector agents."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, RegisterEventHandler
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare
from launch.substitutions import PathJoinSubstitution


def generate_launch_description() -> LaunchDescription:
    config = PathJoinSubstitution(
        [FindPackageShare("homecam_media_agent"), "config", "sim.yaml"]
    )
    backend_url = LaunchConfiguration("backend_url")
    device_id = LaunchConfiguration("device_id")
    model_path = LaunchConfiguration("model_path")
    pose_model_path = LaunchConfiguration("pose_model_path")
    monitoring_enabled = LaunchConfiguration("monitoring_enabled")
    event_clips_enabled = LaunchConfiguration("event_clips_enabled")
    image_topic = LaunchConfiguration("image_topic")
    camera_info_topic = LaunchConfiguration("camera_info_topic")
    odom_topic = LaunchConfiguration("odom_topic")
    navigation_status_topic = LaunchConfiguration("navigation_status_topic")
    audio_source = LaunchConfiguration("audio_source")
    audio_sink = LaunchConfiguration("audio_sink")
    microphone_enabled = LaunchConfiguration("microphone_enabled")

    common_overrides = {
        "image_topic": ParameterValue(image_topic, value_type=str),
        "odom_topic": ParameterValue(odom_topic, value_type=str),
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
                "microphone_enabled": ParameterValue(
                    microphone_enabled, value_type=bool
                ),
            },
        ],
    )
    detector_node = Node(
        package="homecam_detector",
        executable="homecam_detector_node",
        name="homecam_detector",
        output="screen",
        parameters=[
            config,
            common_overrides,
            {
                "model_path": ParameterValue(model_path, value_type=str),
                "pose_model_path": ParameterValue(
                    pose_model_path, value_type=str
                ),
                "navigation_status_topic": ParameterValue(
                    navigation_status_topic, value_type=str
                ),
                "event_clips_enabled": ParameterValue(
                    event_clips_enabled, value_type=bool
                ),
            },
        ],
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument("backend_url", default_value=""),
            DeclareLaunchArgument("device_id", default_value="gazebo-homecam"),
            DeclareLaunchArgument("model_path", default_value=""),
            DeclareLaunchArgument("pose_model_path", default_value=""),
            DeclareLaunchArgument("monitoring_enabled", default_value="false"),
            DeclareLaunchArgument("event_clips_enabled", default_value="true"),
            DeclareLaunchArgument(
                "image_topic", default_value="/camera/color/image_raw"
            ),
            DeclareLaunchArgument(
                "camera_info_topic",
                default_value="/camera/color/camera_info",
            ),
            DeclareLaunchArgument("odom_topic", default_value="/odom"),
            DeclareLaunchArgument(
                "navigation_status_topic",
                default_value="/navigate_to_pose/_action/status",
            ),
            DeclareLaunchArgument("audio_source", default_value="default"),
            DeclareLaunchArgument("audio_sink", default_value="default"),
            DeclareLaunchArgument("microphone_enabled", default_value="true"),
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
            RegisterEventHandler(
                OnProcessExit(
                    target_action=detector_node,
                    on_exit=[
                        EmitEvent(
                            event=Shutdown(reason="homecam detector exited")
                        )
                    ],
                )
            ),
            media_node,
            detector_node,
        ]
    )
