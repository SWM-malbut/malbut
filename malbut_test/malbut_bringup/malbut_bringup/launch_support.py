"""Shared wiring helpers; no external ROS readiness gates."""

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument, GroupAction, IncludeLaunchDescription,
    LogInfo, OpaqueFunction, SetEnvironmentVariable,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource


def defaults():
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
    pose_cache = cache_root / 'malbut_fall_pose'
    pose_python = pose_cache / 'runtime/bin/python'
    values = {
        'start_hardware': 'true',
        'relocalization': 'true',
        'restore_pose': 'true',
        'web_panel': 'false',
        'perception': 'true',
        'fall_monitor': 'auto',
        'fall_config': os.environ.get('MALBUT_FALL_CONFIG', '/etc/malbut/fall_runtime.json'),
        'fall_pose_model_path': os.environ.get(
            'MALBUT_FALL_POSE_MODEL', str(cache / 'yolo26s-pose.onnx')),
        # Apply measured thread tuning without extra deployment arguments.
        # Auto uses CUDA when installed; CPU selection is logged explicitly.
        'fall_pose_execution_provider': 'auto',
        'fall_pose_intra_op_num_threads': '2',
        'fall_pose_allow_spinning': 'false',
        'fall_pose_opencv_num_threads': '1',
        # One switch: the Pose floor height (bed or sofa is not a fall) and the
        # monitor's map places use the same depth. On since 2026-10-08: person
        # following already reads this depth/RGB pair as aligned on the robot and
        # holds 0.6 m from a person with it (the fall approach stops 1 m away).
        'fall_depth_aligned_to_rgb': 'true',
        'fall_depth_camera_info_topic': '/depth_cam/rgb0/camera_info',
        # The camera mount measured against the floor on the robot (2026-10-09,
        # twice: 11.5/11.6 cm, 1.6 degrees down, left side 1.6 degrees low). The
        # model says 12.0 cm and level; Pose floor heights use these values and
        # the monitor's map places are corrected by the difference.
        'fall_camera_height_m': '0.116',
        'fall_camera_pitch_rad': '0.028',
        'fall_camera_roll_rad': '-0.028',
        # Drive 1 m in front of an uncertain suspicion before asking. Needs the
        # depth switch for map points; without a point it asks where it stands.
        'fall_approach': 'true',
        'fall_pose_python_executable': os.environ.get(
            'MALBUT_FALL_POSE_PYTHON', str(pose_python)),
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
        'speech_agent_user_id': 'speech-development-user',
        'speech_agent_conversation_db': '~/.local/state/malbut/speech-dialogue.sqlite3',
        'speech_manager_commands': 'true',
        'speech_prerecorded_audio': 'true', 'speech_audio_directory': '',
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
        # zone_filter keeps the selected saved map's rooms here (SWM25-237).
        'room_map_file': str(Path.home() / '.ros/malbut/zones/active.user-map.geojson'),
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
    values.update(patrol='true', autoslam='true', manual='true', homecam='auto',
                  fall_manager_runtime_id='', fall_bridge_runtime_id='',
                  fall_vlm_runtime_id='')
    return values


def declarations(names):
    values = defaults()
    return [DeclareLaunchArgument(
        name, default_value=values[name],
        choices=(['true', 'false'] if values[name] in ('true', 'false') else
                 ['auto', 'true', 'false'] if name in ('homecam', 'fall_monitor') else None),
    ) for name in names]


def file_path(value, label):
    path = Path(value).expanduser()
    if not value or not path.is_file():
        raise RuntimeError(f'{label}: file not found: {value!r}')
    return str(path.resolve())


def package_file(package, relative):
    return file_path(Path(get_package_share_directory(package)) / relative,
                     f'{package}/{relative}')


def include(path, arguments, *, environment=None):
    return GroupAction([
        *[SetEnvironmentVariable(name, value)
          for name, value in (environment or {}).items()],
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(path),
            launch_arguments={**arguments, 'use_sim_time': 'false'}.items()),
    ], scoped=True)


def module_actions(name, setup):
    def start(context):
        modules = getattr(context.locals, 'malbut_modules', None)
        if modules is None:
            modules = {}
            context.extend_globals({'malbut_modules': modules})
        try:
            actions = setup(context)
        except (RuntimeError, ValueError, OSError) as error:
            modules[name] = str(error)
            if not getattr(context.locals, 'malbut_modular_bringup', False):
                raise
            return [LogInfo(msg=f'Module {name} was not started: {error}')]
        modules[name] = ''
        return actions

    return GroupAction([
        SetEnvironmentVariable('MALBUT_MEASUREMENT_LAUNCH', name),
        OpaqueFunction(function=start),
    ], scoped=True)


def description(name, setup, arguments):
    return LaunchDescription([
        *declarations(arguments),
        module_actions(name, setup),
    ])
