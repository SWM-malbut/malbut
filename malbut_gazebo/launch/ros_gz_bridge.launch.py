from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _as_bool(value, name):
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    raise RuntimeError(f'{name} must be true or false, got: {value}')


def _launch_setup(context):
    use_sim_time = _as_bool(
        LaunchConfiguration('use_sim_time').perform(context),
        'use_sim_time',
    )
    share = Path(get_package_share_directory('malbut_gazebo'))
    return [
        Node(
            package='ros_gz_bridge',
            executable='parameter_bridge',
            name='malbut_bridge',
            parameters=[
                {
                    'config_file': str(share / 'config' / 'bridge.yaml'),
                    'use_sim_time': use_sim_time,
                }
            ],
            output='screen',
        )
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument('use_sim_time', default_value='true'),
            OpaqueFunction(function=_launch_setup),
        ]
    )
