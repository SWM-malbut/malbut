"""Compatibility entry point for the detailed home world."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    gazebo_share = get_package_share_directory('malbut_gazebo')
    return LaunchDescription([
        DeclareLaunchArgument(
            'world_name',
            default_value='small_house',
            description='Home world catalog key',
        ),
        DeclareLaunchArgument('gui', default_value='true'),
        DeclareLaunchArgument('headless', default_value='false'),
        DeclareLaunchArgument('rviz', default_value='false'),
        DeclareLaunchArgument('spawn_robot', default_value='true'),
        DeclareLaunchArgument('bridge', default_value='true'),
        DeclareLaunchArgument('iterations', default_value=''),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(
                    gazebo_share,
                    'launch',
                    'worlds.launch.py',
                )
            ),
            launch_arguments={
                'world_name': LaunchConfiguration('world_name'),
                'gui': LaunchConfiguration('gui'),
                'headless': LaunchConfiguration('headless'),
                'rviz': LaunchConfiguration('rviz'),
                'spawn_robot': LaunchConfiguration('spawn_robot'),
                'bridge': LaunchConfiguration('bridge'),
                'iterations': LaunchConfiguration('iterations'),
            }.items(),
        ),
    ])
