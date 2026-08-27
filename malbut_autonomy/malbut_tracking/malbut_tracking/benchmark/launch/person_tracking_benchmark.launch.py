"""Run a parameterized sensor-only person-following benchmark."""

import math
import os
from pathlib import Path
import shutil

from ament_index_python.packages import (
    get_package_prefix,
    get_package_share_directory,
)
from launch import LaunchDescription
from launch.actions import (
    AppendEnvironmentVariable,
    DeclareLaunchArgument,
    EmitEvent,
    ExecuteProcess,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
    TimerAction,
)
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit, OnShutdown
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_tracking.benchmark.scenario import (
    instrument_world,
    load_scenario,
    optional_file,
)


def _number(context, name, fallback, *, positive=False):
    value = LaunchConfiguration(name).perform(context).strip()
    number = fallback if not value else float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        condition = 'positive' if positive else 'finite'
        raise ValueError(f'{name} must be {condition}')
    return number


def _cleanup_world(_context, path):
    shutil.rmtree(Path(path).parent, ignore_errors=True)
    return []


def _shutdown_on_actor_spawn_failure(event, _context):
    if event.returncode == 0:
        return []
    return [
        EmitEvent(
            event=Shutdown(
                reason=f'benchmark actor spawn failed: {event.returncode}'
            )
        )
    ]


