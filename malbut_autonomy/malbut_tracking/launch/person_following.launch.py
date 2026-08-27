"""Launch the configurable sensor-driven person follower."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Create the target-tracking node and its runtime arguments."""
    package_share = Path(get_package_share_directory('malbut_tracking'))
    lidar_share = Path(
        get_package_share_directory('malbut_lidar_preprocessor')
    )
    default_config = package_share / 'config' / 'person_following.yaml'
    default_lidar_config = lidar_share / 'config' / 'lidar_foreground.yaml'
    arguments = [
        DeclareLaunchArgument('config', default_value=str(default_config)),
        DeclareLaunchArgument(
            'lidar_config', default_value=str(default_lidar_config)
        ),
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
        DeclareLaunchArgument('scan_topic', default_value='/scan'),
        DeclareLaunchArgument(
            'lidar_clusters_topic',
            default_value='/perception/lidar/foreground_clusters',
        ),
        DeclareLaunchArgument('global_frame', default_value='map'),
        DeclareLaunchArgument('robot_frame', default_value='base_footprint'),
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
                'lidar_clusters_topic': LaunchConfiguration(
                    'lidar_clusters_topic'
                ),
                'global_frame': LaunchConfiguration('global_frame'),
                'robot_frame': LaunchConfiguration('robot_frame'),
            },
        ],
    )
    lidar_preprocessor = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(lidar_share / 'launch' / 'lidar_foreground.launch.py')
        ),
        launch_arguments={
            'config': LaunchConfiguration('lidar_config'),
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'scan_topic': LaunchConfiguration('scan_topic'),
            'static_map_topic': LaunchConfiguration('static_map_topic'),
            'clusters_topic': LaunchConfiguration('lidar_clusters_topic'),
            'global_frame': LaunchConfiguration('global_frame'),
        }.items(),
    )
    return LaunchDescription(arguments + [lidar_preprocessor, node])
