"""Launch the Malbut system manager."""

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    """Create the standalone system-manager launch description."""
    return LaunchDescription(
        [
            Node(
                package='malbut_system_manager',
                executable='system_manager',
                name='system_manager',
                output='screen',
            )
        ]
    )
