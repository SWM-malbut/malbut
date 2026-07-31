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

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    """Start localization, navigation, and the project Nav2 RViz view."""
    gazebo_share = get_package_share_directory('malbut_gazebo')
    nav2_share = get_package_share_directory('nav2_bringup')

    namespace = LaunchConfiguration('namespace')
    use_namespace = LaunchConfiguration('use_namespace')
    map_file = LaunchConfiguration('map')
    params_file = LaunchConfiguration('params_file')
    use_sim_time = LaunchConfiguration('use_sim_time')
    autostart = LaunchConfiguration('autostart')
    use_composition = LaunchConfiguration('use_composition')
    use_respawn = LaunchConfiguration('use_respawn')
    log_level = LaunchConfiguration('log_level')
    use_rviz = LaunchConfiguration('rviz')

    bringup = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_share, 'launch', 'bringup_launch.py')
        ),
        launch_arguments={
            'namespace': namespace,
            'use_namespace': use_namespace,
            'slam': 'False',
            'map': map_file,
            'use_sim_time': use_sim_time,
            'params_file': params_file,
            'autostart': autostart,
            'use_composition': use_composition,
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

    return LaunchDescription(
        [
            DeclareLaunchArgument('namespace', default_value=''),
            DeclareLaunchArgument('use_namespace', default_value='false'),
            DeclareLaunchArgument(
                'map',
                default_value=os.path.join(
                    gazebo_share, 'maps', 'map_01.yaml'
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
            DeclareLaunchArgument('use_sim_time', default_value='true'),
            DeclareLaunchArgument('autostart', default_value='true'),
            DeclareLaunchArgument('use_composition', default_value='False'),
            DeclareLaunchArgument('use_respawn', default_value='False'),
            DeclareLaunchArgument('log_level', default_value='info'),
            DeclareLaunchArgument(
                'rviz',
                default_value='true',
                description='Start RViz with the project Nav2 view.',
            ),
            bringup,
            rviz,
        ]
    )
