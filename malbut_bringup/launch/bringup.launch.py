"""Compose independent robot modules; no feature waits for another feature."""

from pathlib import Path
from uuid import uuid4

from launch import LaunchDescription
from launch.actions import LogInfo, OpaqueFunction, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.launch_support import defaults, declarations, include, package_file
from malbut_bringup.speech_audio import shared_xfm_source


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    context.extend_globals({'malbut_modular_bringup': True, 'malbut_modules': {}})
    options = {name: value(name) for name in defaults()}
    enabled = ['robot']
    for name, option in (('tracking', 'perception'), ('patrol', 'patrol'),
                         ('autoslam', 'autoslam'), ('manual', 'manual'),
                         ('relocalization', 'relocalization')):
        if value(option) == 'true':
            enabled.append(name)
    media = value('homecam') == 'true' or (
        value('homecam') == 'auto' and bool(context.environment.get('HOMECAM_BACKEND_URL', '')))
    if media:
        enabled.append('homecam')
    if value('fall_monitor') != 'false' and (
            value('fall_monitor') == 'true' or Path(value('fall_config')).expanduser().is_file()):
        enabled.append('fall')
        for peer in ('manager', 'bridge', 'vlm'):
            key = 'fall_' + peer + '_runtime_id'
            options[key] = options[key] or str(uuid4())
    if value('speech') == 'true':
        enabled.append('speech')
    observed = list(enabled)
    # Shared microphone setup is local wiring, not a ROS readiness prerequisite.
    # A failure affects only the two microphone consumers, never navigation.
    actions = [SetEnvironmentVariable('need_compile', 'False')]
    audio_env = {}
    if media and 'speech' in enabled and value('speech_input_device') == '0':
        try:
            source = shared_xfm_source(dict(context.environment))
            audio_env = {'MALBUT_SHARED_MICROPHONE': source, 'PULSE_SOURCE': source}
        except RuntimeError as error:
            for name in ('homecam', 'speech'):
                enabled.remove(name)
                context.locals.malbut_modules[name] = str(error)
            actions.append(LogInfo(msg=str(error)))
    for name in enabled:
        settings = dict(options)
        if name == 'robot' and value('relocalization') != 'true':
            settings['restore_pose'] = 'false'
        if name == 'speech':
            settings = {
                'python_executable': value('speech_python_executable'),
                'stt_model_path': value('stt_model_path'),
                'stt_library_path': value('stt_library_path'),
                'input_device': value('speech_input_device'),
                'output_device': value('speech_output_device'),
                'cpp_threads': value('stt_cpp_threads'),
                'input_has_aec': value('speech_input_has_aec'),
                'agent_provider': value('speech_agent_provider'),
                'agent_user_id': value('speech_agent_user_id'),
                'agent_conversation_db': value('speech_agent_conversation_db'),
                'manager_commands': value('speech_manager_commands'),
                'prerecorded_audio': value('speech_prerecorded_audio'),
                'audio_directory': value('speech_audio_directory'),
                'preflight_timeout_s': value('speech_preflight_timeout_s'),
                'peer_timeout_s': value('speech_peer_timeout_s'),
                'preflight_only': 'false',
            }
        actions.append(include(
            package_file('malbut_bringup', f'launch/{name}.launch.py'), settings,
            environment=audio_env if name in ('homecam', 'speech') else None))
    nodes = {
        'robot': ['system_manager', 'controller_server', 'planner_server', 'bt_navigator',
                  'behavior_server', 'teleop_behavior_server', 'velocity_smoother',
                  'collision_monitor', 'smoother_server', 'waypoint_follower'],
        'tracking': ['yolo/yolo_node', 'person_reidentifier', 'person_localizer',
                     'person_follower', 'lidar_foreground_preprocessor'],
        'patrol': ['patrol_manager'], 'autoslam': ['autoslam', 'autoslam_map_saver'],
        'manual': ['manual_control'], 'relocalization': ['relocalization'],
        'homecam': ['homecam_media_agent'],
        'fall': ['fall_coordinator', 'malbut_fall_pose', 'malbut_cloud_fall_monitor'],
        'speech': ['malbut_stt', 'malbut_agent_communication', 'malbut_tts'],
    }
    endpoints = {
        'robot': ['/malbut/mission/execute', '/navigate_to_pose', '/compute_path_to_pose',
                  '/follow_path', '/spin', '/wait', '/backup', '/assisted_teleop'],
        'tracking': ['/follow_person'], 'patrol': ['/patrol'], 'autoslam': ['/autoslam'],
        'relocalization': ['/relocalize'],
    }
    topic_options = ['scan_topic', 'odom_topic', 'static_map_topic',
                     'global_costmap_topic', 'patrol_costmap_topic']
    if 'tracking' in observed:
        topic_options += ['rgb_topic', 'depth_topic', 'camera_info_topic']
    elif 'homecam' in observed or 'fall' in observed:
        topic_options.append('rgb_topic')
    # A read-only sibling, not a gate or an owner of any module's process.
    actions.append(Node(
        package='malbut_bringup', executable='wait_for_robot', name='bringup_connections',
        output='screen', parameters=[{
            'use_sim_time': False, 'observe_only': True, 'speech': 'speech' in observed,
            'startup_nodes': ','.join(node for name in observed for node in nodes[name]),
            'required_actions': ','.join(action for name in observed
                                         for action in endpoints.get(name, [])),
            'required_topics': ','.join(dict.fromkeys(value(name) for name in topic_options)),
        }]))
    if value('web_panel') == 'true':
        actions.append(Node(
            package='malbut_bringup', executable='robot_web_panel',
            name='robot_web_panel', output='screen', parameters=[{
                'use_sim_time': False, 'manage_bringup': False,
                'map_directory': value('map_directory'),
                'map_topic': value('patrol_costmap_topic'),
                'robot_frame': value('robot_frame'), 'rgb_topic': value('rgb_topic'),
            }]))
    return actions


def generate_launch_description():
    return LaunchDescription([
        *declarations(defaults()),
        OpaqueFunction(function=_setup),
    ])
