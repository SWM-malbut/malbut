"""Launch the reusable autonomous roaming manager."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Create the roaming node with an overridable parameter file."""
    share = Path(get_package_share_directory('malbut_roaming'))
    return LaunchDescription([
        DeclareLaunchArgument(
            'params_file',
            default_value=str(share / 'config' / 'roaming.yaml'),
        ),
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('autostart', default_value='false'),
        DeclareLaunchArgument('random_seed', default_value='-1'),
        Node(
            package='malbut_roaming',
            executable='roaming_manager',
            name='roaming_manager',
            output='screen',
            parameters=[
                LaunchConfiguration('params_file'),
                {
                    'use_sim_time': LaunchConfiguration('use_sim_time'),
                    'autostart': LaunchConfiguration('autostart'),
                    'random_seed': LaunchConfiguration('random_seed'),
                },
            ],
        ),
    ])
