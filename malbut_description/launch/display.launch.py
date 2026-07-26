import os
from pathlib import Path
import tempfile

import xacro
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    OpaqueFunction,
    RegisterEventHandler,
)
from launch.conditions import IfCondition, UnlessCondition
from launch.event_handlers import OnShutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from malbut_description.variant_config import (
    load_variant_arguments,
    resolve_variant_config,
    xacro_value,
)


DESCRIPTION_PACKAGE = 'malbut_description'
DEFAULT_VARIANT = 'ultimate_orin_nx_super_mecanum.yaml'


def _remove_rendered_urdf(_context, rendered_urdf):
    Path(rendered_urdf).unlink(missing_ok=True)
    return []


def _launch_setup(context):
    package_share = Path(
        get_package_share_directory(DESCRIPTION_PACKAGE)
    )
    config_file = resolve_variant_config(
        LaunchConfiguration('variant_config').perform(context),
        package_share,
    )
    xacro_arguments = load_variant_arguments(config_file)
    xacro_file = package_share / 'urdf' / 'rosorin.xacro'
    mappings = {
        key: xacro_value(value) for key, value in xacro_arguments.items()
    }
    robot_description = xacro.process_file(
        str(xacro_file), mappings=mappings
    ).toxml()

    descriptor, rendered_urdf = tempfile.mkstemp(
        prefix='malbut_rosorin_display_', suffix='.urdf'
    )
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(robot_description)

    return [
        RegisterEventHandler(
            OnShutdown(
                on_shutdown=[
                    OpaqueFunction(
                        function=_remove_rendered_urdf,
                        args=[rendered_urdf],
                    )
                ]
            )
        ),
        Node(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            parameters=[{'robot_description': robot_description}],
            output='screen',
        ),
        Node(
            package='joint_state_publisher',
            executable='joint_state_publisher',
            arguments=[rendered_urdf],
            condition=UnlessCondition(LaunchConfiguration('gui')),
        ),
        Node(
            package='joint_state_publisher_gui',
            executable='joint_state_publisher_gui',
            arguments=[rendered_urdf],
            condition=IfCondition(LaunchConfiguration('gui')),
        ),
        Node(
            package='rviz2',
            executable='rviz2',
            arguments=['-d', str(package_share / 'rviz' / 'view.rviz')],
            condition=IfCondition(LaunchConfiguration('rviz')),
            output='screen',
        ),
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                'variant_config',
                default_value=DEFAULT_VARIANT,
                description='Variant YAML basename or absolute path.',
            ),
            DeclareLaunchArgument('gui', default_value='false'),
            DeclareLaunchArgument('rviz', default_value='true'),
            OpaqueFunction(function=_launch_setup),
        ]
    )
