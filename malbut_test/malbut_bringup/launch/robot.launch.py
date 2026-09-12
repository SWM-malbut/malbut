"""Compose vendor hardware, shared perception, and idle Action servers."""

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument, EmitEvent, GroupAction, IncludeLaunchDescription,
    LogInfo, OpaqueFunction, RegisterEventHandler, SetEnvironmentVariable,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, SetParameter, SetRemap


def _file(value, label):
    path = Path(value).expanduser()
    if not value or not path.is_file():
        raise RuntimeError(f'{label}: file not found: {value!r}')
    return str(path.resolve())


def _package_file(package, relative):
    return _file(Path(get_package_share_directory(package)) / relative,
                 f'{package}/{relative}')


def _include(path, arguments, remappings=()):
    # In particular, a child's generic "config" must not affect its siblings.
    return GroupAction([
        SetParameter('use_sim_time', False),
        *[SetRemap(src=source, dst=destination) for source, destination in remappings],
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(path),
            launch_arguments={
                **arguments, 'use_sim_time': 'false',
            }.items(),
        ),
    ], scoped=True)


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    navigating = value('mode') == 'navigation'
    mapping = value('mode') == 'mapping'
    perception = value('perception') == 'true'
    hardware = value('start_hardware') == 'true'
    navigation = value('start_navigation') == 'true'
    if navigating and not perception:
        raise RuntimeError('navigation mode requires perception for FollowPerson')

    if mapping:
        # AutoSLAM starts missing prerequisites on a Goal, not on page load.
        actions = [_include(_package_file(
            'malbut_autoslam', 'launch/autoslam.launch.py'), {
                'auto_start': 'true', 'scan_topic': value('raw_scan_topic'),
                'normalized_scan_topic': value('scan_topic'),
                'odom_topic': value('odom_topic'),
            })]
        if value('web_panel') == 'true':
            actions.append(Node(
                package='malbut_bringup', executable='robot_web_panel',
                name='robot_web_panel', output='screen'))
        return actions

    # Validate paths before starting any child process. Never substitute a
    # small_house/test map or overwrite the vendor's calibration/parameters.
    hardware_path = None
    if hardware:
        hardware_path = (_file(value('hardware_launch_file'), 'hardware launch')
                         if value('hardware_launch_file') else _package_file(
                             'slam', 'launch/include/robot.launch.py'))
    navigation_path = None
    navigation_arguments = {}
    if navigating and navigation:
        map_path = _file(value('map'), 'real saved map YAML')
        navigation_path = _file(value('navigation_launch_file'), 'navigation launch')
        params = (_file(value('nav2_params_file'), 'Nav2 parameters')
                  if value('nav2_params_file') else _package_file(
                      'malbut_bringup', 'config/nav2_params.yaml'))
        navigation_arguments = {
            'map': map_path, 'params_file': params,
            'namespace': '', 'use_namespace': 'false',
            'autostart': 'true', 'use_teb': 'false',
        }

    actions = []
    if hardware_path:
        actions.append(_include(hardware_path, {
            # This vendor version uses '/' (not '') for unprefixed TF/topics.
            'sim': 'false', 'robot_name': '/', 'master_name': '/',
        }))
    if navigation_path:
        actions.append(_include(navigation_path, navigation_arguments, remappings=[
            ('/scan_normalized', value('scan_topic')),
            ('/odom', value('odom_topic')),
        ]))

    if value('start_scan_adapter') == 'true':
        actions.append(Node(
            package='malbut_bringup', executable='scan_normalizer',
            name='scan_normalizer', output='screen', parameters=[{
                'use_sim_time': False, 'input_topic': value('raw_scan_topic'),
                'output_topic': value('scan_topic'),
            }]))
    if navigating and value('map') and value('pose_memory') == 'true':
        actions.append(Node(
            package='malbut_bringup', executable='pose_memory',
            name='pose_memory', output='screen', parameters=[{
                'use_sim_time': False, 'map': value('map'),
                'restore_pose': value('restore_pose') == 'true',
            }]))
    if value('web_panel') == 'true':
        actions.append(Node(
            package='malbut_bringup', executable='robot_web_panel',
            name='robot_web_panel', output='screen'))

    if perception:
        options = {name: value(name) for name in (
            'rgb_topic', 'depth_topic', 'camera_info_topic', 'model_path',
            'python_executable', 'reid_python_executable', 'device', 'reid_model_path',
            'inference_backend', 'dnn_target', 'publish_debug_image',
        )}
        options['reid_backend'] = 'osnet'
        actions.append(_include(_package_file(
            'malbut_tracking', 'launch/person_detection.launch.py'), options))

    if navigating:
        follower = {name: value(name) for name in (
            'scan_topic', 'global_frame', 'robot_frame', 'static_map_topic',
            'global_costmap_topic',
        )}
        if value('following_config'):
            follower['config'] = _file(value('following_config'), 'follower config')
        if value('lidar_config'):
            follower['lidar_config'] = _file(value('lidar_config'), 'LiDAR config')
        actions.append(_include(_package_file(
            'malbut_tracking', 'launch/person_following.launch.py'), follower))
        actions.append(_include(_package_file(
            'malbut_patrol', 'launch/patrol.launch.py'), {
                'map_topic': value('static_map_topic'),
                'costmap_topic': value('patrol_costmap_topic'),
                'base_frame': value('robot_frame'),
                'camera_image_topic': value('rgb_topic'),
                'camera_info_topic': value('camera_info_topic'),
                'room_map_file': value('room_map_file'),
            }))

    wait = Node(
        package='malbut_bringup', executable='wait_for_robot',
        name='bringup_readiness', output='screen',
        parameters=[{
            'use_sim_time': False, 'navigation': navigating,
            'perception': perception,
            **{name: value(name) for name in (
                'scan_topic', 'odom_topic', 'rgb_topic', 'depth_topic',
                'camera_info_topic', 'global_frame', 'robot_frame',
                'static_map_topic', 'global_costmap_topic', 'patrol_costmap_topic',
            )},
            'sensor_timeout_s': float(value('sensor_timeout_s')),
        }],
    )
    manager = Node(
        package='malbut_system_manager', executable='system_manager',
        name='system_manager', output='screen',
        parameters=[{'use_sim_time': False}],
    )

    def ready(event, launch_context):
        if launch_context.is_shutdown:
            return []
        if event.returncode != 0:
            return [EmitEvent(event=Shutdown(reason='Robot readiness check failed'))]
        if navigating:
            return [LogInfo(msg='Robot ready; starting system manager.'), manager]
        return [LogInfo(msg='Sensors ready. No navigation or missions were started.')]

    def child_exited(event, launch_context):
        if launch_context.is_shutdown or event.action is wait:
            return []
        # Vendor one-shot initialization tools may exit normally. Malbut
        # servers, however, must not silently leave a partially running stack.
        is_malbut = (
            isinstance(event.action, Node)
            and str(event.action.node_package).startswith('malbut_')
        )
        if event.returncode != 0 or is_malbut:
            return [EmitEvent(event=Shutdown(
                reason=f'Bringup child exited: {event.process_name}',
            ))]
        return []

    return [
        RegisterEventHandler(OnProcessExit(target_action=wait, on_exit=ready)),
        RegisterEventHandler(OnProcessExit(on_exit=child_exited)),
        *actions, wait,
    ]


