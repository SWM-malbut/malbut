"""Run the complete Small House autonomous roaming demonstration."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    """Start Small House, static localization, Nav2, RViz, and roaming."""
    gazebo_share = Path(get_package_share_directory('malbut_gazebo'))
    roaming_share = Path(get_package_share_directory('malbut_roaming'))
    use_sim_time = LaunchConfiguration('use_sim_time')
    spawn_x = LaunchConfiguration('x')
    spawn_y = LaunchConfiguration('y')
    spawn_yaw = LaunchConfiguration('yaw')

    simulation_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(gazebo_share / 'launch' / 'worlds.launch.py')
        ),
        launch_arguments={
            'world_name': 'small_house',
            'x': spawn_x,
            'y': spawn_y,
            'z': '0.002',
            'yaw': spawn_yaw,
            'gui': LaunchConfiguration('gui'),
            'headless': LaunchConfiguration('headless'),
            'rviz': 'false',
            'use_sim_time': use_sim_time,
            'depth_camera_enabled': 'true',
            'lidar_enabled': 'true',
            'imu_enabled': 'true',
            'spawn_robot': 'true',
            'bridge': 'true',
        }.items(),
    )
    # Keep the simulation-only rviz:=false argument from leaking into the
    # navigation include, which owns the Nav2 map view.
    simulation = GroupAction(
        actions=[simulation_launch],
        scoped=True,
    )
    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(gazebo_share / 'launch' / 'navigation.launch.py')
        ),
        launch_arguments={
            'map': str(gazebo_share / 'maps' / 'small_house.yaml'),
            'use_sim_time': use_sim_time,
            'rviz': LaunchConfiguration('rviz'),
            'restore_localization': 'false',
            'set_initial_pose': 'true',
            'initial_pose_x': spawn_x,
            'initial_pose_y': spawn_y,
            'initial_pose_yaw': spawn_yaw,
            'localization_source': 'static',
        }.items(),
    )
    roaming = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(roaming_share / 'launch' / 'roaming.launch.py')
        ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'autostart': LaunchConfiguration('autostart'),
            'random_seed': LaunchConfiguration('random_seed'),
        }.items(),
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('gui', default_value='true'),
        DeclareLaunchArgument('headless', default_value='false'),
        DeclareLaunchArgument('rviz', default_value='true'),
        DeclareLaunchArgument('autostart', default_value='true'),
        DeclareLaunchArgument('random_seed', default_value='87'),
        DeclareLaunchArgument('x', default_value='-3.665503'),
        DeclareLaunchArgument('y', default_value='-0.4874'),
        DeclareLaunchArgument('yaw', default_value='0.0'),
        simulation,
        navigation,
        roaming,
    ])
