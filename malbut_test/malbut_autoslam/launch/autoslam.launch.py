"""Start idle exploration and map-saving servers, without starting motion."""

from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Prepare idle servers; mapping SLAM and Nav2 come from the robot Bringup."""
    sim = ParameterValue(LaunchConfiguration('use_sim_time'), value_type=bool)
    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('map_topic', default_value='/map'),
        DeclareLaunchArgument('base_frame', default_value='base_footprint'),
        DeclareLaunchArgument('navigation_action', default_value='/navigate_to_pose'),
        DeclareLaunchArgument('planning_action', default_value='/compute_path_to_pose'),
        DeclareLaunchArgument('ready_timeout_s', default_value='30.0'),
        DeclareLaunchArgument('max_exploration_time_s', default_value='1200.0'),
        DeclareLaunchArgument('map_directory', default_value=str(
            Path.home() / '.ros/malbut/maps')),
        Node(
            package='nav2_map_server', executable='map_saver_server',
            name='autoslam_map_saver', output='screen',
            parameters=[{'use_sim_time': sim,
                         'map_subscribe_transient_local': True}],
        ),
        Node(
            package='nav2_lifecycle_manager', executable='lifecycle_manager',
            name='autoslam_saver_lifecycle', output='screen',
            parameters=[{'use_sim_time': sim, 'autostart': True,
                         'node_names': ['autoslam_map_saver']}],
        ),
        Node(
            package='malbut_autoslam', executable='autoslam', output='screen',
            parameters=[{
                'use_sim_time': sim,
                'ready_timeout_s': ParameterValue(
                    LaunchConfiguration('ready_timeout_s'), value_type=float),
                'max_exploration_time_s': ParameterValue(
                    LaunchConfiguration('max_exploration_time_s'), value_type=float),
                **{
                    name: LaunchConfiguration(name) for name in (
                        'map_topic', 'base_frame', 'navigation_action',
                        'planning_action', 'map_directory')
                }}],
        ),
    ])
