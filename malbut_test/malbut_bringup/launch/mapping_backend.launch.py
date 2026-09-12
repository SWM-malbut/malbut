"""Start only the mapping prerequisites explicitly owned by AutoSLAM."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument, EmitEvent, GroupAction, IncludeLaunchDescription,
    OpaqueFunction, RegisterEventHandler, SetEnvironmentVariable,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, SetRemap


def _file(value):
    path = Path(value).expanduser()
    if not path.is_file():
        raise RuntimeError(f'Mapping prerequisite file not found: {path}')
    return str(path.resolve())


def _vendor_file(package, relative, override):
    if override:
        return _file(override)
    # This robot image omits some include files from its install tree.
    source = Path.home() / 'ros2_ws/src' / package / relative
    if source.is_file():
        return str(source)
    return _file(Path(get_package_share_directory(package)) / relative)


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    def enabled(name):
        return value(name) == 'true'

    sim = enabled('use_sim_time')
    raw = value('scan_topic')
    scan = value('normalized_scan_topic')
    # Resolve everything before starting any process; missing vendor files must
    # not leave half a hardware stack running.
    hardware = (_vendor_file('slam', 'launch/include/robot.launch.py',
                             value('hardware_launch_file'))
                if enabled('start_hardware') else None)
    slam = (_vendor_file('slam', 'config/slam.yaml', value('slam_params_file'))
            if enabled('start_slam') else None)
    navigation = (_vendor_file('navigation', 'launch/include/bringup.launch.py',
                               value('navigation_launch_file'))
                  if enabled('start_navigation') else None)
    params = (_file(value('nav2_params_file') or Path(
        get_package_share_directory('malbut_bringup')) / 'config/nav2_params.yaml')
        if navigation else None)
    actions = [SetEnvironmentVariable('need_compile', 'False')]
    if hardware:
        actions.append(GroupAction([IncludeLaunchDescription(
            PythonLaunchDescriptionSource(hardware), launch_arguments={
                'sim': str(sim).lower(), 'robot_name': '/', 'master_name': '/',
            }.items())], scoped=True))
    if enabled('start_scan_adapter'):
        actions.append(Node(
            package='malbut_bringup', executable='scan_normalizer',
            name='scan_normalizer', output='screen', parameters=[{
                'use_sim_time': sim, 'input_topic': raw, 'output_topic': scan,
            }]))
    if slam:
        actions.append(Node(
            package='slam_toolbox', executable='sync_slam_toolbox_node',
            name='slam_toolbox', output='screen', parameters=[slam, {
                'use_sim_time': sim, 'mode': 'mapping', 'scan_topic': scan,
                'base_frame': 'base_footprint', 'odom_frame': 'odom',
                'map_frame': 'map',
            }]))
    if navigation:
        actions.append(GroupAction([
            SetRemap(src='/scan_normalized', dst=scan),
            SetRemap(src='/odom', dst=value('odom_topic')),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(navigation), launch_arguments={
                    'rtabmap': 'true',  # Vendor switch skips AMCL/map_server.
                    'params_file': params, 'use_sim_time': str(sim).lower(),
                    'namespace': '', 'use_namespace': 'false',
                    'autostart': 'true', 'use_teb': 'false',
                }.items()),
        ], scoped=True))

    def failed(event, launch_context):
        if event.returncode != 0 and not launch_context.is_shutdown:
            return [EmitEvent(event=Shutdown(reason='Mapping prerequisite exited'))]
        return []

    return [RegisterEventHandler(OnProcessExit(on_exit=failed)), *actions]


def generate_launch_description():
    """Expose independent components; launching this file sends no motion goal."""
    defaults = {
        'start_hardware': 'true', 'start_slam': 'true',
        'start_navigation': 'true', 'start_scan_adapter': 'true',
        'use_sim_time': 'false', 'scan_topic': '/scan_raw',
        'odom_topic': '/odom',
        'normalized_scan_topic': '/scan_normalized',
        'hardware_launch_file': '', 'navigation_launch_file': '',
        'slam_params_file': '', 'nav2_params_file': '',
    }
    return LaunchDescription([
        *[DeclareLaunchArgument(name, default_value=default)
          for name, default in defaults.items()],
        OpaqueFunction(function=_setup),
    ])
