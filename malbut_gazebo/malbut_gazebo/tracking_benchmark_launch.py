"""Shared launch composition for time-based tracking benchmarks."""

from dataclasses import dataclass
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    IncludeLaunchDescription,
    RegisterEventHandler,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


@dataclass(frozen=True)
class TrackingBenchmarkProfile:
    """World-specific inputs for one complete humanoid circuit."""

    world_name: str
    map_filename: str
    actor_filename: str
    lap_duration_s: float
    robot_x: float
    robot_y: float
    robot_yaw: float
    actor_x: float
    actor_y: float
    actor_yaw: float


def create_tracking_benchmark_launch(
    profile: TrackingBenchmarkProfile,
) -> LaunchDescription:
    """Compose simulation, tracker, and a duration-reporting benchmark."""
    gazebo_share = Path(get_package_share_directory('malbut_gazebo'))
    report_file = (
        Path.home()
        / '.ros'
        / 'malbut'
        / 'tracking_benchmarks'
        / f'{profile.world_name}.json'
    )
    demo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(gazebo_share / 'launch' / 'target_tracking_demo.launch.py')
        ),
        launch_arguments={
            'world_name': profile.world_name,
            'map': str(gazebo_share / 'maps' / profile.map_filename),
            'actor_file': str(
                gazebo_share
                / 'models'
                / 'humanoid_actor'
                / profile.actor_filename
            ),
            'x': str(profile.robot_x),
            'y': str(profile.robot_y),
            'yaw': str(profile.robot_yaw),
            'actor_x': str(profile.actor_x),
            'actor_y': str(profile.actor_y),
            'actor_yaw': str(profile.actor_yaw),
            'actor_spawn_delay': LaunchConfiguration('actor_spawn_delay'),
            'gui': LaunchConfiguration('gui'),
            'headless': LaunchConfiguration('headless'),
            'rviz': LaunchConfiguration('rviz'),
            'image_view': LaunchConfiguration('image_view'),
            'dnn_target': LaunchConfiguration('dnn_target'),
            'use_sim_time': 'true',
        }.items(),
    )
    benchmark = Node(
        package='malbut_tracking',
        executable='tracking_benchmark',
        name=f'tracking_benchmark_{profile.world_name}',
        output='screen',
        parameters=[{
            'use_sim_time': True,
            'world_name': profile.world_name,
            'lap_duration_s': profile.lap_duration_s,
            'acquisition_timeout_s': LaunchConfiguration(
                'acquisition_timeout_s'
            ),
            'result_file': str(report_file),
        }],
    )
    shutdown = RegisterEventHandler(
        OnProcessExit(
            target_action=benchmark,
            on_exit=[
                EmitEvent(
                    event=Shutdown(
                        reason=(
                            f'{profile.world_name} tracking lap complete'
                        )
                    )
                )
            ],
        )
    )
    return LaunchDescription([
        DeclareLaunchArgument('gui', default_value='true'),
        DeclareLaunchArgument('headless', default_value='false'),
        DeclareLaunchArgument('rviz', default_value='true'),
        DeclareLaunchArgument('image_view', default_value='true'),
        DeclareLaunchArgument('dnn_target', default_value='auto'),
        DeclareLaunchArgument('actor_spawn_delay', default_value='15.0'),
        DeclareLaunchArgument('acquisition_timeout_s', default_value='120.0'),
        demo,
        benchmark,
        shutdown,
    ])
