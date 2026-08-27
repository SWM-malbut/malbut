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
import tempfile

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
)
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


def _replace_exact(
    source: str,
    old: str,
    new: str,
    expected_count: int,
    label: str,
) -> str:
    """Patch a pinned upstream launch contract or fail closed."""
    count = source.count(old)
    if count != expected_count:
        raise RuntimeError(
            f'Unsupported Nav2 launch ({label}: expected '
            f'{expected_count}, found {count}); cannot enforce the '
            'collision-monitor velocity chain.'
        )
    return source.replace(old, new)


def _temporary_launch(source: str, prefix: str) -> str:
    """Write one private launch source consumed by this launch process."""
    with tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', prefix=prefix,
        suffix='.launch.py', delete=False,
    ) as stream:
        stream.write(source)
        return stream.name


def _safe_nav2_launch_sources(nav2_share: str) -> tuple[str, str]:
    """Route every Nav2 motion command through Collision Monitor."""
    launch_root = Path(nav2_share, 'launch')
    navigation_source = Path(
        launch_root, 'navigation_launch.py'
    ).read_text(encoding='utf-8')
    navigation_source = _replace_exact(
        navigation_source,
        "('cmd_vel_smoothed', 'cmd_vel')",
        "('cmd_vel_smoothed', 'cmd_vel_pre_collision')",
        2,
        'velocity smoother output',
    )
    behavior_node = """            Node(
                package='nav2_behaviors',
                executable='behavior_server',
                name='behavior_server',
                output='screen',
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=['--ros-args', '--log-level', log_level],
                remappings=remappings),"""
    safe_behavior_node = behavior_node.replace(
        'remappings=remappings),',
        "remappings=remappings + [('cmd_vel', "
        "'cmd_vel_pre_collision')]),",
    )
    navigation_source = _replace_exact(
        navigation_source,
        behavior_node,
        safe_behavior_node,
        1,
        'behavior server output',
    )
    behavior_component = """            ComposableNode(
                package='nav2_behaviors',
                plugin='behavior_server::BehaviorServer',
                name='behavior_server',
                parameters=[configured_params],
                remappings=remappings),"""
    safe_behavior_component = behavior_component.replace(
        'remappings=remappings),',
        "remappings=remappings + [('cmd_vel', "
        "'cmd_vel_pre_collision')]),",
    )
    navigation_source = _replace_exact(
        navigation_source,
        behavior_component,
        safe_behavior_component,
        1,
        'composed behavior server output',
    )
    safe_navigation = _temporary_launch(
        navigation_source, 'malbut-nav2-navigation-'
    )

    bringup_source = Path(
        launch_root, 'bringup_launch.py'
    ).read_text(encoding='utf-8')
    bringup_source = _replace_exact(
        bringup_source,
        "'navigation_launch.py'",
        repr(safe_navigation),
        1,
        'bringup navigation include',
    )
    safe_bringup = _temporary_launch(
        bringup_source, 'malbut-nav2-bringup-'
    )
    return safe_navigation, safe_bringup


