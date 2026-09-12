"""Start idle exploration and map-saving servers, without starting motion."""

from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue

from malbut_autoslam.runtime import DEFAULT_READY_TIMEOUT_S, LAUNCH_SHUTDOWN_OVERHEAD_S


def _servers(context):
    """Resolve shutdown timing while the enclosing launch scope still exists."""
    sim = ParameterValue(LaunchConfiguration('use_sim_time'), value_type=bool)
    ready_timeout = float(LaunchConfiguration('ready_timeout_s').perform(context))
    return [
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
            # This server owns a separate mapping process group. Do not let
            # parent launch kill it before child cancellation/cleanup finishes.
            # ExecuteLocal evaluates shutdown timers later, after a scoped
            # include may have discarded this launch's configurations.
            sigterm_timeout=str(ready_timeout + LAUNCH_SHUTDOWN_OVERHEAD_S),
            parameters=[{
                'use_sim_time': sim,
                'auto_start': ParameterValue(LaunchConfiguration('auto_start'), value_type=bool),
                'ready_timeout_s': ready_timeout,
                **{
                    name: LaunchConfiguration(name) for name in (
                        'map_topic', 'scan_topic', 'odom_topic', 'normalized_scan_topic',
                        'base_frame', 'navigation_action', 'map_directory')
                }}],
        ),
    ]


def generate_launch_description():
    """Prepare idle servers; start missing real mapping components only on a Goal."""
    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('auto_start', default_value=PythonExpression([
            "'false' if '", LaunchConfiguration('use_sim_time'),
            "' == 'true' else 'true'",
        ]), choices=['true', 'false']),
        DeclareLaunchArgument('map_topic', default_value='/map'),
        DeclareLaunchArgument('scan_topic', default_value='/scan_raw'),
        DeclareLaunchArgument('odom_topic', default_value='/odom'),
        DeclareLaunchArgument('normalized_scan_topic', default_value='/scan_normalized'),
        DeclareLaunchArgument('base_frame', default_value='base_footprint'),
        DeclareLaunchArgument('navigation_action', default_value='/navigate_to_pose'),
        DeclareLaunchArgument('ready_timeout_s', default_value=str(DEFAULT_READY_TIMEOUT_S)),
        DeclareLaunchArgument('map_directory', default_value=str(
            Path.home() / '.ros/malbut/maps')),
        OpaqueFunction(function=_servers),
    ])
