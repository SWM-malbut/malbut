"""Launch the explicitly enabled Gazebo monitor-room execution bridge."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Keep the bridge absent unless a supervisor explicitly enables it."""
    enabled = LaunchConfiguration('enabled')
    config_file = LaunchConfiguration('config_file')
    runtime = Node(
        package='malbut_gazebo',
        executable='gazebo_monitor_room_runtime',
        name='gazebo_monitor_room_runtime',
        output='screen',
        condition=IfCondition(enabled),
        arguments=['--config', config_file],
        parameters=[{'use_sim_time': True}],
        respawn=False,
    )
    return LaunchDescription([
        DeclareLaunchArgument(
            'enabled',
            default_value='false',
            description=(
                'Explicitly start the Gazebo-only monitor-room bridge.'
            ),
        ),
        DeclareLaunchArgument(
            'config_file',
            default_value='',
            description=(
                'Absolute private mode-0600 runtime configuration. '
                'Required when enabled=true.'
            ),
        ),
        runtime,
    ])