def _launch_setup(context):
    tracking_share = Path(get_package_share_directory('malbut_tracking'))
    gazebo_share = Path(get_package_share_directory('malbut_gazebo'))
    perception_share = Path(get_package_share_directory('malbut_perception'))
    benchmark_share = tracking_share / 'benchmark'
    catalog = optional_file(
        LaunchConfiguration('scenario_file').perform(context),
        benchmark_share / 'config' / 'scenarios.yaml',
        'scenario_file',
    )
    scenario = load_scenario(
        catalog, LaunchConfiguration('scenario').perform(context)
    )
    world_file = optional_file(
        LaunchConfiguration('world_file').perform(context),
        scenario.world_file,
        'world_file',
    )
    map_file = optional_file(
        LaunchConfiguration('map_file').perform(context),
        scenario.map_file,
        'map_file',
    )
    actor_file = optional_file(
        LaunchConfiguration('actor_file').perform(context),
        scenario.actor_file,
        'actor_file',
    )
    actor_name = LaunchConfiguration('actor_name').perform(context).strip()
    robot_name = LaunchConfiguration(
        'robot_entity_name'
    ).perform(context).strip()
    if not actor_name or not robot_name:
        raise ValueError('actor_name and robot_entity_name are required')
    truth_topic = LaunchConfiguration(
        'ground_truth_topic'
    ).perform(context).strip()
    truth_rate = _number(
        context, 'ground_truth_rate_hz', 20.0, positive=True
    )
    prepared_world = instrument_world(
        world_file,
        robot_name=robot_name,
        actor_name=actor_name,
        topic=truth_topic,
        publish_rate_hz=truth_rate,
    )

    robot_pose = {
        name: _number(
            context, f'robot_{name}', getattr(scenario.robot_pose, name)
        )
        for name in ('x', 'y', 'z', 'yaw')
    }
    actor_pose = {
        name: _number(
            context, f'actor_{name}', getattr(scenario.actor_pose, name)
        )
        for name in ('x', 'y', 'z', 'yaw')
    }
    duration = _number(
        context, 'measurement_duration_s', 180.0, positive=True
    )
    script_start_delay = _number(
        context, 'actor_script_start_delay_s', 5.0, positive=True
    )
    trajectory = LaunchConfiguration(
        'trajectory_name'
    ).perform(context).strip() or scenario.trajectory
    use_sim_time = LaunchConfiguration('use_sim_time')
    spawn_helper = (
        Path(get_package_prefix('malbut_gazebo'))
        / 'lib'
        / 'malbut_gazebo'
        / 'spawn_when_ready'
    )

    simulation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(gazebo_share / 'launch' / 'simulation.launch.py')
        ),
        launch_arguments={
            'world': str(prepared_world),
            'entity_name': robot_name,
            'x': str(robot_pose['x']),
            'y': str(robot_pose['y']),
            'z': str(robot_pose['z']),
            'yaw': str(robot_pose['yaw']),
            'gui': LaunchConfiguration('gui'),
            'headless': LaunchConfiguration('headless'),
            'paused': 'false',
            'use_sim_time': use_sim_time,
            'rviz': 'false',
            'spawn_robot': 'true',
            'bridge': 'true',
        }.items(),
    )
    actor_spawn = ExecuteProcess(
        cmd=[
            str(spawn_helper),
            '--world',
            scenario.world_name,
            '--entity-name',
            actor_name,
            '--file',
            str(actor_file),
            '--align-actor-script',
            '--actor-script-start-delay',
            str(script_start_delay),
            '--x',
            str(actor_pose['x']),
            '--y',
            str(actor_pose['y']),
            '--z',
            str(actor_pose['z']),
            '--yaw',
            str(actor_pose['yaw']),
            '--timeout',
            LaunchConfiguration('spawn_timeout'),
        ],
        output='screen',
    )
    perception = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(perception_share / 'launch' / 'person_detection.launch.py')
        ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'projection_frame': 'camera_depth_optical_frame',
            'publish_debug_image': LaunchConfiguration(
                'publish_debug_image'
            ),
            'debug_image_transport': 'raw',
            'inference_backend': LaunchConfiguration('inference_backend'),
            'dnn_target': LaunchConfiguration('dnn_target'),
        }.items(),
    )
    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(gazebo_share / 'launch' / 'navigation.launch.py')
        ),
        launch_arguments={
            'map': str(map_file),
            'use_sim_time': use_sim_time,
            'rviz': 'false',
            'robot_web': 'false',
            'restore_localization': 'false',
            'set_initial_pose': 'true',
            'initial_pose_x': str(robot_pose['x']),
            'initial_pose_y': str(robot_pose['y']),
            'initial_pose_yaw': str(robot_pose['yaw']),
            'localization_source': 'static',
        }.items(),
    )
    tracking = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(tracking_share / 'launch' / 'person_following.launch.py')
        ),
        launch_arguments={'use_sim_time': use_sim_time}.items(),
    )
    truth_bridge = Node(
        package='ros_gz_bridge',
        executable='parameter_bridge',
        name='benchmark_ground_truth_bridge',
        arguments=[f'{truth_topic}@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V'],
        output='screen',
    )
    evaluator = Node(
        package='malbut_tracking',
        executable='person_tracking_benchmark',
        name='person_tracking_benchmark',
        output='screen',
        parameters=[
            str(benchmark_share / 'config' / 'benchmark.yaml'),
            {
                'use_sim_time': use_sim_time,
                'scenario_name': scenario.name,
                'world_name': scenario.world_name,
                'trajectory_name': trajectory,
                'ground_truth_topic': truth_topic,
                'robot_entity_name': robot_name,
                'person_entity_name': actor_name,
                'measurement_duration_s': duration,
                'output_directory': LaunchConfiguration('output_directory'),
            },
        ],
    )
    rviz = Node(
        package='rviz2',
        executable='rviz2',
        arguments=['-d', str(gazebo_share / 'rviz' / 'nav_nav2.rviz')],
        parameters=[{'use_sim_time': use_sim_time}],
        output='screen',
        condition=IfCondition(LaunchConfiguration('rviz')),
    )
    image_view = Node(
        package='rqt_image_view',
        executable='rqt_image_view',
        arguments=['/perception/person/debug_image'],
        output='screen',
        condition=IfCondition(LaunchConfiguration('image_view')),
    )
    return [
        AppendEnvironmentVariable(
            'IGN_GAZEBO_RESOURCE_PATH',
            os.pathsep.join(
                [
                    str(gazebo_share / 'models'),
                    str(gazebo_share / 'models' / 'aws_small_house'),
                ]
            ),
        ),
        AppendEnvironmentVariable(
            'GZ_SIM_RESOURCE_PATH',
            os.pathsep.join(
                [
                    str(gazebo_share / 'models'),
                    str(gazebo_share / 'models' / 'aws_small_house'),
                ]
            ),
        ),
        simulation,
        TimerAction(
            period=LaunchConfiguration('actor_spawn_delay'),
            actions=[actor_spawn],
        ),
        RegisterEventHandler(
            OnProcessExit(
                target_action=actor_spawn,
                on_exit=_shutdown_on_actor_spawn_failure,
            )
        ),
        perception,
        navigation,
        tracking,
        truth_bridge,
        evaluator,
        RegisterEventHandler(
            OnProcessExit(
                target_action=evaluator,
                on_exit=[
                    EmitEvent(
                        event=Shutdown(
                            reason='person tracking benchmark finished'
                        )
                    )
                ],
            )
        ),
        RegisterEventHandler(
            OnShutdown(
                on_shutdown=[
                    OpaqueFunction(
                        function=_cleanup_world,
                        args=[str(prepared_world)],
                    )
                ]
            )
        ),
        rviz,
        image_view,
    ]


