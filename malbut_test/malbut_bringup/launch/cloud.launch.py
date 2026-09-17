"""Connect the real robot to the service without exposing a local HTTP port."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Keep startup idle; reuse the existing device credential file convention."""
    return LaunchDescription([
        DeclareLaunchArgument('backend_url', default_value=EnvironmentVariable(
            'HOMECAM_BACKEND_URL', default_value='')),
        DeclareLaunchArgument('token_file', default_value=EnvironmentVariable(
            'HOMECAM_DEVICE_TOKEN_FILE', default_value='')),
        DeclareLaunchArgument('map_directory', default_value=EnvironmentVariable(
            'HOMECAM_MAP_DIRECTORY', default_value='~/.ros/malbut/maps')),
        # Child Bringup processes inherit these even when configured through
        # launch arguments rather than shell exports. The token stays in a file.
        SetEnvironmentVariable('HOMECAM_BACKEND_URL', LaunchConfiguration('backend_url')),
        SetEnvironmentVariable('HOMECAM_DEVICE_TOKEN_FILE', LaunchConfiguration('token_file')),
        Node(package='malbut_bringup', executable='robot_cloud_sync',
             name='robot_cloud_sync', output='screen', parameters=[{
                 'use_sim_time': False, 'manage_bringup': True, 'map_topic': '/map',
                 'backend_url': LaunchConfiguration('backend_url'),
                 'token_file': LaunchConfiguration('token_file'),
                 'map_directory': LaunchConfiguration('map_directory'),
             }]),
    ])
