"""Launch the configurable sensor-driven person follower."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Create the target-tracking node and its runtime arguments."""
    package_share = Path(get_package_share_directory('malbut_tracking'))
    default_config = package_share / 'config' / 'person_following.yaml'
    arguments = [
        DeclareLaunchArgument('config', default_value=str(default_config)),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument(
            'detections_topic',
            default_value='/perception/person/detections_3d',
        ),
        DeclareLaunchArgument(
            'global_costmap_topic',
            default_value='/global_costmap/costmap_raw',
        ),
        DeclareLaunchArgument('static_map_topic', default_value='/map'),
        DeclareLaunchArgument('global_frame', default_value='map'),
        DeclareLaunchArgument('robot_frame', default_value='base_footprint'),
        DeclareLaunchArgument('twist_mixer', default_value='false'),
        DeclareLaunchArgument(
            'nav_cmd_vel_topic', default_value='/cmd_vel'
        ),
        DeclareLaunchArgument(
            'mixed_cmd_vel_topic', default_value='/cmd_vel_tracking_raw'
        ),
    ]
    node = Node(
        package='malbut_tracking',
        executable='person_follower',
        name='person_follower',
        output='screen',
        parameters=[
            LaunchConfiguration('config'),
            {
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'detections_topic': LaunchConfiguration('detections_topic'),
                'global_costmap_topic': LaunchConfiguration(
                    'global_costmap_topic'
                ),
                'static_map_topic': LaunchConfiguration('static_map_topic'),
                'global_frame': LaunchConfiguration('global_frame'),
                'robot_frame': LaunchConfiguration('robot_frame'),
            },
        ],
    )
    twist_mixer = Node(
        package='malbut_tracking',
        executable='tracking_twist_mixer',
        name='tracking_twist_mixer',
        output='screen',
        condition=IfCondition(LaunchConfiguration('twist_mixer')),
        parameters=[
            LaunchConfiguration('config'),
            {
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'nav_input_topic': LaunchConfiguration('nav_cmd_vel_topic'),
                'output_topic': LaunchConfiguration('mixed_cmd_vel_topic'),
            },
        ],
    )
    return LaunchDescription(arguments + [node, twist_mixer])