def generate_launch_description():
    """Expose only wiring and startup choices; never send motion goals."""
    cache_root = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')).expanduser()
    cache = cache_root / 'malbut_perception'
    yolo_runtime = Path(os.environ.get(
        'MALBUT_YOLO_RUNTIME', cache_root / 'malbut_yolo/runtime')).expanduser()
    reid_runtime = Path(os.environ.get(
        'MALBUT_REID_RUNTIME', cache_root / 'malbut_reid/runtime')).expanduser()
    defaults = {
        'mode': 'sensors',
        'start_hardware': 'true',
        'start_navigation': 'true',
        'start_scan_adapter': 'true',
        'pose_memory': 'true',
        'restore_pose': 'true',
        'web_panel': 'false',
        'perception': 'true',
        'hardware_launch_file': '',
        # The robot image installs only the top-level navigation launches.
        # Its existing source tree contains the lower, navigation-only launch.
        'navigation_launch_file': str(
            Path.home() / 'ros2_ws/src/navigation/launch/include/bringup.launch.py'),
        'map': '',
        'nav2_params_file': '',
        # Topic names match the robot's supplied topic list. Header frames and
        # RGB-D alignment still require device verification; do not invent TF.
        'rgb_topic': '/depth_cam/rgb0/image_raw',
        'depth_topic': '/depth_cam/depth0/image_raw',
        'camera_info_topic': '/depth_cam/rgb0/camera_info',
        'raw_scan_topic': '/scan_raw',
        'scan_topic': '/scan_normalized',
        'odom_topic': '/odom',
        'global_frame': 'map',
        'robot_frame': 'base_footprint',
        'static_map_topic': '/map',
        'global_costmap_topic': '/global_costmap/costmap_raw',
        'patrol_costmap_topic': '/global_costmap/costmap',
        'room_map_file': '',
        'following_config': '',
        'lidar_config': '',
        'model_path': str(cache / 'yolo26n.pt'),
        'python_executable': str(yolo_runtime / 'bin/python'),
        'reid_python_executable': str(reid_runtime / 'bin/python'),
        'device': 'cuda:0',
        'reid_model_path': str(cache / 'osnet_ain_x1_0_msmt17.onnx'),
        'inference_backend': 'auto',
        'dnn_target': 'auto',
        'publish_debug_image': 'false',
        'sensor_timeout_s': '3.0',
    }
    choices = {
        'mode': ['sensors', 'navigation', 'mapping'],
        **{key: ['true', 'false'] for key in (
            'start_hardware', 'start_navigation', 'perception',
            'publish_debug_image', 'start_scan_adapter', 'pose_memory',
            'restore_pose', 'web_panel',
        )},
    }
    return LaunchDescription([
        # The supplied robot image keeps complete vendor launch includes in src.
        SetEnvironmentVariable('need_compile', 'False'),
        *[DeclareLaunchArgument(
            key, default_value=default, choices=choices.get(key))
          for key, default in defaults.items()],
        OpaqueFunction(function=_setup),
    ])
