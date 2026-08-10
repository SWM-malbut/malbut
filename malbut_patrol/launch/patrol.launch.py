"""Launch only the Malbut patrol orchestration node."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Build the standalone patrol launch description."""
    share = Path(get_package_share_directory('malbut_patrol'))
    default_route = share / 'config' / 'routes' / 'small_house_patrol.yaml'
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                'route_file',
                default_value=str(default_route),
                description='Absolute path to a patrol route YAML file.',
            ),
            DeclareLaunchArgument(
                'use_sim_time',
                default_value='false',
                description='Use the ROS simulation clock.',
            ),
            DeclareLaunchArgument(
                'autostart',
                default_value='false',
                description='Start and arm patrol as soon as the node launches.',
            ),
            DeclareLaunchArgument(
                'nav2_action_name',
                default_value='navigate_to_pose',
                description='Relative Nav2 NavigateToPose action name.',
            ),
            DeclareLaunchArgument(
                'nav2_server_timeout_seconds',
                default_value='30.0',
                description='Maximum wait for Nav2 before applying failure policy.',
            ),
            Node(
                package='malbut_patrol',
                executable='patrol_manager',
                name='patrol_manager',
                output='screen',
                parameters=[
                    {
                        'autostart': ParameterValue(
                            LaunchConfiguration('autostart'),
                            value_type=bool,
                        ),
                        'nav2_action_name':
                            LaunchConfiguration('nav2_action_name'),
                        'nav2_server_timeout_seconds': ParameterValue(
                            LaunchConfiguration(
                                'nav2_server_timeout_seconds'
                            ),
                            value_type=float,
                        ),
                        'route_file': LaunchConfiguration('route_file'),
                        'use_sim_time': ParameterValue(
                            LaunchConfiguration('use_sim_time'),
                            value_type=bool,
                        ),
                    }
                ],
            ),
        ]
    )
