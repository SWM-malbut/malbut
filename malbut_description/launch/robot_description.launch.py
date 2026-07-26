from pathlib import Path

import xacro
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from malbut_description.variant_config import (
    load_variant_arguments,
    resolve_variant_config,
    xacro_value,
)


DESCRIPTION_PACKAGE = 'malbut_description'
DEFAULT_VARIANT = 'ultimate_orin_nx_super_mecanum.yaml'


def _as_bool(value, name):
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    raise RuntimeError(f'{name} must be true or false, got: {value}')


def _launch_setup(context):
    package_share = Path(
        get_package_share_directory(DESCRIPTION_PACKAGE)
    )
    config_file = resolve_variant_config(
        LaunchConfiguration('variant_config').perform(context),
        package_share,
    )
    arguments = load_variant_arguments(config_file)
    mappings = {
        key: xacro_value(value) for key, value in arguments.items()
    }
    description = xacro.process_file(
        str(package_share / 'urdf' / 'rosorin.xacro'),
        mappings=mappings,
    ).toxml()
    use_sim_time = _as_bool(
        LaunchConfiguration('use_sim_time').perform(context),
        'use_sim_time',
    )

    return [
        Node(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            name='robot_state_publisher',
            parameters=[
                {
                    'robot_description': description,
                    'use_sim_time': use_sim_time,
                }
            ],
            output='screen',
        )
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                'variant_config',
                default_value=DEFAULT_VARIANT,
                description='Variant YAML basename or absolute path.',
            ),
            DeclareLaunchArgument('use_sim_time', default_value='false'),
            OpaqueFunction(function=_launch_setup),
        ]
    )