def generate_launch_description():
    """Start localization, navigation, and the project Nav2 RViz view."""
    gazebo_share = get_package_share_directory('malbut_gazebo')
    nav2_share = get_package_share_directory('nav2_bringup')
    perception_share = get_package_share_directory('malbut_perception')
    tracking_share = get_package_share_directory('malbut_tracking')
    lidar_preprocessor_share = get_package_share_directory(
        'malbut_lidar_preprocessor'
    )
    safe_navigation_source, safe_bringup_source = (
        _safe_nav2_launch_sources(nav2_share)
    )

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
    robot_web = LaunchConfiguration('robot_web')
    robot_web_port = LaunchConfiguration('robot_web_port')
    robot_web_navigation_action = LaunchConfiguration(
        'robot_web_navigation_action'
    )
    user_map = LaunchConfiguration('user_map')
    pose_checkpoint_store = LaunchConfiguration('pose_checkpoint_store')
    pose_checkpoint_map_id = LaunchConfiguration('pose_checkpoint_map_id')
    pose_checkpoint_map_revision = LaunchConfiguration(
        'pose_checkpoint_map_revision'
    )
    boot_pose_trusted = LaunchConfiguration('boot_pose_trusted')
    autonomous_modes = LaunchConfiguration('autonomous_modes')
    patrol_route_file = LaunchConfiguration('patrol_route_file')
    person_following = LaunchConfiguration('person_following')
    person_projection_frame = LaunchConfiguration('person_projection_frame')
    inscribed_escape_enabled = LaunchConfiguration(
        'inscribed_escape_enabled'
    )
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
        PythonLaunchDescriptionSource(safe_bringup_source),
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
        PythonLaunchDescriptionSource(safe_navigation_source),
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
    inscribed_escape = Node(
        package='malbut_gazebo',
        executable='inscribed_escape',
        name='inscribed_escape',
        namespace=namespace,
        condition=IfCondition(inscribed_escape_enabled),
        # 로봇이 벽에 붙어 멈추면 그 셀이 내접 장애물이 되어 어떤 계획도
        # 시작하지 못하고, collision monitor 가 탈출용 후진까지 막아 Nav2
        # 복구가 제자리에서 반복된다. 그때만 짧게 빠져나오게 한다.
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
        }],
    )
    localization_recorder = Node(
        package='malbut_gazebo',
        executable='record_localization_state',
        name='localization_state_recorder',
        namespace=namespace,
        # 기록기는 그동안 SLAM 계열 런치에만 있었다. 그래서 저장된 지도의
        # map->odom 이 한 번도 기록되지 않았고, 재매핑을 중지하고 돌아올 때
        # 복원할 근거가 없어 AMCL 이 초기 위치를 받지 못했다.
        condition=IfCondition(use_static_map),
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'state_path': localization_state,
            'pinned': True,
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
    pose_checkpoint = Node(
        package='malbut_gazebo',
        executable='pose_checkpoint',
        name='pose_checkpoint',
        namespace=namespace,
        condition=IfCondition(PythonExpression([
            "'", localization_source, "' != 'slam' and '",
            pose_checkpoint_store, "' != ''",
        ])),
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'map_store': pose_checkpoint_store,
            'map_id': pose_checkpoint_map_id,
            'map_revision': pose_checkpoint_map_revision,
            'initially_trusted': boot_pose_trusted,
        }],
    )
    robot_web_server = Node(
        package='malbut_gazebo',
        executable='robot_web_server',
        name='robot_web_server',
        namespace=namespace,
        condition=IfCondition(PythonExpression([
            "'", robot_web, "' == 'true' and '", user_map, "' != ''",
        ])),
        output='screen',
        arguments=[
            '--port', robot_web_port,
            '--map', user_map,
            '--slam-map', map_file,
        ],
        parameters=[{
            'use_sim_time': use_sim_time,
            'navigation_action_name': robot_web_navigation_action,
            'boot_validation_state': PythonExpression([
                "'verifying' if '", boot_pose_trusted,
                "' == 'true' else 'revalidation_required'",
            ]),
            'patrol_route_file': patrol_route_file,
        }],
    )
    patrol_manager = Node(
        package='malbut_patrol',
        executable='patrol_manager',
        name='patrol_manager',
        namespace=namespace,
        condition=IfCondition(PythonExpression([
            "'", autonomous_modes, "' == 'true' and '",
            patrol_route_file, "' != ''",
        ])),
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'autostart': False,
            'route_file': patrol_route_file,
        }],
    )
    roaming_manager = Node(
        package='malbut_roaming',
        executable='roaming_manager',
        name='roaming_manager',
        namespace=namespace,
        condition=IfCondition(autonomous_modes),
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'autostart': False,
        }],
    )
    collision_monitor = Node(
        package='nav2_collision_monitor',
        executable='collision_monitor',
        name='collision_monitor',
        namespace=namespace,
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'base_frame_id': 'base_footprint',
            'odom_frame_id': 'odom',
            'cmd_vel_in_topic': '/cmd_vel_pre_collision',
            'cmd_vel_out_topic': '/cmd_vel',
            'state_topic': '/collision_monitor_state',
            'transform_tolerance': 0.2,
            'source_timeout': 0.5,
            'base_shift_correction': True,
            'stop_pub_timeout': 0.2,
            'polygons': ['FootprintApproach'],
            'FootprintApproach.type': 'polygon',
            'FootprintApproach.action_type': 'approach',
            'FootprintApproach.footprint_topic': (
                '/local_costmap/published_footprint'
            ),
            'FootprintApproach.time_before_collision': 1.0,
            'FootprintApproach.simulation_time_step': 0.05,
            'FootprintApproach.max_points': 3,
            'FootprintApproach.visualize': False,
            'FootprintApproach.enabled': True,
            'observation_sources': ['scan'],
            'scan.type': 'scan',
            'scan.topic': '/scan',
            'scan.enabled': True,
        }],
    )
    collision_lifecycle = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='collision_lifecycle_manager',
        namespace=namespace,
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'autostart': autostart,
            'node_names': ['collision_monitor'],
        }],
    )
    person_detector = Node(
        package='malbut_perception',
        executable='person_localizer',
        name='person_localizer',
        namespace=namespace,
        condition=IfCondition(person_following),
        output='screen',
        parameters=[
            os.path.join(
                perception_share, 'config', 'person_detection.yaml'
            ),
            {
                'use_sim_time': use_sim_time,
                'model_path': str(
                    Path.home()
                    / '.cache'
                    / 'malbut_perception'
                    / 'yolo26n.onnx'
                ),
                'reid_model_path': str(
                    Path.home()
                    / '.cache'
                    / 'malbut_perception'
                    / 'osnet_ain_x1_0_msmt17.onnx'
                ),
                'projection_frame': person_projection_frame,
                'publish_debug_image': False,
            },
        ],
    )
    person_follower = Node(
        package='malbut_tracking',
        executable='person_follower',
        name='person_follower',
        namespace=namespace,
        condition=IfCondition(person_following),
        output='screen',
        parameters=[
            os.path.join(
                tracking_share, 'config', 'person_following.yaml'
            ),
            {'use_sim_time': use_sim_time},
        ],
    )
    person_lidar_preprocessor = Node(
        package='malbut_lidar_preprocessor',
        executable='lidar_foreground_preprocessor',
        name='lidar_foreground_preprocessor',
        namespace=namespace,
        condition=IfCondition(person_following),
        output='screen',
        parameters=[
            os.path.join(
                lidar_preprocessor_share,
                'config',
                'lidar_foreground.yaml',
            ),
            {'use_sim_time': use_sim_time},
        ],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument('namespace', default_value=''),
            DeclareLaunchArgument('use_namespace', default_value='false'),
            DeclareLaunchArgument(
                'map',
                default_value=os.path.join(
                    gazebo_share, 'maps', 'small_house.yaml'
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
            DeclareLaunchArgument('pose_checkpoint_store', default_value=''),
            DeclareLaunchArgument('pose_checkpoint_map_id', default_value=''),
            DeclareLaunchArgument(
                'pose_checkpoint_map_revision', default_value=''
            ),
            DeclareLaunchArgument(
                'boot_pose_trusted',
                default_value='false',
                description=(
                    'Skip the explicit initialpose proposal only when the '
                    'runtime controls the simulator spawn or verified '
                    'same-boot localization handoff.'
                ),
            ),
            DeclareLaunchArgument(
                'rviz',
                default_value='true',
                description='Start RViz with the project Nav2 view.',
            ),
            DeclareLaunchArgument(
                'robot_web',
                default_value='true',
                description=(
                    'Start the same-origin robot map web server when a '
                    'User Map is provided.'
                ),
            ),
            DeclareLaunchArgument(
                'robot_web_port',
                default_value='8765',
                description='TCP port for the robot map web server.',
            ),
            DeclareLaunchArgument(
                'robot_web_navigation_action',
                default_value='/navigate_to_pose',
                description=(
                    'NavigateToPose action used by the robot web server. '
                    'A scenario coordinator may proxy this action.'
                ),
            ),
            DeclareLaunchArgument(
                'autonomous_modes',
                default_value='false',
                description='Start patrol and roaming managers for the web.',
            ),
            DeclareLaunchArgument(
                'patrol_route_file',
                default_value='',
                description='Room-derived patrol route paired with this map.',
            ),
            DeclareLaunchArgument(
                'person_following',
                default_value='false',
                description='Start RGB-D person perception and following.',
            ),
            DeclareLaunchArgument(
                'person_projection_frame',
                default_value='',
                description=(
                    'Optional optical frame override for simulated RGB-D.'
                ),
            ),
            DeclareLaunchArgument(
                'inscribed_escape_enabled',
                default_value='true',
                description=(
                    'Allow the bounded recovery helper to publish velocity '
                    'when the robot is trapped in an inscribed costmap cell.'
                ),
            ),
            DeclareLaunchArgument(
                'user_map',
                default_value='',
                description=(
                    'User Map GeoJSON used by the robot web server. An empty '
                    'value disables the web server.'
                ),
            ),
            bringup,
            navigation_with_active_slam,
            zone_filter_mask_server,
            zone_filter_info_server,
            zone_filter_lifecycle_manager,
            inscribed_escape,
            localization_recorder,
            localization_restorer,
            pose_checkpoint,
            collision_monitor,
            collision_lifecycle,
            patrol_manager,
            roaming_manager,
            person_detector,
            person_lidar_preprocessor,
            person_follower,
            robot_web_server,
            rviz,
        ]
    )
