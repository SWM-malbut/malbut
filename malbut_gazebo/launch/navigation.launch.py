# Copyright (c) 2018 Intel Corporation
# Modifications Copyright 2026 Malbut Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import (
    EqualsSubstitution,
    LaunchConfiguration,
    NotEqualsSubstitution,
    PathJoinSubstitution,
    PythonExpression,
)
from launch_ros.actions import Node
from nav2_common.launch import RewrittenYaml


def generate_launch_description():
    """Start localization, navigation, and the project Nav2 RViz view."""
    gazebo_share = get_package_share_directory('malbut_gazebo')
    nav2_share = get_package_share_directory('nav2_bringup')

    namespace = LaunchConfiguration('namespace')
    use_namespace = LaunchConfiguration('use_namespace')
    map_file = LaunchConfiguration('map')
    params_file = LaunchConfiguration('params_file')
    zone_mask = LaunchConfiguration('zone_mask')
    use_sim_time = LaunchConfiguration('use_sim_time')
    autostart = LaunchConfiguration('autostart')
    use_composition = LaunchConfiguration('use_composition')
    use_respawn = LaunchConfiguration('use_respawn')
    log_level = LaunchConfiguration('log_level')
    use_rviz = LaunchConfiguration('rviz')
    restore_localization = LaunchConfiguration('restore_localization')
    set_initial_pose = LaunchConfiguration(
        'set_initial_pose', default='false'
    )
    initial_pose_x = LaunchConfiguration('initial_pose_x', default='0.0')
    initial_pose_y = LaunchConfiguration('initial_pose_y', default='0.0')
    initial_pose_yaw = LaunchConfiguration(
        'initial_pose_yaw', default='0.0'
    )
    localization_state = LaunchConfiguration('localization_state')
    localization_source = LaunchConfiguration('localization_source')
    use_active_slam = EqualsSubstitution(localization_source, 'slam')
    use_static_map = NotEqualsSubstitution(localization_source, 'slam')
    zone_filter_enabled = NotEqualsSubstitution(zone_mask, '')
    zone_filter_info_topic = PathJoinSubstitution([
        '/', namespace, 'keepout_costmap_filter_info',
    ])
    zone_filter_mask_topic = PathJoinSubstitution([
        '/', namespace, 'keepout_filter_mask',
    ])
    configured_params = RewrittenYaml(
        source_file=params_file,
        param_rewrites={
            'amcl.ros__parameters.set_initial_pose': set_initial_pose,
            'amcl.ros__parameters.initial_pose.x': initial_pose_x,
            'amcl.ros__parameters.initial_pose.y': initial_pose_y,
            'amcl.ros__parameters.initial_pose.yaw': initial_pose_yaw,
            (
                'local_costmap.local_costmap.ros__parameters.'
                'keepout_filter.enabled'
            ): zone_filter_enabled,
            (
                'local_costmap.local_costmap.ros__parameters.'
                'keepout_filter.filter_info_topic'
            ): zone_filter_info_topic,
            (
                'global_costmap.global_costmap.ros__parameters.'
                'keepout_filter.enabled'
            ): zone_filter_enabled,
            (
                'global_costmap.global_costmap.ros__parameters.'
                'keepout_filter.filter_info_topic'
            ): zone_filter_info_topic,
        },
        convert_types=True,
    )

    bringup = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_share, 'launch', 'bringup_launch.py')
        ),
        condition=IfCondition(use_static_map),
        launch_arguments={
            'namespace': namespace,
            'use_namespace': use_namespace,
            'slam': 'False',
            'map': map_file,
            'use_sim_time': use_sim_time,
            'params_file': configured_params,
            'autostart': autostart,
            'use_composition': use_composition,
            'use_respawn': use_respawn,
            'log_level': log_level,
        }.items(),
    )
    navigation_with_active_slam = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_share, 'launch', 'navigation_launch.py')
        ),
        condition=IfCondition(use_active_slam),
        launch_arguments={
            'namespace': namespace,
            'use_sim_time': use_sim_time,
            'params_file': configured_params,
            'autostart': autostart,
            # The SLAM launch does not own a Nav2 component container.
            'use_composition': 'False',
            'use_respawn': use_respawn,
            'log_level': log_level,
        }.items(),
    )

    rviz = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_share, 'launch', 'rviz_launch.py')
        ),
        condition=IfCondition(use_rviz),
        launch_arguments={
            'namespace': namespace,
            'use_namespace': use_namespace,
            'rviz_config': os.path.join(
                gazebo_share, 'rviz', 'nav_nav2.rviz'
            ),
        }.items(),
    )

    zone_filter_mask_server = Node(
        package='nav2_map_server',
        executable='map_server',
        name='zone_filter_mask_server',
        namespace=namespace,
        condition=IfCondition(zone_filter_enabled),
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'yaml_filename': zone_mask,
            'topic_name': zone_filter_mask_topic,
            'frame_id': 'map',
        }],
    )
    zone_filter_info_server = Node(
        package='nav2_map_server',
        executable='costmap_filter_info_server',
        name='zone_filter_info_server',
        namespace=namespace,
        condition=IfCondition(zone_filter_enabled),
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'type': 0,
            'filter_info_topic': zone_filter_info_topic,
            'mask_topic': zone_filter_mask_topic,
            'base': 0.0,
            'multiplier': 1.0,
        }],
    )
    zone_filter_lifecycle_manager = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='zone_filter_lifecycle_manager',
        namespace=namespace,
        condition=IfCondition(zone_filter_enabled),
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'autostart': autostart,
            'node_names': [
                'zone_filter_mask_server',
                'zone_filter_info_server',
            ],
        }],
    )
    localization_restorer = Node(
        package='malbut_gazebo',
        executable='restore_localization_state',
        name='localization_state_restorer',
        namespace=namespace,
        condition=IfCondition(PythonExpression([
            "'", localization_source, "' != 'slam' and '",
            restore_localization, "' == 'true'",
        ])),
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'state_path': localization_state,
        }],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument('namespace', default_value=''),
            DeclareLaunchArgument('use_namespace', default_value='false'),
            DeclareLaunchArgument(
                'map',
                default_value=os.path.join(
                    gazebo_share, 'maps', 'robocup_home.yaml'
                ),
                description='Full path to the occupancy grid map YAML.',
            ),
            DeclareLaunchArgument(
                'params_file',
                default_value=os.path.join(
                    gazebo_share, 'config', 'nav2_params.yaml'
                ),
                description='Full path to the Nav2 parameter file.',
            ),
            DeclareLaunchArgument(
                'zone_mask',
                default_value='',
                description=(
                    'Optional Nav2 filter mask YAML generated from Zones.'
                ),
            ),
            DeclareLaunchArgument('use_sim_time', default_value='true'),
            DeclareLaunchArgument('autostart', default_value='true'),
            DeclareLaunchArgument('use_composition', default_value='False'),
            DeclareLaunchArgument('use_respawn', default_value='False'),
            DeclareLaunchArgument('log_level', default_value='info'),
            DeclareLaunchArgument(
                'restore_localization',
                default_value='true',
                description=(
                    'Restore the pose recorded by the active SLAM session.'
                ),
            ),
            DeclareLaunchArgument(
                'set_initial_pose',
                default_value='false',
                description=(
                    'Initialize AMCL from the explicit pose arguments. '
                    'Intended for repeatable simulation starts.'
                ),
            ),
            DeclareLaunchArgument('initial_pose_x', default_value='0.0'),
            DeclareLaunchArgument('initial_pose_y', default_value='0.0'),
            DeclareLaunchArgument('initial_pose_yaw', default_value='0.0'),
            DeclareLaunchArgument(
                'localization_source',
                default_value='static',
                description=(
                    "Use 'slam' while the mapping SLAM node remains active, "
                    "or 'static' for map_server and AMCL."
                ),
            ),
            DeclareLaunchArgument(
                'localization_state',
                default_value=str(
                    Path.home()
                    / '.ros'
                    / 'malbut'
                    / 'localization_state.yaml'
                ),
                description='SLAM-to-Nav2 localization handoff state.',
            ),
            DeclareLaunchArgument(
                'rviz',
                default_value='true',
                description='Start RViz with the project Nav2 view.',
            ),
            bringup,
            navigation_with_active_slam,
            zone_filter_mask_server,
            zone_filter_info_server,
            zone_filter_lifecycle_manager,
            localization_restorer,
            rviz,
        ]
    )
