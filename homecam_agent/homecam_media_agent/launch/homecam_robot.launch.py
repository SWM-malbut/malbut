"""Reuse ROSOrin RGB and existing KVS media without a second perception stack."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    defaults = {
        "image_topic": "/depth_cam/rgb0/image_raw",
        "camera_info_topic": "/depth_cam/rgb0/camera_info",
        "odom_topic": "/odom",
        "backend_url": "",
        "device_id": "jetson-homecam",
        "audio_source": "default",
        "audio_sink": "default",
        "microphone_enabled": "true",
    }
    return LaunchDescription([
        *[DeclareLaunchArgument(name, default_value=value)
          for name, value in defaults.items()],
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(PathJoinSubstitution([
                FindPackageShare("homecam_media_agent"), "launch", "homecam_aurora.launch.py",
            ])),
            launch_arguments={
                **{name: LaunchConfiguration(name) for name in defaults},
                "start_detector": "false",
            }.items(),
        ),
    ])
