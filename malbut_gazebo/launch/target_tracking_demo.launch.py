"""Run the Small House RGB-D person-following demonstration."""

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
    """Start simulation, humanoid, perception, Nav2, RViz, and follower."""
    gazebo_share = Path(get_package_share_directory('malbut_gazebo'))
    tracking_share = Path(get_package_share_directory('malbut_tracking'))
    use_sim_time = LaunchConfiguration('use_sim_time')
    spawn_x = LaunchConfiguration('x')
    spawn_y = LaunchConfiguration('y')
    spawn_yaw = LaunchConfiguration('yaw')

    humanoid_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(gazebo_share / 'launch' / 'humanoid_demo.launch.py')
        ),
        launch_arguments={
            'world_name': LaunchConfiguration('world_name'),
            'x': spawn_x,
            'y': spawn_y,
            'z': '0.002',
            'yaw': spawn_yaw,
            'actor_x': LaunchConfiguration('actor_x'),
            'actor_y': LaunchConfiguration('actor_y'),
            'actor_z': LaunchConfiguration('actor_z'),
            'actor_yaw': LaunchConfiguration('actor_yaw'),
            'actor_name': LaunchConfiguration('actor_name'),
            'actor_spawn_delay': LaunchConfiguration('actor_spawn_delay'),
            'spawn_timeout': LaunchConfiguration('spawn_timeout'),
            'gui': LaunchConfiguration('gui'),
            'headless': LaunchConfiguration('headless'),
            'paused': LaunchConfiguration('paused'),
            'rviz': 'false',
            'use_sim_time': use_sim_time,
            'perception': 'true',
            'publish_debug_image': LaunchConfiguration(
                'publish_debug_image'
            ),
            'debug_image_transport': LaunchConfiguration(
                'debug_image_transport'
            ),
            'dnn_target': LaunchConfiguration('dnn_target'),
            'projection_frame': 'camera_depth_optical_frame',
        }.items(),
    )
    # Only this demo reroutes the simulated drivetrain. Normal Nav2 demos and
    # the canonical robot interface remain unchanged.
    simulation = GroupAction(
        actions=[
            SetRemap(
                src='/cmd_vel',
                dst='/cmd_vel_tracking_output',
            ),
            humanoid_launch,
        ],
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
    tracking = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(tracking_share / 'launch' / 'person_following.launch.py')
        ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'twist_mixer': 'true',
            'nav_cmd_vel_topic': '/cmd_vel',
            'mixed_cmd_vel_topic': '/cmd_vel_tracking_raw',
        }.items(),
    )
    collision_monitor = Node(
        package='nav2_collision_monitor',
        executable='collision_monitor',
        name='tracking_collision_monitor',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'base_frame_id': 'base_footprint',
            'odom_frame_id': 'odom',
            'cmd_vel_in_topic': '/cmd_vel_tracking_raw',
            'cmd_vel_out_topic': '/cmd_vel_tracking_output',
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
        name='tracking_collision_lifecycle_manager',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'autostart': True,
            'node_names': ['tracking_collision_monitor'],
        }],
    )
    image_view = Node(
        package='rqt_image_view',
        executable='rqt_image_view',
        arguments=['/perception/person/debug_image'],
        output='screen',
        condition=IfCondition(LaunchConfiguration('image_view')),
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('world_name', default_value='small_house'),
        DeclareLaunchArgument('gui', default_value='true'),
        DeclareLaunchArgument('headless', default_value='false'),
        DeclareLaunchArgument('paused', default_value='false'),
        DeclareLaunchArgument('rviz', default_value='true'),
        DeclareLaunchArgument('image_view', default_value='true'),
        DeclareLaunchArgument('publish_debug_image', default_value='true'),
        DeclareLaunchArgument(
            'debug_image_transport', default_value='raw'
        ),
        DeclareLaunchArgument('dnn_target', default_value='auto'),
        DeclareLaunchArgument('actor_spawn_delay', default_value='15.0'),
        DeclareLaunchArgument('actor_name', default_value='humanoid_target'),
        DeclareLaunchArgument('actor_z', default_value='0.0'),
        DeclareLaunchArgument('spawn_timeout', default_value='60'),
        DeclareLaunchArgument('x', default_value='-3.665503'),
        DeclareLaunchArgument('y', default_value='-0.4874'),
        DeclareLaunchArgument('yaw', default_value='0.0'),
        DeclareLaunchArgument('actor_x', default_value='-2.19'),
        DeclareLaunchArgument('actor_y', default_value='-1.17'),
        DeclareLaunchArgument('actor_yaw', default_value='0.0'),
        simulation,
        navigation,
        tracking,
        collision_monitor,
        collision_lifecycle,
        image_view,
    ])
