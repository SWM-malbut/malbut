"""Start the whole robot once: vendor hardware, Nav2, applications and manager."""

import os
from pathlib import Path
import shlex
from uuid import uuid4

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument, GroupAction, IncludeLaunchDescription,
    LogInfo, OpaqueFunction, RegisterEventHandler, SetEnvironmentVariable,
)
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.fall_setup import prepare_fall_monitor
from malbut_bringup.nav2_stack import nav2_actions
from malbut_bringup.perception_setup import validate_perception_files

# Nav2 AssistedTeleop input used by the manual_drive capability.
TELEOP_TOPIC = '/cmd_vel_teleop'
# The readiness checker's report; the manager accepts missions after READY.
READY_TOPIC = '/malbut/bringup/status'
RELOCALIZE_ACTION = '/relocalize'


def _file(value, label):
    path = Path(value).expanduser()
    if not value or not path.is_file():
        raise RuntimeError(f'{label}: file not found: {value!r}')
    return str(path.resolve())


def _package_file(package, relative):
    return _file(Path(get_package_share_directory(package)) / relative,
                 f'{package}/{relative}')


def _include(path, arguments):
    # In particular, a child's generic "config" must not affect its siblings.
    # Pass clock mode through the child launch, not a global SetParameter:
    # creating /** first lets named YAML values override later inline wiring.
    return GroupAction([
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

    perception = value('perception') == 'true'
    hardware = value('start_hardware') == 'true'
    relocalization = value('relocalization') == 'true'
    # The unified robot launch always starts navigation. The separate mapping
    # backend does not own fall analysis. Permissions still gate all collection.
    fall_inputs = prepare_fall_monitor(value('fall_monitor'), value('fall_config'))
    fall_monitor = None
    fall_pose = None
    fall_coordinator = None
    # The legacy "manager" wire slot now belongs to the fall coordinator.
    fall_ids = {key: str(uuid4()) for key in ('manager', 'bridge', 'vlm')} if fall_inputs else {}
    if fall_inputs is not None:
        fall_config, fall_image_topic = fall_inputs
        fall_coordinator = Node(
            package='malbut_fall_coordinator', executable='fall_coordinator',
            name='fall_coordinator', output='screen',
            parameters=[{
                'use_sim_time': False, 'runtime_id': fall_ids['manager'],
                'bridge_runtime_id': fall_ids['bridge'], 'vlm_runtime_id': fall_ids['vlm'],
            }],
        )
        pose_model = _file(value('fall_pose_model_path'), 'Fall pose ONNX model')
        pose_python = str(Path(value('fall_pose_python_executable')).expanduser())
        _file(pose_python, 'Fall pose Python')
        if not os.access(pose_python, os.X_OK):
            raise RuntimeError('Fall pose Python is not executable')
        fall_pose = Node(
            package='homecam_detector', executable='homecam_detector_node',
            name='malbut_fall_pose', output='screen', prefix=[shlex.quote(pose_python)],
            parameters=[{
                'use_sim_time': False, 'fall_only': True,
                'fall_runtime_id': fall_ids['vlm'],
                'image_topic': value('rgb_topic'), 'odom_topic': value('odom_topic'),
                'pose_model_path': pose_model, 'pose_keep_aspect': True,
                'pose_inference_fps': 5.0,
                'pose_candidate_confidence_threshold': 0.10,
            }],
        )
        fall_monitor = Node(
            package='malbut_agent_server', executable='malbut-fall-monitor',
            output='screen', arguments=['--config', fall_config, '--execute'],
            parameters=[{'use_sim_time': False, 'manager_runtime_id': fall_ids['manager'],
                         'runtime_id': fall_ids['vlm']}],
            remappings=[(fall_image_topic, value('rgb_topic'))],
        )
    speech = None
    if value('speech') == 'true':
        # Preserve the venv executable path: resolving its symlink would select
        # system Python and lose the isolated speech dependencies.
        python = str(Path(value('speech_python_executable')).expanduser())
        _file(python, 'speech Python')
        if not os.access(python, os.X_OK):
            raise RuntimeError(f'speech Python is not executable: {python}')
        speech = _include(_package_file('malbut_bringup', 'launch/speech.launch.py'), {
            'python_executable': python,
            'stt_model_path': _file(value('stt_model_path'), 'STT model'),
            'stt_library_path': _file(value('stt_library_path'), 'STT CUDA library'),
            'input_device': value('speech_input_device'),
            'output_device': value('speech_output_device'),
            'cpp_threads': value('stt_cpp_threads'),
            'input_has_aec': value('speech_input_has_aec'),
            'agent_provider': value('speech_agent_provider'),
            'control_server': 'manager',
            'preflight_timeout_s': value('speech_preflight_timeout_s'),
            'peer_timeout_s': value('speech_peer_timeout_s'),
            'preflight_only': 'false',
        })
    web_parameters = {
        'use_sim_time': False, 'manage_bringup': False,
        'map_directory': value('map_directory'),
        'map_topic': value('patrol_costmap_topic'),
        'robot_frame': value('robot_frame'), 'rgb_topic': value('rgb_topic'),
    }

    # Validate paths before starting any child process. Never substitute a
    # small_house/test map or overwrite the vendor's calibration/parameters.
    perception_files = {}
    if perception:
        perception_files = validate_perception_files(
            value('python_executable'), value('reid_python_executable'),
            value('model_path'), value('reid_model_path'))
    hardware_path = None
    if hardware:
        hardware_path = (_file(value('hardware_launch_file'), 'hardware launch')
                         if value('hardware_launch_file') else _package_file(
                             'slam', 'launch/include/robot.launch.py'))
    params = (_file(value('nav2_params_file'), 'Nav2 parameters')
              if value('nav2_params_file') else _package_file(
                  'malbut_bringup', 'config/nav2_params.yaml'))
    slam_params = (_file(value('slam_params_file'), 'SLAM parameters')
                   if value('slam_params_file') else _package_file(
                       'malbut_bringup', 'config/slam_toolbox.yaml'))
    # No map starts SLAM mapping; a map starts saved-map AMCL. The manager
    # switches between them at runtime, so this only picks the first state.
    initial_map = _file(value('map'), 'saved map YAML') if value('map') else ''

    # Reuse the outbound bridge's configuration. Offline/LAN-only Bringup
    # does not need cloud media; never put the device token in launch arguments.
    backend_url = context.environment.get('HOMECAM_BACKEND_URL', '').strip()
    media_path = (_package_file('homecam_media_agent', 'launch/homecam_robot.launch.py')
                  if backend_url else None)

    actions = []
    if fall_monitor is None:
        reason = ('fall_monitor=false' if value('fall_monitor') == 'false'
                  else 'fall_config is missing; set fall_config or MALBUT_FALL_CONFIG')
        actions.append(LogInfo(msg=f'Cloud VLM startup disabled: {reason}.'))
    if hardware_path:
        actions.append(_include(hardware_path, {
            # This vendor version uses '/' (not '') for unprefixed TF/topics.
            'sim': 'false', 'robot_name': '/', 'master_name': '/',
            # Aurora's upstream launch accepts this inherited argument. Person
            # tracking uses the RGB/depth images; nothing needs a point cloud.
            'point_cloud_enable': 'false',
            # The joystick feeds manual_drive instead of the driver, so its
            # commands can no longer mix with autonomous /cmd_vel output.
            'use_joy': 'false',
        }))
        # Same vendor node and speeds as its hardware launch; only the output moves.
        actions.append(Node(
            package='peripherals', executable='joystick_control',
            name='joystick_control', output='screen', parameters=[{
                'use_sim_time': False, 'max_linear': 0.15, 'max_angular': 0.45,
                'disable_servo_control': True,
            }],
            remappings=[('controller/cmd_vel', TELEOP_TOPIC)]))
    if media_path:
        actions.append(_include(media_path, {
            'backend_url': backend_url,
            'device_id': context.environment.get('HOMECAM_DEVICE_ID', 'jetson-homecam'),
            'image_topic': value('rgb_topic'),
            'camera_info_topic': value('camera_info_topic'),
            'odom_topic': value('odom_topic'),
            **{'fall_' + key + '_runtime_id': value for key, value in fall_ids.items()},
        }))

    # Nav2 servers with Malbut-owned parameters, composed like the vendor stack,
    # plus the Collision Monitor and Zone mask. Localization stays unconfigured
    # until the manager selects SLAM or a saved map.
    actions.extend(nav2_actions(
        params, scan_topic=value('scan_topic'), odom_topic=value('odom_topic')))

    if relocalization:
        actions.append(Node(
            package='malbut_relocalization', executable='relocalization',
            name='relocalization', output='screen', parameters=[{'use_sim_time': False}]))
    if value('web_panel') == 'true':
        actions.append(Node(
            package='malbut_bringup', executable='robot_web_panel',
            name='robot_web_panel', output='screen', parameters=[web_parameters]))

    if perception:
        options = {name: value(name) for name in (
            'rgb_topic', 'depth_topic', 'camera_info_topic', 'model_path',
            'python_executable', 'reid_python_executable', 'device', 'reid_model_path',
            'inference_backend', 'dnn_target', 'publish_debug_image',
        )}
        options.update(perception_files)
        options['reid_backend'] = 'osnet'
        actions.append(_include(_package_file(
            'malbut_tracking', 'launch/person_detection.launch.py'), options))
        follower = {name: value(name) for name in (
            'scan_topic', 'global_frame', 'robot_frame', 'static_map_topic',
            'global_costmap_topic',
        )}
        if value('following_config'):
            follower['config'] = _file(value('following_config'), 'follower config')
        # An empty parent LaunchConfiguration otherwise shadows the child's
        # default, which ROS interprets as the current directory, not a YAML.
        follower['lidar_config'] = (
            _file(value('lidar_config'), 'LiDAR config') if value('lidar_config')
            else _package_file('malbut_tracking', 'config/lidar_foreground.yaml'))
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
    # Bringup owns SLAM and Nav2 here; the Goal never starts a second stack.
    actions.append(_include(_package_file(
        'malbut_autoslam', 'launch/autoslam.launch.py'), {
            'map_directory': value('map_directory'),
            'map_topic': value('static_map_topic'),
            'base_frame': value('robot_frame'),
        }))

    # The manager starts immediately: it owns localization, which Nav2 needs
    # before it can activate. Missions wait for the readiness report instead.
    actions.append(Node(
        package='malbut_system_manager', executable='system_manager',
        name='system_manager', output='screen',
        parameters=[{
            'use_sim_time': False, 'ready_topic': READY_TOPIC,
            'localization_control': True, 'initial_map': initial_map,
            'slam_params_file': slam_params, 'scan_topic': value('scan_topic'),
            # Each saved-map load finds the robot through the Action, saved pose first.
            'relocalize_action': (RELOCALIZE_ACTION if relocalization
                                  and value('restore_pose') == 'true' else ''),
        }],
    ))
    actions.append(Node(
        package='malbut_system_manager', executable='manual_control',
        name='manual_control', output='screen',
        parameters=[{'use_sim_time': False, 'teleop_topic': TELEOP_TOPIC}],
    ))

    wait = Node(
        package='malbut_bringup', executable='wait_for_robot',
        name='bringup_readiness', output='screen',
        parameters=[{
            'use_sim_time': False, 'navigation': True,
            'perception': perception, 'relocalization': relocalization,
            **{name: value(name) for name in (
                'scan_topic', 'odom_topic', 'rgb_topic', 'depth_topic',
                'camera_info_topic', 'global_frame', 'robot_frame',
                'static_map_topic', 'global_costmap_topic', 'patrol_costmap_topic',
            )},
            'sensor_timeout_s': float(value('sensor_timeout_s')),
        }],
    )

    def ready(event, launch_context):
        if launch_context.is_shutdown:
            return []
        if event.returncode != 0:
            raise RuntimeError('Robot readiness check failed')
        speech_actions = [speech] if speech else []
        fall_actions = []
        if fall_monitor is not None:
            fall_actions = [
                LogInfo(msg='Starting fall coordinator and VLM; waiting for permissions.'),
                fall_coordinator,
                fall_monitor,
                fall_pose,
            ]
        return [LogInfo(msg='Robot ready; the system manager accepts missions.'),
                *fall_actions, *speech_actions]

    def child_exited(event, launch_context):
        if launch_context.is_shutdown or event.action is wait:
            return []
        # Vendor one-shot initialization tools may exit normally. Malbut
        # servers, however, must not silently leave a partially running stack.
        is_malbut = (
            isinstance(event.action, Node)
            and str(event.action.node_package).startswith('malbut_')
        )
        if event.returncode != 0 or is_malbut or event.action is fall_pose:
            # Also covers nested speech failures. A plain Shutdown would mask
            # a failed component as a successful shell exit (status 0).
            raise RuntimeError(f'Bringup child exited: {event.process_name}')
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
    speech_cache = cache_root / 'malbut_speech'
    speech_runtime = Path(os.environ.get(
        'MALBUT_SPEECH_RUNTIME', speech_cache / 'runtime')).expanduser()
    stt_build = Path(os.environ.get(
        'MALBUT_STT_BUILD_DIR', speech_cache / 'whisper-cpp-build')).expanduser()
    defaults = {
        'start_hardware': 'true',
        'relocalization': 'true',
        'restore_pose': 'true',
        'web_panel': 'false',
        'perception': 'true',
        'fall_monitor': 'auto',
        'fall_config': os.environ.get('MALBUT_FALL_CONFIG', '/etc/malbut/fall_runtime.json'),
        'fall_pose_model_path': os.environ.get(
            'MALBUT_FALL_POSE_MODEL', str(cache / 'yolo26s-pose.onnx')),
        'fall_pose_python_executable': os.environ.get(
            'MALBUT_FALL_POSE_PYTHON',
            str(cache_root / 'malbut_fall_pose/runtime/bin/python')),
        'speech': 'true',
        'speech_python_executable': str(speech_runtime / 'bin/python'),
        'stt_model_path': os.environ.get(
            'MALBUT_STT_MODEL_PATH', str(speech_cache / 'models/ggml-small.bin')),
        'stt_library_path': os.environ.get(
            'MALBUT_STT_LIBRARY_PATH', str(stt_build / 'bin/libmalbut_whisper.so')),
        'speech_input_device': '0',
        'speech_output_device': '-1',
        'stt_cpp_threads': '6',
        'speech_input_has_aec': 'false',
        'speech_agent_provider': 'openai',
        'speech_preflight_timeout_s': '120.0',
        'speech_peer_timeout_s': '30.0',
        'hardware_launch_file': '',
        'map': '',
        'map_directory': str(Path.home() / '.ros/malbut/maps'),
        'nav2_params_file': '',
        'slam_params_file': '',
        # Topic names match the robot's supplied topic list. Header frames and
        # RGB-D alignment still require device verification; do not invent TF.
        'rgb_topic': '/depth_cam/rgb0/image_raw',
        'depth_topic': '/depth_cam/depth0/image_raw',
        'camera_info_topic': '/depth_cam/rgb0/camera_info',
        'scan_topic': '/scan_raw',
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
        'fall_monitor': ['auto', 'true', 'false'],
        **{key: ['true', 'false'] for key in (
            'start_hardware', 'perception',
            'publish_debug_image', 'relocalization',
            'restore_pose', 'web_panel', 'speech', 'speech_input_has_aec',
        )},
        'speech_agent_provider': ['openai', 'mock'],
    }
    return LaunchDescription([
        # The vendor hardware launch reads its complete includes from src.
        SetEnvironmentVariable('need_compile', 'False'),
        *[DeclareLaunchArgument(
            key, default_value=default, choices=choices.get(key))
          for key, default in defaults.items()],
        OpaqueFunction(function=_setup),
    ])
