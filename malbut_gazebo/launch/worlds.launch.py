from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    share = Path(get_package_share_directory('malbut_gazebo'))
    simulation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(share / 'launch' / 'simulation.launch.py')
        ),
        launch_arguments={
            'world': LaunchConfiguration('world_name'),
            'gui': LaunchConfiguration('gui'),
            'headless': LaunchConfiguration('headless'),
            'rviz': LaunchConfiguration('rviz'),
        }.items(),
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument('world_name', default_value='empty'),
            DeclareLaunchArgument('gui', default_value='true'),
            DeclareLaunchArgument('headless', default_value='false'),
            DeclareLaunchArgument('rviz', default_value='false'),
            simulation,
        ]
    )
