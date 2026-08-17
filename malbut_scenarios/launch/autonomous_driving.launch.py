"""Launch the complete SWM25-94 autonomous-driving demonstration."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, SetRemap


def generate_launch_description():
    """Start simulation, perception, Nav2, web, and scenario coordination."""
    scenario_share = Path(
        get_package_share_directory('malbut_scenarios')
    )
    gazebo_share = Path(get_package_share_directory('malbut_gazebo'))
    roaming_share = Path(get_package_share_directory('malbut_roaming'))
    tracking_share = Path(get_package_share_directory('malbut_tracking'))
    use_sim_time = LaunchConfiguration('use_sim_time')
    spawn_x = LaunchConfiguration('x')
    spawn_y = LaunchConfiguration('y')
    spawn_yaw = LaunchConfiguration('yaw')

    simulation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(gazebo_share / 'launch' / 'humanoid_demo.launch.py')
        ),
        launch_arguments={
            'world_name': 'small_house',
            'x': spawn_x,
            'y': spawn_y,
            'z': '0.002',
            'yaw': spawn_yaw,
            'gui': LaunchConfiguration('gui'),
            'headless': LaunchConfiguration('headless'),
            'paused': 'false',
            'rviz': 'false',
            'use_sim_time': use_sim_time,
            'perception': 'true',
            'actor_spawn_delay': LaunchConfiguration('actor_spawn_delay'),
            'publish_debug_image': 'true',
            'debug_image_transport': 'raw',
            'inference_backend': LaunchConfiguration('inference_backend'),
            'dnn_target': LaunchConfiguration('dnn_target'),
        }.items(),
    )
    navigation = GroupAction(
        scoped=True,
        actions=[
            SetRemap(src='/cmd_vel', dst='/scenario/nav_cmd_vel'),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    str(gazebo_share / 'launch' / 'navigation.launch.py')
                ),
                launch_arguments={
                    'map': str(
                        gazebo_share / 'maps' / 'small_house.yaml'
                    ),
                    'params_file': str(
                        gazebo_share / 'config' / 'nav2_params.yaml'
                    ),
                    'zone_mask': str(
                        scenario_share / 'maps' / 'zone-filter.yaml'
                    ),
                    'user_map': str(
                        scenario_share
                        / 'maps'
                        / 'small_house_user_map.geojson'
                    ),
                    'use_sim_time': use_sim_time,
                    'rviz': LaunchConfiguration('rviz'),
                    'robot_web': 'true',
                    'robot_web_port': LaunchConfiguration('web_port'),
                    'robot_web_navigation_action': (
                        '/scenario/navigate_to_pose'
                    ),
                    'restore_localization': 'false',
                    'set_initial_pose': 'true',
                    'initial_pose_x': spawn_x,
                    'initial_pose_y': spawn_y,
                    'initial_pose_yaw': spawn_yaw,
                    'localization_source': 'static',
                }.items(),
            ),
        ],
    )
    roaming = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(roaming_share / 'launch' / 'roaming.launch.py')
        ),
        launch_arguments={
            'params_file': str(
                scenario_share / 'config' / 'roaming.yaml'
            ),
            'use_sim_time': use_sim_time,
            'autostart': 'false',
            'random_seed': '94',
        }.items(),
    )
    tracking = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(
                tracking_share / 'launch' / 'person_following.launch.py'
            )
        ),
        launch_arguments={'use_sim_time': use_sim_time}.items(),
    )
    manager = Node(
        package='malbut_scenarios',
        executable='autonomous_driving_manager',
        name='autonomous_driving_manager',
        output='screen',
        parameters=[
            str(scenario_share / 'config' / 'autonomous_driving.yaml'),
            {
                'use_sim_time': use_sim_time,
                'patrol_autostart': LaunchConfiguration(
                    'patrol_autostart'
                ),
                'room_routes_file': str(
                    scenario_share / 'config' / 'room_routes.yaml'
                ),
            },
        ],
    )
    cloud_sync = Node(
        package='malbut_gazebo',
        executable='cloud_robot_sync',
        name='cloud_robot_sync',
        output='screen',
        condition=IfCondition(LaunchConfiguration('cloud_sync')),
        parameters=[{
            'use_sim_time': use_sim_time,
            'backend_url': LaunchConfiguration('cloud_backend_url'),
            'device_id': LaunchConfiguration('cloud_device_id'),
            'token_file': LaunchConfiguration('cloud_token_file'),
            'map_store': LaunchConfiguration('map_store'),
            'local_url': LaunchConfiguration('cloud_local_url'),
            'runtime_request_file': LaunchConfiguration(
                'runtime_request_file'
            ),
        }],
    )
    collision_monitor = Node(
        package='nav2_collision_monitor',
        executable='collision_monitor',
        name='scenario_collision_monitor',
        output='screen',
        parameters=[
            str(scenario_share / 'config' / 'collision_monitor.yaml'),
            {'use_sim_time': use_sim_time},
        ],
    )
    collision_lifecycle = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='scenario_collision_lifecycle_manager',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'autostart': True,
            'node_names': ['scenario_collision_monitor'],
        }],
    )
    image_view = Node(
        package='rqt_image_view',
        executable='rqt_image_view',
        arguments=['/perception/person/debug_image'],
        condition=IfCondition(LaunchConfiguration('image_view')),
        output='screen',
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('gui', default_value='true'),
        DeclareLaunchArgument('headless', default_value='false'),
        DeclareLaunchArgument('rviz', default_value='true'),
        DeclareLaunchArgument('image_view', default_value='true'),
        DeclareLaunchArgument('web_port', default_value='8765'),
        DeclareLaunchArgument('cloud_sync', default_value='false'),
        DeclareLaunchArgument('cloud_backend_url', default_value=''),
        DeclareLaunchArgument('cloud_device_id', default_value=''),
        DeclareLaunchArgument('cloud_token_file', default_value=''),
        DeclareLaunchArgument(
            'cloud_local_url',
            default_value='http://127.0.0.1:8765',
        ),
        DeclareLaunchArgument(
            'map_store',
            default_value=str(
                Path.home() / '.local' / 'share' / 'malbut' / 'maps'
            ),
        ),
        DeclareLaunchArgument('runtime_request_file', default_value=''),
        DeclareLaunchArgument('patrol_autostart', default_value='false'),
        DeclareLaunchArgument('actor_spawn_delay', default_value='180.0'),
        DeclareLaunchArgument('inference_backend', default_value='auto'),
        DeclareLaunchArgument('dnn_target', default_value='auto'),
        DeclareLaunchArgument('x', default_value='-3.665503'),
        DeclareLaunchArgument('y', default_value='-0.4874'),
        DeclareLaunchArgument('yaw', default_value='0.0'),
        simulation,
        navigation,
        roaming,
        tracking,
        manager,
        cloud_sync,
        collision_monitor,
        collision_lifecycle,
        image_view,
    ])