def generate_launch_description():
    """Declare reusable scenario and runtime overrides."""
    tracking_share = Path(get_package_share_directory('malbut_tracking'))
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                'scenario', default_value='test_arena_perimeter'
            ),
            DeclareLaunchArgument(
                'scenario_file',
                default_value=str(
                    tracking_share
                    / 'benchmark'
                    / 'config'
                    / 'scenarios.yaml'
                ),
            ),
            DeclareLaunchArgument('world_file', default_value=''),
            DeclareLaunchArgument('map_file', default_value=''),
            DeclareLaunchArgument(
                'actor_file',
                default_value='',
                description=(
                    'Absolute actor SDF override; use variants here to '
                    'benchmark different actor appearances.'
                ),
            ),
            DeclareLaunchArgument(
                'trajectory_name', default_value=''
            ),
            DeclareLaunchArgument(
                'actor_name', default_value='benchmark_person'
            ),
            DeclareLaunchArgument('robot_entity_name', default_value='malbut'),
            DeclareLaunchArgument('robot_x', default_value=''),
            DeclareLaunchArgument('robot_y', default_value=''),
            DeclareLaunchArgument('robot_z', default_value=''),
            DeclareLaunchArgument('robot_yaw', default_value=''),
            DeclareLaunchArgument('actor_x', default_value=''),
            DeclareLaunchArgument('actor_y', default_value=''),
            DeclareLaunchArgument('actor_z', default_value=''),
            DeclareLaunchArgument('actor_yaw', default_value=''),
            DeclareLaunchArgument(
                'actor_script_start_delay_s', default_value='5.0'
            ),
            DeclareLaunchArgument('actor_spawn_delay', default_value='0.0'),
            DeclareLaunchArgument('spawn_timeout', default_value='60'),
            DeclareLaunchArgument(
                'measurement_duration_s', default_value='180.0'
            ),
            DeclareLaunchArgument('output_directory', default_value=''),
            DeclareLaunchArgument(
                'ground_truth_topic', default_value='/benchmark/ground_truth'
            ),
            DeclareLaunchArgument(
                'ground_truth_rate_hz', default_value='20.0'
            ),
            DeclareLaunchArgument('use_sim_time', default_value='true'),
            DeclareLaunchArgument('gui', default_value='true'),
            DeclareLaunchArgument('headless', default_value='false'),
            DeclareLaunchArgument('rviz', default_value='true'),
            DeclareLaunchArgument('image_view', default_value='true'),
            DeclareLaunchArgument(
                'publish_debug_image', default_value='true'
            ),
            DeclareLaunchArgument('inference_backend', default_value='auto'),
            DeclareLaunchArgument('dnn_target', default_value='auto'),
            OpaqueFunction(function=_launch_setup),
        ]
    )
