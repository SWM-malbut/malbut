"""Launch map-subtracted LiDAR foreground preprocessing."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Create the C++ LaserScan preprocessor and its portable arguments."""
    package_share = Path(
        get_package_share_directory('malbut_lidar_preprocessor')
    )
    default_config = package_share / 'config' / 'lidar_foreground.yaml'
    arguments = [
        DeclareLaunchArgument('config', default_value=str(default_config)),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('scan_topic', default_value='/scan'),
        DeclareLaunchArgument('static_map_topic', default_value='/map'),
        DeclareLaunchArgument(
            'clusters_topic',
            default_value='/perception/lidar/foreground_clusters',
        ),
        DeclareLaunchArgument(
            'processing_trace_topic',
            default_value='/perception/sensor_processing_trace',
        ),
        DeclareLaunchArgument('global_frame', default_value='map'),
    ]
    node = Node(
        package='malbut_lidar_preprocessor',
        executable='lidar_foreground_preprocessor',
        name='lidar_foreground_preprocessor',
        output='screen',
        parameters=[
            LaunchConfiguration('config'),
            {
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'scan_topic': LaunchConfiguration('scan_topic'),
                'static_map_topic': LaunchConfiguration('static_map_topic'),
                'clusters_topic': LaunchConfiguration('clusters_topic'),
                'processing_trace_topic': LaunchConfiguration(
                    'processing_trace_topic'
                ),
                'global_frame': LaunchConfiguration('global_frame'),
            },
        ],
    )
    return LaunchDescription(arguments + [node])
