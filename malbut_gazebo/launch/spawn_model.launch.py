from pathlib import Path

import xacro
from ament_index_python.packages import (
    get_package_prefix,
    get_package_share_directory,
)
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from malbut_description.variant_config import (
    load_variant_arguments,
    resolve_variant_config,
    xacro_value,
)


DEFAULT_VARIANT = 'ultimate_orin_nx_super_mecanum.yaml'


def _launch_setup(context):
    description_share = Path(
        get_package_share_directory('malbut_description')
    )
    gazebo_share = Path(get_package_share_directory('malbut_gazebo'))
    variant_file = resolve_variant_config(
        LaunchConfiguration('variant_config').perform(context),
        description_share,
    )
    arguments = load_variant_arguments(variant_file)
    mappings = {
        key: xacro_value(value) for key, value in arguments.items()
    }
    robot_description = xacro.process_file(
        str(gazebo_share / 'urdf' / 'robot.gazebo.xacro'),
        mappings=mappings,
    ).toxml()
    helper = (
        Path(get_package_prefix('malbut_gazebo'))
        / 'lib'
        / 'malbut_gazebo'
        / 'spawn_when_ready'
    )

    state_publisher = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        parameters=[
            {
                'robot_description': robot_description,
                'use_sim_time': True,
            }
        ],
        output='screen',
    )
    spawn = ExecuteProcess(
        cmd=[
            str(helper),
            '--world',
            LaunchConfiguration('world_name'),
            '--entity-name',
            LaunchConfiguration('entity_name'),
            '--topic',
            '/robot_description',
            '--x',
            LaunchConfiguration('x'),
            '--y',
            LaunchConfiguration('y'),
            '--z',
            LaunchConfiguration('z'),
            '--yaw',
            LaunchConfiguration('yaw'),
            '--timeout',
            LaunchConfiguration('spawn_timeout'),
        ],
        output='screen',
    )
    return [state_publisher, spawn]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                'variant_config', default_value=DEFAULT_VARIANT
            ),
            DeclareLaunchArgument('world_name', default_value='empty'),
            DeclareLaunchArgument('entity_name', default_value='malbut'),
            DeclareLaunchArgument('spawn_timeout', default_value='60'),
            DeclareLaunchArgument('x', default_value='0.0'),
            DeclareLaunchArgument('y', default_value='0.0'),
            DeclareLaunchArgument('z', default_value='0.002'),
            DeclareLaunchArgument('yaw', default_value='0.0'),
            OpaqueFunction(function=_launch_setup),
        ]
    )
